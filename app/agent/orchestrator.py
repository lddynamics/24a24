"""Orchestrateur : traite un job (un message client) de bout en bout.

Étapes : charger le contexte -> boucle d'agent (Claude + outils) -> enregistrer la réponse -> l'envoyer.
Aucune transaction n'est ouverte pendant les appels réseau (Claude, Meta).
"""
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.llm import LLMClient
from app.agent.confirmation import payment_confirmation_text
from app.agent.guards import PAYMENT_NOT_CONFIRMED, claims_payment_received
from app.agent.links import finalize_reply, has_link_reference, mask_urls
from app.agent.prompt import build_system_prompt
from app.agent.text import clean_for_messenger
from app.agent.tools import ToolContext, enabled_tools, run_tool
from app.channels.sender import SendError, Sender, get_sender
from app.config import get_settings
from app.crypto import decrypt
from app.db import tenant_session
from app.models import AgentRun, Conversation, Message
from app.services.jobs import Job
from app.services.orders import LINK_REUSE_MINUTES, PAYMENT_SUCCEEDED
from app.services import catalog as catalog_service
from app.services import media as media_service
from app.services.media import HttpMediaFetcher, MediaError
from app.services.transcribe import Transcriber, TranscriptionError
from app.services.vision import Describer, VisionError

MediaFetcher = HttpMediaFetcher  # type indicatif : tout objet avec .fetch(url) -> Audio convient

log = logging.getLogger(__name__)

HISTORY_LIMIT = 30
FALLBACK_ESCALATION = "Je transmets votre demande à un membre de l'équipe, qui vous répondra dès que possible."
FALLBACK_PAYMENT_LINK = (
    "Je rencontre un souci pour vous transmettre votre lien de paiement. "
    "Je préviens un membre de l'équipe, qui vous l'enverra dès que possible."
)
FALLBACK_TECHNICAL = "Nous rencontrons un souci technique. Un membre de l'équipe va vous répondre dès que possible."

PLACEHOLDERS = {
    "image": "[Le client a envoyé une image que je ne peux pas encore voir]",
    "audio": "[Le client a envoyé un message vocal que je ne peux pas encore écouter]",
    "video": "[Le client a envoyé une vidéo que je ne peux pas encore voir]",
    "file": "[Le client a envoyé un fichier que je ne peux pas encore lire]",
    "other": "[Le client a envoyé un contenu que je ne peux pas traiter]",
}


@dataclass
class Loaded:
    skip: str | None = None
    resume_message_id: UUID | None = None   # réponse déjà enregistrée mais non envoyée
    resume_text: str | None = None
    tenant_name: str = ""
    sector: str | None = None
    config: dict | None = None
    channel_id: UUID | None = None
    channel_type: str = ""
    token: str = ""
    recipient_id: str = ""
    history: list[dict] | None = None
    covered_until: datetime | None = None


STICKER_TEXT = "[Le client a envoyé un autocollant (par exemple un pouce levé), pas une photo]"


def to_llm_messages(rows: list[tuple]) -> list[dict]:
    """(direction, type, texte[, description d'image]) -> messages du modèle. Fusionne les tours consécutifs du même rôle."""
    out: list[dict] = []
    for row in rows:
        direction, kind, body = row[0], row[1], row[2]
        description = row[3] if len(row) > 3 else None
        role = "user" if direction == "in" else "assistant"
        content = body or (PLACEHOLDERS.get(kind, PLACEHOLDERS["other"]) if direction == "in" else "")
        if direction == "out":
            content = mask_urls(content)  # le modèle ne voit jamais une vraie URL : il ne peut ni la déformer ni la recopier
        if direction == "in" and kind == "audio" and body:
            content = f"[Message vocal transcrit] {body}"  # le texte d'un vocal est sa transcription
        if direction == "in" and kind == "image" and description:
            content = f"[Image envoyée par le client — description automatique : {description}]"
            if body:
                content += f" Légende du client : {body}"
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] == "assistant":  # la conversation doit commencer par le client
        out.pop(0)
    return out


