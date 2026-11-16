"""Publications sur la page Facebook : brouillons, programmation, publication, nouvelle tentative, statistiques.

Cycle de vie : draft -> scheduled -> publishing -> published   (ou failed ; published -> deleted)
- Validation humaine : rien ne part sans une action explicite de la console (« Publier » ou « Programmer »).
- Programmation par NOTRE planificateur (le worker), pas par Facebook : on peut modifier ou annuler jusqu'à la dernière
  minute, et aucune contrainte de fenêtre côté Facebook.
- Une publication « en cours » est réclamée de façon atomique ; si le worker s'arrête en plein envoi, elle est reprise
  après 10 minutes (risque connu et rare de doublon si Facebook avait déjà publié).
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.text import clean_for_messenger
from app.crypto import decrypt
from app.db import routing_session, tenant_session
from app.services import facebook as fb

log = logging.getLogger(__name__)

MAX_MESSAGE = 3000
MAX_IMAGE_BYTES = 8_000_000
MAX_ATTEMPTS = 3
BACKOFF_MINUTES = (1, 5, 15)
EDITABLE = ("draft", "scheduled", "failed")
STATUS_LABELS = {"draft": "Brouillon", "scheduled": "Programmée", "publishing": "Publication en cours",
                 "published": "Publiée", "failed": "Échec", "deleted": "Supprimée de Facebook"}


class PostError(Exception):
    """Erreur de saisie ou d'état, lisible par l'utilisateur."""


def sniff_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def clean_message(message: str) -> str:
    message = (message or "").replace("\r\n", "\n").strip()
    if len(message) > MAX_MESSAGE:
        raise PostError(f"Le texte est trop long ({MAX_MESSAGE} caractères maximum).")
    return message


def parse_local(value: str, offset_hours: int) -> datetime:
    """« 2026-11-12T09:00 » (heure locale de l'entreprise) -> datetime UTC."""
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise PostError("Date de programmation invalide.")
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours))).astimezone(timezone.utc)


