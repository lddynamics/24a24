"""Requêtes et actions de la console d'entreprise. Toujours appelées dans `tenant_session` (RLS)."""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.channels.sender import SendError, get_sender
from app.db import tenant_session
from app.models import Conversation, Message
from app.services import media as media_service

log = logging.getLogger(__name__)

MAX_REPLY = 1800
ORDER_STATUSES = ("draft", "awaiting_payment", "paid", "delivered", "failed", "cancelled")
# Transitions permises depuis la console (les remboursements restent manuels et hors outil).
ORDER_TRANSITIONS = {
    "paid": {"delivered"},
    "draft": {"cancelled"},
    "awaiting_payment": {"cancelled"},
    "failed": {"cancelled"},
}


def tz(offset_hours: int) -> timezone:
    return timezone(timedelta(hours=offset_hours))


# --- tableau de bord ---------------------------------------------------------------------------------

def dashboard(s: Session, offset_hours: int) -> dict:
    zone = tz(offset_hours)
    now = datetime.now(zone)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    conv = s.execute(
        text("""SELECT count(*) AS total, count(*) FILTER (WHERE status = 'human') AS human,
                  count(*) FILTER (WHERE last_message_at >= :t0) AS today FROM conversations"""), {"t0": today0}
    ).one()
    msgs = s.execute(
        text("""SELECT count(*) FILTER (WHERE direction = 'in') AS inbound,
                  count(*) FILTER (WHERE direction = 'out') AS outbound FROM messages WHERE created_at >= :since"""),
        {"since": now - timedelta(hours=24)},
    ).one()
    orders = s.execute(
        text("""SELECT count(*) AS total,
                  count(*) FILTER (WHERE status IN ('paid', 'delivered')) AS paid,
                  COALESCE(sum(total) FILTER (WHERE status IN ('paid', 'delivered')), 0) AS revenue,
                  COALESCE(sum(total) FILTER (WHERE status = 'awaiting_payment'), 0) AS pending
                FROM orders""")
    ).one()
    runs = s.execute(
        text("""SELECT count(*) AS total, count(*) FILTER (WHERE r.outcome = 'escalated') AS escalated,
                  percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY GREATEST(extract(epoch FROM (r.finished_at - m.received_at)), 0)) AS median_s
                FROM agent_runs r JOIN messages m ON m.id = r.trigger_message_id
                WHERE r.finished_at IS NOT NULL AND r.outcome IN ('replied', 'escalated')""")
    ).one()

    since = today0 - timedelta(days=6)
    rows = s.execute(
        text("""SELECT (paid_at + make_interval(hours => :off))::date AS d, count(*) AS n, sum(amount) AS amt
                FROM payments WHERE status = 'succeeded' AND paid_at >= :since GROUP BY 1"""),
        {"off": offset_hours, "since": since},
    ).all()
    by_day = {r.d: (r.n, int(r.amt)) for r in rows}
    series = []
    for i in range(6, -1, -1):
        d = (today0 - timedelta(days=i)).date()
        n, amt = by_day.get(d, (0, 0))
        series.append({"label": d.strftime("%d/%m"), "orders": n, "amount": amt})
    top = max((p["amount"] for p in series), default=0)
    for p in series:
        p["h"] = round(100 * p["amount"] / top) if top else 0

    return {
        "conversations": conv.total, "human_conversations": conv.human, "conversations_today": conv.today,
        "inbound_24h": msgs.inbound, "outbound_24h": msgs.outbound,
        "orders": orders.total, "paid_orders": orders.paid, "revenue": int(orders.revenue), "pending": int(orders.pending),
        "conversion": round(100 * orders.paid / conv.total) if conv.total else 0,
        "agent_runs": runs.total,
        "human_rate": round(100 * runs.escalated / runs.total) if runs.total else 0,
        "median_seconds": round(runs.median_s) if runs.median_s is not None else None,
        "series": series,
    }