def _channel_info(s, conversation_id: UUID):
    row = s.execute(
        text(
            "SELECT ch.id, ch.type, ch.credentials_encrypted, ct.channel_user_id "
            "FROM conversations cv JOIN channels ch ON ch.id = cv.channel_id "
            "JOIN contacts ct ON ct.id = cv.contact_id WHERE cv.id = :c"
        ),
        {"c": conversation_id},
    ).one()
    return row.id, row.type, decrypt(row.credentials_encrypted or ""), row.channel_user_id


def _load(job: Job) -> Loaded:
    with tenant_session(job.tenant_id) as s:
        trigger = s.get(Message, job.message_id)
        conv = s.get(Conversation, job.conversation_id)
        if trigger is None or conv is None:
            return Loaded(skip="missing")

        # 1. Réponse déjà enregistrée pour ce message ? (reprise après un échec d'envoi)
        existing = s.execute(
            select(Message.id, Message.text, Message.delivery_status).where(
                Message.conversation_id == conv.id, Message.idempotency_key == f"reply:{job.message_id}"
            )
        ).first()
        base = Loaded()
        base.channel_id, base.channel_type, base.token, base.recipient_id = _channel_info(s, conv.id)
        if existing:
            if existing.delivery_status == "pending":
                base.resume_message_id, base.resume_text = existing.id, existing.text
                return base
            return Loaded(skip="already_answered")

        # 2. L'agent est-il toujours en charge ?
        if conv.status != "agent":
            return Loaded(skip="not_agent")

        tenant = s.execute(
            text("SELECT name, sector, config FROM tenants WHERE id = :t"), {"t": job.tenant_id}
        ).one()
        rows = s.execute(
            text(
                """SELECT m.direction, m.type, m.text, m.created_at, mm.description
                   FROM messages m LEFT JOIN message_media mm ON mm.message_id = m.id AND mm.tenant_id = m.tenant_id
                   WHERE m.conversation_id = :c ORDER BY m.created_at DESC, m.id DESC LIMIT :n"""
            ),
            {"c": conv.id, "n": HISTORY_LIMIT},
        ).all()[::-1]

    base.tenant_name, base.sector, base.config = tenant.name, tenant.sector, tenant.config or {}
    base.history = to_llm_messages([(r.direction, r.type, r.text, r.description) for r in rows])
    inbound_times = [r.created_at for r in rows if r.direction == "in"]
    base.covered_until = max(inbound_times) if inbound_times else trigger.created_at
    if not base.history or base.history[-1]["role"] != "user":
        return Loaded(skip="no_user_turn")
    return base


@dataclass
class AgentResult:
    text: str | None
    escalated: bool
    steps: int
    input_tokens: int
    output_tokens: int


def _agent_loop(llm: LLMClient, model: str, system: str, ctx: ToolContext, messages: list[dict], max_steps: int) -> AgentResult:
    tools = enabled_tools(ctx.config)
    schemas = [t.schema() for t in tools]
    steps = in_tok = out_tok = 0
    escalated = False
    for _ in range(max_steps):
        reply = llm.create(model=model, system=system, tools=schemas, messages=messages, max_tokens=1000)
        steps += 1
        in_tok += reply.input_tokens
        out_tok += reply.output_tokens
        if not reply.tool_calls:
            return AgentResult(clean_for_messenger(reply.text) or None, escalated, steps, in_tok, out_tok)
        messages.append({"role": "assistant", "content": reply.content_blocks})
        results = []
        for call in reply.tool_calls:
            res = run_tool(ctx, call.name, call.input)
            if call.name == "escalate_to_human" and res.ok:
                escalated = True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(res.output, ensure_ascii=False),
                    "is_error": not res.ok,
                }
            )
        messages.append({"role": "user", "content": results})
    return AgentResult(None, escalated, steps, in_tok, out_tok)  # limite d'étapes atteinte


