"""Adaptateur WhatsApp NON OFFICIEL via Evolution API (session WhatsApp Web / Baileys) : webhooks -> NormalizedMessage.

À réserver aux prototypes : ça ne passe pas par Meta, donc pas de vérification d'entreprise ni de numéro de test, mais
WhatsApp interdit ce mode dans ses conditions d'utilisation (risque de bannissement du numéro : utiliser un numéro dédié).

Conventions (pour garder le reste du projet inchangé) :
- `channel_external_id` = le NOM DE L'INSTANCE Evolution (c'est elle qui identifie le numéro connecté) ; on le stocke dans
  `channels.external_id` comme le « phone_number_id » de l'API Meta (voir `credentials.py`).
- `channel_user_id` = le numéro du contact en chiffres (ex. 22901XXXXXXXX), ou le JID `...@lid` si WhatsApp ne donne pas le numéro.
- Texte et VOCAUX sont traités. Un vocal porte `media_ref = "evo-media:<instance>:<id du message>"` : le worker le télécharge via
  l'API d'Evolution (voir `HttpMediaFetcher`) puis le transcrit. Les images et autres médias sont signalés mais pas téléchargés.
"""
import hmac
import logging
from datetime import UTC, datetime

from app.schemas import NormalizedMessage

log = logging.getLogger(__name__)

MEDIA_PREFIX = "evo-media:"
SECRET_HEADER = "X-Webhook-Secret"   # en-tête que l'on demande à Evolution d'ajouter à chaque appel (voir scripts/setup_evolution_instance.py)

_WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage")
_MEDIA = (("imageMessage", "image"), ("videoMessage", "video"), ("documentMessage", "file"), ("audioMessage", "audio"),
          ("stickerMessage", "other"))
# Événements qui ne sont pas des messages à traiter (réaction, suppression, synchronisation des clés...).
_IGNORED = ("reactionMessage", "protocolMessage", "senderKeyDistributionMessage", "messageContextInfo")


def verify_secret(expected: str, received: str | None) -> bool:
    """Compare le secret partagé en temps constant. Un secret non configuré refuse tout (jamais de webhook ouvert)."""
    if not expected or not received:
        return False
    return hmac.compare_digest(expected.encode(), received.encode())


def _timestamp(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC)


def _unwrap(message: dict) -> dict:
    """Les messages temporaires / « vue unique » enveloppent le vrai contenu dans un niveau de plus."""
    for _ in range(3):
        for wrapper in _WRAPPERS:
            inner = (message.get(wrapper) or {}).get("message")
            if inner:
                message = inner
                break
        else:
            break
    return message


def _user_id(key: dict) -> str | None:
    """Identifiant du contact, ou None s'il faut ignorer (groupes, statuts, chaînes)."""
    jid = key.get("remoteJid") or ""
    if jid.endswith("@lid"):  # WhatsApp masque parfois le numéro : une variante du JID peut le donner
        jid = key.get("remoteJidAlt") or key.get("senderPn") or jid
    if jid.endswith("@s.whatsapp.net"):
        return jid.split("@")[0].split(":")[0]
    if jid.endswith("@lid"):
        return jid
    return None


def _content(message: dict) -> tuple[str, str | None] | None:
    """(type normalisé, texte) d'un message, ou None s'il faut l'ignorer."""
    if not message or all(k in _IGNORED for k in message):
        return None
    m = _unwrap(message)
    if "conversation" in m:
        return "text", m["conversation"]
    if "extendedTextMessage" in m:
        return "text", (m["extendedTextMessage"] or {}).get("text")
    if "buttonsResponseMessage" in m:
        return "text", (m["buttonsResponseMessage"] or {}).get("selectedDisplayText")
    if "listResponseMessage" in m:
        return "text", (m["listResponseMessage"] or {}).get("title")
    for key, kind in _MEDIA:
        if key in m:
            return kind, (m[key] or {}).get("caption")
    if "locationMessage" in m:
        loc = m["locationMessage"] or {}
        return "other", f"[Localisation partagée par le client] latitude {loc.get('degreesLatitude')}, longitude {loc.get('degreesLongitude')}"
    return "other", None


def parse_payload(payload: dict) -> list[NormalizedMessage]:
    """Extrait les messages ENTRANTS d'un webhook Evolution (`messages.upsert`). Ignore tout le reste."""
    event = str(payload.get("event") or "").lower().replace("_", ".")
    instance = payload.get("instance")
    if event != "messages.upsert" or not instance:
        return []
    data = payload.get("data")
    items = data if isinstance(data, list) else [data]
    out: list[NormalizedMessage] = []
    for d in items:
        if not isinstance(d, dict):
            continue
        key = d.get("key") or {}
        if key.get("fromMe"):  # écho de nos propres envois
            continue
        message_id, user = key.get("id"), _user_id(key)
        if not message_id or not user:
            continue
        content = _content(d.get("message") or {})
        if content is None:
            log.debug("événement WhatsApp ignoré (type %s)", d.get("messageType"))
            continue
        kind, text = content
        media_ref = f"{MEDIA_PREFIX}{instance}:{message_id}" if kind == "audio" else None
        out.append(
            NormalizedMessage(
                channel_type="whatsapp",
                channel_external_id=str(instance),
                channel_user_id=user,
                type=kind,
                text=text,
                media_ref=media_ref,
                received_at=_timestamp(d.get("messageTimestamp")),
                idempotency_key=f"whatsapp:{message_id}",
                raw=d,
            )
        )
    return out