# --- conversations -----------------------------------------------------------------------------------

def contact_label(name: str | None, channel_user_id: str) -> str:
    return name or f"Client Messenger …{channel_user_id[-4:]}"


def list_conversations(s: Session, *, limit: int = 100, status: str | None = None) -> list[dict]:
    where = "WHERE c.status = :st" if status else ""
    rows = s.execute(
        text(f"""SELECT c.id, c.status, c.last_message_at, ct.name, ct.channel_user_id,
                   lm.text AS last_text, lm.direction AS last_dir, lm.type AS last_type,
                   (SELECT count(*) FROM orders o WHERE o.conversation_id = c.id
                      AND o.status IN ('paid', 'delivered')) AS paid_orders
                 FROM conversations c
                 JOIN contacts ct ON ct.id = c.contact_id AND ct.tenant_id = c.tenant_id
                 LEFT JOIN LATERAL (SELECT text, direction, type FROM messages m WHERE m.conversation_id = c.id
                                    ORDER BY m.created_at DESC, m.id DESC LIMIT 1) lm ON true
                 {where}
                 ORDER BY c.last_message_at DESC NULLS LAST LIMIT :limit"""),
        {"limit": limit, **({"st": status} if status else {})},
    ).all()
    return [
        {"id": r.id, "status": r.status, "last_at": r.last_message_at, "label": contact_label(r.name, r.channel_user_id),
         "preview": _preview(r.last_text, r.last_type), "last_dir": r.last_dir, "paid_orders": r.paid_orders}
        for r in rows
    ]


def _preview(text_: str | None, kind: str | None) -> str:
    if kind == "audio":
        return "🎤 Message vocal"
    if kind == "image":
        return "🖼 Photo"
    if text_:
        return text_.replace("\n", " ")[:90]
    return {"audio": "🎤 Message vocal", "image": "🖼 Image"}.get(kind or "", "Pièce jointe")


def get_conversation(s: Session, conversation_id: UUID) -> dict | None:
    r = s.execute(
        text("""SELECT c.id, c.status, c.last_message_at, ct.name, ct.channel_user_id
                FROM conversations c JOIN contacts ct ON ct.id = c.contact_id AND ct.tenant_id = c.tenant_id
                WHERE c.id = :c"""), {"c": conversation_id},
    ).first()
    if r is None:
        return None
    return {"id": r.id, "status": r.status, "label": contact_label(r.name, r.channel_user_id)}


VOICE_PLACEHOLDER = "[Message vocal envoyé par l'équipe]"   # lu par l'agent si la conversation lui est rendue


def _message_view(r) -> dict:
    raw = r.raw or {}
    if r.direction == "in":
        author = "system" if raw.get("system") else "customer"
    else:
        author = "human" if raw.get("author") == "human" else "agent"
    audio = r.type == "audio" and bool(r.has_media)
    image = r.type == "image" and bool(r.has_media)
    if author == "system":
        body = "Notification de paiement reçue de FedaPay"
    elif image:
        body = r.text or ""                                         # légende du client, ou « [Photo envoyée : …] » côté agent
    elif audio:
        body = (r.text or "") if r.direction == "in" else ""        # transcription d'un vocal reçu ; rien pour un vocal envoyé
    elif r.type == "audio":
        body = f"🎤 Vocal transcrit : « {r.text} »" if r.text else "🎤 Message vocal (non transcrit)"
    elif r.text:
        body = r.text
    else:
        body = {"image": "🖼 Image", "video": "🎞 Vidéo", "file": "📎 Fichier"}.get(r.type, "📎 Pièce jointe")
    note = r.description if (image and r.direction == "in") else None   # ce que l'IA a compris de l'image reçue
    return {"id": str(r.id), "author": author, "text": body, "audio": audio, "image": image, "note": note,
            "at": r.created_at, "delivery": r.delivery_status}