def _insert_outbound(
    s, job: Job, loaded: Loaded, key: str, body: str, after: datetime | None = None, *,
    kind: str = "text", raw: dict | None = None, offset: int = 1,
) -> UUID | None:
    """`after` : la réponse est classée juste après le dernier message client qu'elle couvre.
    Ainsi un message arrivé PENDANT le traitement reste, dans l'historique, après cette réponse
    et obtient sa propre réponse ; les messages déjà pris en compte, eux, la précèdent.
    `offset` (en microsecondes après `after`) fixe l'ordre entre les photos et le texte d'une même réponse."""
    values = dict(
        tenant_id=job.tenant_id,
        conversation_id=job.conversation_id,
        channel_id=loaded.channel_id,
        direction="out",
        type=kind,
        text=body,
        idempotency_key=key,
        delivery_status="pending",
        received_at=datetime.now().astimezone(),
    )
    if raw is not None:
        values["raw"] = raw
    if after is not None:
        values["created_at"] = after + timedelta(microseconds=offset)
    return s.execute(
        pg_insert(Message)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Message.tenant_id, Message.channel_id, Message.idempotency_key])
        .returning(Message.id)
    ).scalar()


def _insert_product_images(s, job: Job, loaded: Loaded, images: list[dict], after: datetime | None) -> None:
    """Photos demandées par l'agent : enregistrées (message + image) dans la même transaction que la réponse."""
    for n, img in enumerate(images, start=1):
        data = catalog_service.get_product_image(s, UUID(img["image_id"]))
        if data is None:                      # photo supprimée entre-temps : on l'ignore, le texte part quand même
            log.warning("job %s : photo %s introuvable, ignorée", job.id, img["image_id"])
            continue
        message_id = _insert_outbound(
            s, job, loaded, f"reply-img:{job.message_id}:{n}", f"[Photo envoyée : {img['name']}]", after,
            kind="image", raw={"kind": "product_image", "sku": img["sku"]}, offset=n,
        )
        if message_id is not None:
            media_service.store(s, job.tenant_id, message_id, media_service.Media(data[0], data[1]))


def _deliver(job: Job, loaded: Loaded, message_id: UUID, body: str, sender: Sender) -> None:
    try:
        external_id = sender.send_text(loaded.recipient_id, body)
    except SendError as e:
        if e.permanent:
            with tenant_session(job.tenant_id) as s:
                s.execute(update(Message).where(Message.id == message_id).values(delivery_status="failed"))
        raise
    with tenant_session(job.tenant_id) as s:
        s.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(delivery_status="sent", external_id=external_id, sent_at=datetime.now().astimezone())
        )


def _deliver_image(job: Job, loaded: Loaded, message_id: UUID, sender: Sender) -> None:
    with tenant_session(job.tenant_id) as s:
        media = media_service.load(s, message_id)
    if media is None:
        with tenant_session(job.tenant_id) as s:
            s.execute(update(Message).where(Message.id == message_id).values(delivery_status="failed"))
        raise SendError("photo introuvable en base", permanent=True)
    try:
        external_id = sender.send_image(loaded.recipient_id, media.data, media.content_type)
    except SendError as e:
        if e.permanent:
            with tenant_session(job.tenant_id) as s:
                s.execute(update(Message).where(Message.id == message_id).values(delivery_status="failed"))
        raise
    with tenant_session(job.tenant_id) as s:
        s.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(delivery_status="sent", external_id=external_id, sent_at=datetime.now().astimezone())
        )


def _deliver_reply(job: Job, loaded: Loaded, message_id: UUID, body: str, sender: Sender) -> None:
    """Envoie les photos encore en attente de cette réponse (dans l'ordre), PUIS le texte. Sert aussi à la reprise :
    ce qui est déjà parti n'est jamais renvoyé."""
    with tenant_session(job.tenant_id) as s:
        pending = s.execute(
            text(
                """SELECT id FROM messages WHERE conversation_id = :c AND direction = 'out' AND type = 'image'
                   AND delivery_status = 'pending' AND idempotency_key LIKE :k ORDER BY created_at, id"""
            ),
            {"c": job.conversation_id, "k": f"reply-img:{job.message_id}:%"},
        ).scalars().all()
    for image_message_id in pending:
        _deliver_image(job, loaded, image_message_id, sender)
    _deliver(job, loaded, message_id, body, sender)