def check_schedule(when_utc: datetime, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if when_utc < now + timedelta(minutes=1):
        raise PostError("L'heure choisie est déjà passée ou trop proche : choisissez au moins une minute dans le futur.")
    if when_utc > now + timedelta(days=365):
        raise PostError("La programmation est limitée à un an.")


_COLS = ("id, status, message, source, product_sku, scheduled_for, attempts, next_attempt_at, fb_post_id, permalink, error, "
         "published_at, reactions, comments, shares, stats_at, created_at, updated_at, "
         "EXISTS (SELECT 1 FROM social_post_media m WHERE m.post_id = social_posts.id) AS has_image")


def _row(r) -> dict:
    d = dict(r._mapping)
    d["label"] = STATUS_LABELS.get(d["status"], d["status"])
    return d


def get_post(s: Session, post_id: UUID) -> dict | None:
    r = s.execute(text(f"SELECT {_COLS} FROM social_posts WHERE id = :i"), {"i": post_id}).first()
    return _row(r) if r else None


def list_posts(s: Session, status: str | None = None, limit: int = 200) -> list[dict]:
    where = "WHERE status = :st" if status in STATUS_LABELS else ""
    rows = s.execute(
        text(f"""SELECT {_COLS} FROM social_posts {where}
                 ORDER BY COALESCE(scheduled_for, published_at, created_at) DESC LIMIT :n"""),
        {"n": limit, **({"st": status} if where else {})},
    ).all()
    return [_row(r) for r in rows]


def posts_between(s: Session, start: datetime, end: datetime) -> list[dict]:
    rows = s.execute(
        text(f"""SELECT {_COLS}, COALESCE(published_at, scheduled_for, created_at) AS day_at FROM social_posts
                 WHERE status <> 'deleted' AND COALESCE(published_at, scheduled_for, created_at) >= :a
                   AND COALESCE(published_at, scheduled_for, created_at) < :b
                 ORDER BY day_at"""), {"a": start, "b": end},
    ).all()
    return [_row(r) for r in rows]


def create_post(s: Session, tenant_id: UUID, *, message: str, source: str = "human", product_sku: str | None = None) -> UUID:
    return s.execute(
        text("""INSERT INTO social_posts (tenant_id, message, source, product_sku) VALUES (:t, :m, :src, :sku) RETURNING id"""),
        {"t": tenant_id, "m": clean_message(message), "src": "ai" if source == "ai" else "human", "sku": product_sku or None},
    ).scalar_one()


def update_post(s: Session, post_id: UUID, *, message: str, product_sku: str | None = None, source: str | None = None) -> None:
    res = s.execute(
        text("""UPDATE social_posts SET message = :m, product_sku = :sku, source = COALESCE(:src, source), updated_at = now()
                WHERE id = :i AND status IN ('draft', 'scheduled', 'failed')"""),
        {"i": post_id, "m": clean_message(message), "sku": product_sku or None, "src": "ai" if source == "ai" else None},
    )
    if res.rowcount == 0:
        raise PostError("Cette publication ne peut plus être modifiée (déjà publiée ou en cours d'envoi).")


def set_image(s: Session, tenant_id: UUID, post_id: UUID, data: bytes) -> str:
    if len(data) > MAX_IMAGE_BYTES:
        raise PostError("Image trop volumineuse (8 Mo maximum).")
    mime = sniff_image(data)
    if mime is None:
        raise PostError("Format d'image non pris en charge (JPEG, PNG ou GIF).")
    if s.execute(text("SELECT 1 FROM social_posts WHERE id = :i AND status IN ('draft', 'scheduled', 'failed')"), {"i": post_id}).first() is None:
        raise PostError("Cette publication ne peut plus être modifiée.")
    s.execute(text("DELETE FROM social_post_media WHERE post_id = :i"), {"i": post_id})
    s.execute(
        text("INSERT INTO social_post_media (post_id, tenant_id, content_type, size, data) VALUES (:i, :t, :ct, :n, :d)"),
        {"i": post_id, "t": tenant_id, "ct": mime, "n": len(data), "d": data},
    )
    s.execute(text("UPDATE social_posts SET updated_at = now() WHERE id = :i"), {"i": post_id})
    return mime


def remove_image(s: Session, post_id: UUID) -> None:
    s.execute(text("DELETE FROM social_post_media WHERE post_id = :i"), {"i": post_id})


def get_image(s: Session, post_id: UUID) -> tuple[bytes, str] | None:
    r = s.execute(text("SELECT data, content_type FROM social_post_media WHERE post_id = :i"), {"i": post_id}).first()
    return (bytes(r.data), r.content_type) if r else None


def _require_content(s: Session, post: dict) -> None:
    if not post["message"] and not post["has_image"]:
        raise PostError("Ajoutez un texte ou une image avant de publier.")


def schedule(s: Session, post_id: UUID, when_utc: datetime) -> None:
    check_schedule(when_utc)
    post = get_post(s, post_id)
    if post is None or post["status"] not in EDITABLE:
        raise PostError("Cette publication ne peut plus être programmée.")
    _require_content(s, post)
    s.execute(
        text("""UPDATE social_posts SET status = 'scheduled', scheduled_for = :w, attempts = 0, next_attempt_at = NULL,
                  error = NULL, updated_at = now() WHERE id = :i"""), {"i": post_id, "w": when_utc})


def unschedule(s: Session, post_id: UUID) -> None:
    res = s.execute(
        text("UPDATE social_posts SET status = 'draft', scheduled_for = NULL, next_attempt_at = NULL, updated_at = now() "
             "WHERE id = :i AND status = 'scheduled'"), {"i": post_id})
    if res.rowcount == 0:
        raise PostError("Cette publication n'est pas programmée.")


def delete_draft(s: Session, post_id: UUID) -> None:
    res = s.execute(text("DELETE FROM social_posts WHERE id = :i AND status IN ('draft', 'scheduled', 'failed')"), {"i": post_id})
    if res.rowcount == 0:
        raise PostError("Seuls les brouillons, publications programmées ou en échec peuvent être supprimés ici.")


# --- connexion à la page -----------------------------------------------------------------------------------

def page_credentials(s: Session) -> tuple[str, str]:
    row = s.execute(
        text("SELECT external_id, credentials_encrypted FROM channels WHERE type = 'messenger' AND status = 'active' LIMIT 1")
    ).first()
    if row is None or not row.credentials_encrypted:
        raise PostError("Aucune page Facebook connectée à cette entreprise.")
    return row.external_id, decrypt(row.credentials_encrypted)


def default_client_factory(page_id: str, token: str) -> fb.FacebookPageClient:
    return fb.FacebookPageClient(page_id, token)


def test_connection(tenant_id: UUID, client_factory=None) -> dict:
    with tenant_session(tenant_id) as s:
        page_id, token = page_credentials(s)
    return (client_factory or default_client_factory)(page_id, token).whoami()


# --- publication --------------------------------------------------------------------------------------------

def _claim(tenant_id: UUID, post_id: UUID):
    """Passe la publication à « publishing » de façon atomique et charge ce qu'il faut pour l'envoyer."""
    with tenant_session(tenant_id) as s:
        row = s.execute(
            text("""UPDATE social_posts SET status = 'publishing', error = NULL, updated_at = now()
                    WHERE id = :i AND (status IN ('draft', 'scheduled', 'failed')
                                       OR (status = 'publishing' AND updated_at < now() - interval '10 minutes'))
                    RETURNING message, attempts"""), {"i": post_id}).first()
        if row is None:
            return None
        image = get_image(s, post_id)
        if not row.message and image is None:
            s.execute(text("UPDATE social_posts SET status = 'draft', updated_at = now() WHERE id = :i"), {"i": post_id})
            raise PostError("Ajoutez un texte ou une image avant de publier.")
        try:
            page_id, token = page_credentials(s)
        except PostError:
            s.execute(text("UPDATE social_posts SET status = 'draft', updated_at = now() WHERE id = :i"), {"i": post_id})
            raise
        return row.message, row.attempts, image, page_id, token


def publish(tenant_id: UUID, post_id: UUID, client_factory=None, *, retry_transient: bool = False) -> dict:
    """Publie sur la page. Appelle Facebook HORS transaction. Retourne {status, permalink?, error?}.

    retry_transient : mode planificateur (worker) : une erreur passagère reprogramme la publication (3 essais, délais croissants).
    Sinon (clic dans la console) l'échec est immédiat et visible, avec un bouton « Réessayer ».
    """
    claimed = _claim(tenant_id, post_id)
    if claimed is None:
        raise PostError("Cette publication n'est pas dans un état publiable (déjà publiée ou en cours).")
    message, attempts, image, page_id, token = claimed
    try:
        client = (client_factory or default_client_factory)(page_id, token)
        result = client.publish_photo(image[0], image[1], message) if image else client.publish_text(message)
    except fb.FacebookError as e:
        return _record_failure(tenant_id, post_id, e, attempts + 1, retry_transient)
    with tenant_session(tenant_id) as s:
        s.execute(
            text("""UPDATE social_posts SET status = 'published', fb_post_id = :p, permalink = :l, published_at = now(),
                      attempts = :n, error = NULL, next_attempt_at = NULL, updated_at = now() WHERE id = :i"""),
            {"i": post_id, "p": result.post_id, "l": result.permalink, "n": attempts + 1})
    log.info("publication %s -> Facebook %s", post_id, result.post_id)
    return {"status": "published", "permalink": result.permalink}


def _record_failure(tenant_id: UUID, post_id: UUID, e: fb.FacebookError, attempts: int, retry_transient: bool) -> dict:
    log.warning("publication %s: échec Facebook (%s) tentative %s", post_id, e, attempts)
    with tenant_session(tenant_id) as s:
        if retry_transient and not e.permanent and attempts < MAX_ATTEMPTS:
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            s.execute(
                text("""UPDATE social_posts SET status = 'scheduled', scheduled_for = COALESCE(scheduled_for, now()),
                          attempts = :n, error = :e, updated_at = now(),
                          next_attempt_at = now() + make_interval(mins => :d) WHERE id = :i"""),
                {"i": post_id, "n": attempts, "e": e.user_message, "d": delay})
            return {"status": "scheduled", "error": e.user_message, "retry_in_minutes": delay}
        s.execute(
            text("UPDATE social_posts SET status = 'failed', attempts = :n, error = :e, updated_at = now() WHERE id = :i"),
            {"i": post_id, "n": attempts, "e": e.user_message})
    return {"status": "failed", "error": e.user_message}


def publish_due(client_factory=None, limit: int = 5) -> list[tuple[UUID, str]]:
    """Planificateur : publie les publications dont l'heure est venue (tous clients). Appelé périodiquement par le worker."""
    with routing_session() as s:
        due = s.execute(
            text("SELECT post_id, tenant_id FROM social_schedule WHERE run_at <= now() ORDER BY run_at LIMIT :n"), {"n": limit}
        ).all()
    done = []
    for row in due:
        try:
            out = publish(row.tenant_id, row.post_id, client_factory, retry_transient=True)
            done.append((row.post_id, out["status"]))
        except PostError as e:
            log.info("publication %s ignorée: %s", row.post_id, e)
        except Exception:
            log.exception("publication %s: erreur inattendue", row.post_id)
    return done


def delete_from_facebook(tenant_id: UUID, post_id: UUID, client_factory=None) -> None:
    with tenant_session(tenant_id) as s:
        post = get_post(s, post_id)
        if post is None or post["status"] != "published" or not post["fb_post_id"]:
            raise PostError("Seules les publications publiées peuvent être retirées de Facebook.")
        page_id, token = page_credentials(s)
    (client_factory or default_client_factory)(page_id, token).delete_post(post["fb_post_id"])
    with tenant_session(tenant_id) as s:
        s.execute(text("UPDATE social_posts SET status = 'deleted', updated_at = now() WHERE id = :i"), {"i": post_id})


def refresh_stats(tenant_id: UUID, post_id: UUID, client_factory=None) -> dict:
    with tenant_session(tenant_id) as s:
        post = get_post(s, post_id)
        if post is None or post["status"] != "published" or not post["fb_post_id"]:
            raise PostError("Les statistiques n'existent que pour une publication publiée.")
        page_id, token = page_credentials(s)
    stats = (client_factory or default_client_factory)(page_id, token).get_stats(post["fb_post_id"])
    with tenant_session(tenant_id) as s:
        s.execute(text("UPDATE social_posts SET reactions = :r, comments = :c, shares = :sh, stats_at = now() WHERE id = :i"),
                  {"i": post_id, "r": stats["reactions"], "c": stats["comments"], "sh": stats["shares"]})
    return stats