def messages(s: Session, conversation_id: UUID, *, since: datetime | None = None, limit: int = 300) -> list[dict]:
    """Messages d'une conversation, du plus ancien au plus récent. `since` : relecture avec marge (le client dédoublonne)."""
    where = "AND m.created_at >= :since" if since else ""
    rows = s.execute(
        text(f"""SELECT m.id, m.direction, m.type, m.text, m.created_at, m.delivery_status, m.raw,
                   EXISTS (SELECT 1 FROM message_media mm WHERE mm.message_id = m.id) AS has_media,
                   (SELECT mm.description FROM message_media mm WHERE mm.message_id = m.id) AS description
                 FROM messages m WHERE m.conversation_id = :c {where}
                 ORDER BY m.created_at DESC, m.id DESC LIMIT :limit"""),
        {"c": conversation_id, "limit": limit, **({"since": since} if since else {})},
    ).all()[::-1]
    return [_message_view(r) for r in rows]


def set_conversation_status(s: Session, conversation_id: UUID, status: str) -> bool:
    if status not in ("agent", "human"):
        raise ValueError("statut inconnu")
    return s.execute(
        update(Conversation).where(Conversation.id == conversation_id, Conversation.status != "closed").values(status=status)
    ).rowcount > 0


def send_human_reply(tenant_id: UUID, conversation_id: UUID, body: str, sender_factory=None) -> dict:
    """Réponse d'un humain depuis la console : même circuit d'envoi que l'agent, et l'agent se met en pause.

    Le message est enregistré AVANT l'envoi (visible dans l'historique même si l'envoi échoue), puis envoyé hors transaction.
    Lève LookupError (conversation inconnue), ValueError (texte invalide) ou SendError (échec d'envoi).
    """
    from app.agent.orchestrator import _channel_info  # même résolution canal/jeton/destinataire que l'agent

    body = (body or "").strip()
    if not body or len(body) > MAX_REPLY:
        raise ValueError(f"Le message doit contenir entre 1 et {MAX_REPLY} caractères.")
    now = datetime.now().astimezone()
    with tenant_session(tenant_id) as s:
        if s.get(Conversation, conversation_id) is None:
            raise LookupError("conversation inconnue")
        channel_id, channel_type, token, recipient = _channel_info(s, conversation_id)
        message_id = s.execute(
            pg_insert(Message).values(
                tenant_id=tenant_id, conversation_id=conversation_id, channel_id=channel_id, direction="out", type="text",
                text=body, raw={"author": "human"}, idempotency_key=f"human:{uuid4()}", delivery_status="pending",
                received_at=now,
            ).returning(Message.id)
        ).scalar_one()
        s.execute(update(Conversation).where(Conversation.id == conversation_id).values(status="human", last_message_at=now))

    factory = sender_factory or get_sender   # résolu à l'appel (substituable dans les tests)
    try:
        external_id = factory(channel_type, token).send_text(recipient, body)
    except SendError:
        with tenant_session(tenant_id) as s:
            s.execute(update(Message).where(Message.id == message_id).values(delivery_status="failed"))
        raise
    with tenant_session(tenant_id) as s:
        s.execute(update(Message).where(Message.id == message_id).values(
            delivery_status="sent", external_id=external_id, sent_at=datetime.now().astimezone()))
    return {"id": str(message_id), "author": "human", "text": body, "at": now, "delivery": "sent"}