def _is_sticker(raw: dict | None) -> bool:
    """Messenger range les autocollants (dont le pouce levé) parmi les images ; ils portent un `sticker_id`."""
    try:
        return bool(((raw or {}).get("message") or {})["attachments"][0]["payload"].get("sticker_id"))
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def _prepare_image(job: Job, describer: Describer | None, fetcher: MediaFetcher | None) -> None:
    """Pour une image entrante : archive une copie normalisée (affichée dans la console) puis la fait décrire par le
    modèle de vision ; la description est enregistrée et présentée à l'agent à la place de l'image.

    Appels réseau HORS transaction. Un échec n'interrompt jamais le traitement : sans description, l'agent verra
    « image que je ne peux pas encore voir » et demandera poliment d'écrire."""
    with tenant_session(job.tenant_id) as s:
        m = s.get(Message, job.message_id)
        if m is None or m.direction != "in" or m.type != "image":
            return
        if _is_sticker(m.raw):
            if not m.text:
                s.execute(update(Message).where(Message.id == m.id).values(text=STICKER_TEXT))
            return
        if not m.media_ref or (describer is None and fetcher is None):
            return
        media_ref = m.media_ref
        media = media_service.load(s, job.message_id) if media_service.exists(s, job.message_id) else None
        described = bool(media_service.description(s, job.message_id)) if media is not None else False

    if media is None and fetcher is not None:
        try:
            media = fetcher.fetch_image(media_ref)
        except MediaError as e:
            log.warning("archivage de l'image impossible (job %s): %s", job.id, e)
        else:
            with tenant_session(job.tenant_id) as s:
                media_service.store(s, job.tenant_id, job.message_id, media)

    if describer is None or media is None or described:
        return
    try:
        description = describer.describe(media.data, media.content_type)
    except VisionError as e:
        log.warning("lecture de l'image impossible (job %s): %s", job.id, e)
        return
    if description:
        with tenant_session(job.tenant_id) as s:
            media_service.set_description(s, job.message_id, description)


def _prepare_audio(job: Job, transcriber: Transcriber | None, fetcher: MediaFetcher | None) -> None:
    """Pour un vocal entrant : archive l'audio (lecture dans la console) puis le transcrit pour l'agent.

    Appels réseau HORS transaction. Un échec n'interrompt jamais le traitement : sans transcription, l'agent verra
    un vocal non lisible et demandera poliment d'écrire ; sans archive, la console n'affichera que le texte.
    """
    if transcriber is None and fetcher is None:
        return
    with tenant_session(job.tenant_id) as s:
        m = s.get(Message, job.message_id)
        if m is None or m.direction != "in" or m.type != "audio" or not m.media_ref:
            return
        media_ref, has_text = m.media_ref, bool(m.text)
        audio = media_service.load(s, job.message_id) if media_service.exists(s, job.message_id) else None

    if audio is None and fetcher is not None:
        try:
            audio = fetcher.fetch(media_ref)
        except MediaError as e:
            log.warning("archivage du vocal impossible (job %s): %s", job.id, e)
        else:
            with tenant_session(job.tenant_id) as s:
                media_service.store(s, job.tenant_id, job.message_id, audio)

    if transcriber is None or has_text:
        return
    try:
        if audio is not None:
            transcript = transcriber.transcribe_bytes(audio.data, audio.content_type, media_ref)
        else:
            transcript = transcriber.transcribe_url(media_ref)
    except TranscriptionError as e:
        log.warning("transcription impossible (job %s): %s", job.id, e)
        return
    if transcript:
        with tenant_session(job.tenant_id) as s:
            s.execute(
                update(Message).where(Message.id == job.message_id, Message.text.is_(None)).values(text=transcript)
            )


def _has_paid_order(job: Job) -> bool:
    with tenant_session(job.tenant_id) as s:
        return s.execute(
            text("SELECT 1 FROM orders WHERE conversation_id = :c AND status = 'paid' LIMIT 1"), {"c": job.conversation_id}
        ).first() is not None


