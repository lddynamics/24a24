"""Adaptateur WhatsApp Business (Cloud API) : traduction des webhooks vers NormalizedMessage.

Différences avec Messenger qui comptent ici :
- Le canal est identifié par `metadata.phone_number_id` (pas par un identifiant de page) ; le contact par son `wa_id` (numéro).
- La signature est la même (X-Hub-Signature-256, secret de l'app Meta) : on réutilise `verify_signature`.
- Un média n'arrive PAS sous forme d'URL mais d'un identifiant : il faut le jeton du canal pour l'obtenir (voir
  `HttpMediaFetcher`). On stocke donc `media_ref = "wa-media:<id>"`.
- Un envoi accepté par l'API n'est pas un envoi livré : les échecs tardifs arrivent par des webhooks `statuses`.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.channels.messenger import verify_signature  # noqa: F401  (même algorithme que Messenger)
from app.schemas import NormalizedMessage

log = logging.getLogger(__name__)

MEDIA_PREFIX = "wa-media:"
EXPECTED_OBJECT = "whatsapp_business_account"

# Types qui portent un média : type WhatsApp -> type normalisé. Les stickers sont rangés en « other » (non lisibles par l'agent).
_MEDIA_KINDS = {"image": "image", "audio": "audio", "video": "video", "document": "file", "sticker": "other"}
_CAPTIONED = {"image", "video", "document"}
# Événements qui ne sont pas des messages à traiter : une réaction 👍 ne doit pas déclencher de réponse de l'agent.
_IGNORED_TYPES = {"reaction", "system", "request_welcome"}


@dataclass
class DeliveryStatus:
    phone_number_id: str
    message_id: str            # wamid du message sortant concerné
    status: str                # sent | delivered | read | failed
    error_code: int | None = None
    error_title: str | None = None


def _timestamp(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC)


def _location_text(loc: dict) -> str:
    parts = [f"latitude {loc.get('latitude')}, longitude {loc.get('longitude')}"]
    for key in ("name", "address"):
        if loc.get(key):
            parts.append(str(loc[key]))
    return "[Localisation partagée par le client] " + " — ".join(parts)


def _interactive_text(m: dict) -> str | None:
    inter = m.get("interactive") or {}
    reply = inter.get("button_reply") or inter.get("list_reply") or {}
    return reply.get("title")


def _normalize(m: dict) -> tuple[str, str | None, str | None] | None:
    """(type normalisé, texte, media_ref) d'un message WhatsApp, ou None s'il faut l'ignorer."""
    kind = m.get("type")
    if kind in _IGNORED_TYPES:
        return None
    if kind == "text":
        return "text", (m.get("text") or {}).get("body"), None
    if kind in _MEDIA_KINDS:
        block = m.get(kind) or {}
        media_id = block.get("id")
        caption = block.get("caption") if kind in _CAPTIONED else None
        return _MEDIA_KINDS[kind], caption, f"{MEDIA_PREFIX}{media_id}" if media_id else None
    if kind == "button":          # réponse rapide à un modèle de message
        return "text", (m.get("button") or {}).get("text"), None
    if kind == "interactive":     # réponse à des boutons / une liste
        return "text", _interactive_text(m), None
    if kind == "location":
        return "other", _location_text(m.get("location") or {}), None
    return "other", None, None    # contacts, order, unsupported...


def parse_payload(payload: dict) -> list[NormalizedMessage]:
    """Extrait les messages entrants. Ignore statuts, réactions et événements système."""
    messages: list[NormalizedMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if not phone_number_id:
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                sender, wamid = m.get("from"), m.get("id")
                if not sender or not wamid:
                    continue
                normalized = _normalize(m)
                if normalized is None:
                    log.debug("événement WhatsApp ignoré (type %s)", m.get("type"))
                    continue
                kind, text, media_ref = normalized
                messages.append(
                    NormalizedMessage(
                        channel_type="whatsapp",
                        channel_external_id=phone_number_id,
                        channel_user_id=str(sender),
                        contact_name=names.get(sender) or None,
                        type=kind,
                        text=text,
                        media_ref=media_ref,
                        received_at=_timestamp(m.get("timestamp")),
                        idempotency_key=f"whatsapp:{wamid}",
                        raw=m,
                    )
                )
    return messages


def parse_statuses(payload: dict) -> list[DeliveryStatus]:
    """Statuts de messages sortants (envoyé, livré, lu, échec)."""
    out: list[DeliveryStatus] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if not phone_number_id:
                continue
            for st in value.get("statuses") or []:
                if not st.get("id") or not st.get("status"):
                    continue
                err = (st.get("errors") or [{}])[0]
                code = err.get("code")
                out.append(
                    DeliveryStatus(
                        phone_number_id=phone_number_id,
                        message_id=str(st["id"]),
                        status=str(st["status"]),
                        error_code=code if isinstance(code, int) else None,
                        error_title=err.get("title"),
                    )
                )
    return out