def send_human_voice(tenant_id: UUID, conversation_id: UUID, data: bytes, sender_factory=None) -> dict:
    """Vocal enregistré dans la console, envoyé au client comme pièce jointe audio (WAV 16 bits).

    Même circuit que le texte : enregistré (message + audio) AVANT l'envoi, puis envoyé hors transaction ;
    l'agent se met en pause. Lève MediaError (audio invalide), LookupError ou SendError.
    """
    from app.agent.orchestrator import _channel_info

    media_service.parse_wav(data)                                   # valide avant toute écriture
    now = datetime.now().astimezone()
    with tenant_session(tenant_id) as s:
        if s.get(Conversation, conversation_id) is None:
            raise LookupError("conversation inconnue")
        channel_id, channel_type, token, recipient = _channel_info(s, conversation_id)
        message_id = s.execute(
            pg_insert(Message).values(
                tenant_id=tenant_id, conversation_id=conversation_id, channel_id=channel_id, direction="out", type="audio",
                text=VOICE_PLACEHOLDER, raw={"author": "human"}, idempotency_key=f"human:{uuid4()}",
                delivery_status="pending", received_at=now,
            ).returning(Message.id)
        ).scalar_one()
        media_service.store(s, tenant_id, message_id, media_service.Audio(data, "audio/wav"))
        s.execute(update(Conversation).where(Conversation.id == conversation_id).values(status="human", last_message_at=now))

    factory = sender_factory or get_sender
    try:
        external_id = factory(channel_type, token).send_audio(recipient, data, "audio/wav")
    except SendError:
        with tenant_session(tenant_id) as s:
            s.execute(update(Message).where(Message.id == message_id).values(delivery_status="failed"))
        raise
    with tenant_session(tenant_id) as s:
        s.execute(update(Message).where(Message.id == message_id).values(
            delivery_status="sent", external_id=external_id, sent_at=datetime.now().astimezone()))
    return {"id": str(message_id), "author": "human", "text": "", "audio": True, "at": now, "delivery": "sent"}


def media_for_message(tenant_id: UUID, message_id: UUID):
    with tenant_session(tenant_id) as s:
        return media_service.load(s, message_id)


# --- commandes ---------------------------------------------------------------------------------------

def list_orders(s: Session, status: str | None = None, limit: int = 200) -> list[dict]:
    where = "WHERE o.status = :st" if status in ORDER_STATUSES else ""
    rows = s.execute(
        text(f"""SELECT o.id, o.reference, o.status, o.customer_name, o.customer_phone, o.delivery_zone, o.total, o.items,
                   o.created_at, o.conversation_id,
                   (SELECT p.status FROM payments p WHERE p.order_id = o.id ORDER BY p.created_at DESC LIMIT 1) AS pay_status
                 FROM orders o {where} ORDER BY o.created_at DESC LIMIT :limit"""),
        {"limit": limit, **({"st": status} if where else {})},
    ).all()
    return [dict(r._mapping) for r in rows]


def get_order(s: Session, order_id: UUID) -> dict | None:
    r = s.execute(
        text("""SELECT o.*, ct.name AS contact_name, ct.channel_user_id
                FROM orders o JOIN conversations c ON c.id = o.conversation_id AND c.tenant_id = o.tenant_id
                JOIN contacts ct ON ct.id = c.contact_id AND ct.tenant_id = c.tenant_id WHERE o.id = :o"""),
        {"o": order_id},
    ).first()
    if r is None:
        return None
    order = dict(r._mapping)
    order["contact_label"] = contact_label(order.pop("contact_name"), order.pop("channel_user_id"))
    order["payments"] = [dict(p._mapping) for p in s.execute(
        text("""SELECT status, amount, currency, provider, provider_transaction_id, created_at, paid_at
                FROM payments WHERE order_id = :o ORDER BY created_at"""), {"o": order_id}).all()]
    return order


def change_order_status(s: Session, order_id: UUID, new_status: str) -> bool:
    """Applique une transition permise ; retourne False si la commande est introuvable ou la transition interdite."""
    cur = s.execute(text("SELECT status FROM orders WHERE id = :o FOR UPDATE"), {"o": order_id}).scalar()
    if cur is None or new_status not in ORDER_TRANSITIONS.get(cur, set()):
        return False
    s.execute(text("UPDATE orders SET status = :st, updated_at = now() WHERE id = :o"), {"st": new_status, "o": order_id})
    return True


def tenant_name(s: Session, tenant_id: UUID) -> str:
    return s.execute(text("SELECT name FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar() or "Entreprise"