def _latest_pending_payment_url(job: Job) -> str | None:
    """Dernier lien de paiement encore valable de la conversation (filet de sécurité si le modèle réutilise un ancien lien)."""
    with tenant_session(job.tenant_id) as s:
        return s.execute(
            text(
                """SELECT p.payment_url FROM payments p JOIN orders o ON o.id = p.order_id
                   WHERE o.conversation_id = :c AND p.status = 'pending' AND p.payment_url IS NOT NULL
                     AND p.created_at > now() - make_interval(mins => :m)
                   ORDER BY p.created_at DESC LIMIT 1"""
            ),
            {"c": job.conversation_id, "m": LINK_REUSE_MINUTES},
        ).scalar()


def _payment_notification(job: Job) -> UUID | None:
    """Le job vient-il d'une notification de paiement confirmé (message système) plutôt que d'un message client ?"""
    with tenant_session(job.tenant_id) as s:
        m = s.get(Message, job.message_id)
        raw = (m.raw if m is not None else None) or {}
    if raw.get("system") and raw.get("kind") == PAYMENT_SUCCEEDED and raw.get("payment_id"):
        return UUID(str(raw["payment_id"]))
    return None


def _send_payment_confirmation(job: Job, payment_id: UUID, sender_factory: Callable[[str, str], Sender]) -> str:
    """Envoie au client la confirmation de paiement, écrite par le backend (aucun appel au modèle).

    Idempotent : un seul message par paiement (clé d'idempotence) ; si l'envoi a échoué, la reprise le renvoie."""
    key = f"payment_confirmed:{payment_id}"
    with tenant_session(job.tenant_id) as s:
        channel_id, ctype, token, recipient = _channel_info(s, job.conversation_id)
        loaded = Loaded(channel_id=channel_id, channel_type=ctype, token=token, recipient_id=recipient)
        existing = s.execute(
            select(Message.id, Message.text, Message.delivery_status).where(
                Message.conversation_id == job.conversation_id, Message.idempotency_key == key
            )
        ).first()
        if existing is not None:
            if existing.delivery_status != "pending":
                return "skipped:already_confirmed"
            message_id, body = existing.id, existing.text
        else:
            order = s.execute(
                text("""SELECT o.reference, o.customer_name, o.items, o.delivery_zone, o.delivery_address,
                               o.delivery_fee, o.total
                        FROM payments p JOIN orders o ON o.id = p.order_id AND o.tenant_id = p.tenant_id
                        WHERE p.id = :p"""),
                {"p": payment_id},
            ).mappings().one()
            tenant = s.execute(text("SELECT name, config FROM tenants WHERE id = :t"), {"t": job.tenant_id}).one()
            body = payment_confirmation_text(dict(order), tenant.config or {}, tenant.name)
            message_id = _insert_outbound(s, job, loaded, key, body)
    if message_id is None:
        return "skipped:already_confirmed"
    _deliver(job, loaded, message_id, body, sender_factory(ctype, token))
    return "payment_confirmed"


