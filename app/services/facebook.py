"""Client de l'API Graph de Facebook pour une page : publier (texte ou photo), lire les statistiques, supprimer.

Documentation : developers.facebook.com/documentation/pages-api/posts
- texte : POST /{page-id}/feed (message, link) ; photo : POST /{page-id}/photos (source ou url, caption) ;
- suppression : DELETE /{post-id} ; lien d'une publication : https://www.facebook.com/{post-id}.
Le jeton doit être un jeton de PAGE avec pages_manage_posts (+ pages_read_engagement, pages_show_list).
"""
import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

MESSAGES = {
    "token": "Le jeton d'accès de la page est invalide ou expiré. Générez un nouveau jeton de page puis enregistrez-le "
             "avec « python -m scripts.set_page_token ».",
    "permission": "Le jeton de la page n'a pas la permission de publier (pages_manage_posts). Générez un jeton de page avec "
                  "pages_manage_posts, pages_read_engagement et pages_show_list, puis enregistrez-le avec "
                  "« python -m scripts.set_page_token ».",
    "rate": "Facebook limite temporairement les publications. Réessayez dans quelques minutes.",
    "duplicate": "Facebook refuse une publication identique à une publication récente. Modifiez légèrement le texte.",
    "blocked": "Facebook a temporairement bloqué la publication (règles anti-spam). Réessayez plus tard.",
    "network": "Facebook est injoignable pour le moment.",
}


class FacebookError(Exception):
    """Erreur Facebook. `permanent` : inutile de réessayer tel quel ; `user_message` : explication en français."""

    def __init__(self, detail: str, *, kind: str = "api", permanent: bool = True, code: int | None = None):
        super().__init__(detail)
        self.kind, self.permanent, self.code = kind, permanent, code

    @property
    def user_message(self) -> str:
        return MESSAGES.get(self.kind) or f"Facebook a refusé la requête : {str(self)[:160]}"


@dataclass
class PostResult:
    post_id: str
    permalink: str


def classify(status: int, body: dict | None) -> FacebookError:
    err = (body or {}).get("error") or {}
    code, msg = err.get("code"), str(err.get("message") or "")
    detail = f"{status} (code {code}) {msg}"[:300]
    if code == 190:
        return FacebookError(detail, kind="token", code=code)
    if code in (10, 200, 283) or (code is not None and 200 <= int(code) < 300) or "permission" in msg.lower():
        return FacebookError(detail, kind="permission", code=code)
    if code in (4, 17, 32, 341, 613):
        return FacebookError(detail, kind="rate", permanent=False, code=code)
    if code == 368:
        return FacebookError(detail, kind="blocked", code=code)
    if code == 506:
        return FacebookError(detail, kind="duplicate", code=code)
    if code in (1, 2) or status >= 500 or status == 429:
        return FacebookError(detail, kind="api", permanent=False, code=code)
    return FacebookError(detail, kind="api", code=code)


class FacebookPageClient:
    def __init__(self, page_id: str, token: str, client: httpx.Client | None = None):
        self.page_id = page_id
        self._token = token
        self._base = f"https://graph.facebook.com/{get_settings().graph_api_version}"
        self._http = client or httpx.Client(timeout=45)

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._http.request(method, f"{self._base}{path}", headers={"Authorization": f"Bearer {self._token}"}, **kw)
        except httpx.TransportError as e:
            raise FacebookError(f"réseau: {type(e).__name__}", kind="network", permanent=False) from e
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            raise classify(r.status_code, body if isinstance(body, dict) else None)
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _result(data: dict) -> PostResult:
        post_id = str(data.get("post_id") or data.get("id") or "")
        if not post_id:
            raise FacebookError("réponse Facebook sans identifiant de publication", kind="api", permanent=False)
        return PostResult(post_id, f"https://www.facebook.com/{post_id}")

    def whoami(self) -> dict:
        """Avec un jeton de page, /me est la page elle-même : sert à tester la connexion."""
        data = self._call("GET", "/me", params={"fields": "id,name"})
        return {"id": data.get("id"), "name": data.get("name")}

    def publish_text(self, message: str, link: str | None = None) -> PostResult:
        form = {"message": message, **({"link": link} if link else {})}
        return self._result(self._call("POST", f"/{self.page_id}/feed", data=form))

    def publish_photo(self, data: bytes, content_type: str, caption: str) -> PostResult:
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}.get(content_type)
        if ext is None:
            raise FacebookError(f"format d'image non pris en charge: {content_type}", kind="api")
        out = self._call(
            "POST", f"/{self.page_id}/photos",
            data={"caption": caption, "published": "true"},
            files={"source": (f"photo.{ext}", data, content_type)},
        )
        return self._result(out)

    def get_stats(self, post_id: str) -> dict:
        data = self._call("GET", f"/{post_id}", params={
            "fields": "reactions.summary(true).limit(0),comments.summary(true).limit(0),shares"})
        total = lambda k: int(((data.get(k) or {}).get("summary") or {}).get("total_count") or 0)  # noqa: E731
        return {"reactions": total("reactions"), "comments": total("comments"), "shares": int((data.get("shares") or {}).get("count") or 0)}

    def delete_post(self, post_id: str) -> None:
        self._call("DELETE", f"/{post_id}")