def handle_job(
    job: Job,
    *,
    llm: LLMClient,
    sender_factory: Callable[[str, str], Sender] = get_sender,
    transcriber: Transcriber | None = None,
    media_fetcher: MediaFetcher | None = None,
    describer: Describer | None = None,
) -> str:
    payment_id = _payment_notification(job)
    if payment_id is not None:      # paiement confirmé : message du backend, sans modèle, même si un humain a la main
        return _send_payment_confirmation(job, payment_id, sender_factory)
    _prepare_audio(job, transcriber, media_fetcher)
    _prepare_image(job, describer, media_fetcher)
    loaded = _load(job)
    if loaded.skip:
        return f"skipped:{loaded.skip}"

    # Reprise : la réponse existe déjà, il ne reste qu'à l'envoyer (pas de nouvel appel à Claude).
    if loaded.resume_message_id:
        _deliver_reply(job, loaded, loaded.resume_message_id, loaded.resume_text or "", sender_factory(loaded.channel_type, loaded.token))
        return "resumed"

    config = loaded.config or {}
    agent_cfg = config.get("agent") or {}
    model = agent_cfg.get("model") or get_settings().agent_model
    max_steps = int(agent_cfg.get("max_steps", 6))

    with tenant_session(job.tenant_id) as s:
        run = AgentRun(
            tenant_id=job.tenant_id,
            conversation_id=job.conversation_id,
            trigger_message_id=job.message_id,
            model=model,
        )
        s.add(run)
        s.flush()
        run_id = run.id

    ctx = ToolContext(tenant_id=job.tenant_id, conversation_id=job.conversation_id, run_id=run_id, config=config)
    system = build_system_prompt(loaded.tenant_name, loaded.sector, config)
    result = _agent_loop(llm, model, system, ctx, list(loaded.history or []), max_steps)

    # Aucune réponse exploitable ou trop d'étapes : on ne laisse pas le client sans réponse, on escalade.
    force_escalation = result.text is None
    body = result.text or FALLBACK_ESCALATION
    escalated = result.escalated or force_escalation

    # Garde-fou liens : la vraie URL vient du serveur (outil create_payment_link, ou dernier lien en attente en base),
    # jamais du modèle. Jeton -> vraie URL ; URL inventée/déformée -> supprimée ou remplacée ; lien généré mais absent -> ajouté.
    if result.text and (ctx.payment_url or has_link_reference(result.text)):
        trusted = ctx.payment_url or _latest_pending_payment_url(job)
        fixed = finalize_reply(result.text, trusted, allowed_domains=agent_cfg.get("allowed_link_domains") or ())
        if fixed.removed_urls:
            log.warning("job %s : URL du modèle neutralisées %s", job.id, fixed.removed_urls)
        if fixed.ok:
            result.text = body = fixed.text
        else:  # le modèle promet un lien que le serveur n'a pas : on ne laisse pas partir un message vide de sens
            log.warning("job %s : lien annoncé sans lien réel, escalade", job.id)
            result.text, body, force_escalation, escalated = None, FALLBACK_PAYMENT_LINK, True, True

    # Garde-fou : jamais d'annonce de paiement reçu si aucune commande de cette conversation n'est « payée » en base.
    if result.text and claims_payment_received(result.text) and not _has_paid_order(job):
        log.warning("job %s : annonce de paiement bloquée (aucune commande payée en base)", job.id)
        body = PAYMENT_NOT_CONFIRMED

    with tenant_session(job.tenant_id) as s:
        if force_escalation:
            s.execute(
                update(Conversation).where(Conversation.id == job.conversation_id, Conversation.status == "agent").values(status="human")
            )
        # Photos demandées : seulement si l'agent a bien produit un texte (jamais de photos seules avec un message de secours).
        images = ctx.pending_images if result.text else []
        _insert_product_images(s, job, loaded, images, loaded.covered_until)
        message_id = _insert_outbound(
            s, job, loaded, f"reply:{job.message_id}", body, after=loaded.covered_until, offset=len(images) + 1
        )
        s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                outcome="escalated" if escalated else "replied",
                steps=result.steps,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                covered_until=loaded.covered_until,
                finished_at=datetime.now().astimezone(),
            )
        )
    if message_id is None:
        return "skipped:duplicate_reply"

    _deliver_reply(job, loaded, message_id, body, sender_factory(loaded.channel_type, loaded.token))
    return "escalated" if escalated else "replied"


def on_terminal_failure(job: Job, *, sender_factory: Callable[[str, str], Sender] = get_sender) -> None:
    """Échec définitif : on passe la main à un humain et on prévient honnêtement le client (au mieux)."""
    try:
        with tenant_session(job.tenant_id) as s:
            s.execute(
                update(Conversation).where(Conversation.id == job.conversation_id, Conversation.status == "agent").values(status="human")
            )
            channel_id, ctype, token, recipient = _channel_info(s, job.conversation_id)
            loaded = Loaded(channel_id=channel_id, channel_type=ctype, token=token, recipient_id=recipient)
            message_id = _insert_outbound(s, job, loaded, f"fallback:{job.message_id}", FALLBACK_TECHNICAL)
        if message_id:
            _deliver(job, loaded, message_id, FALLBACK_TECHNICAL, sender_factory(ctype, token))
    except Exception:
        log.exception("impossible d'envoyer le message de secours (job %s)", job.id)
