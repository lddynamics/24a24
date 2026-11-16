import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from app.channels import evolution, messenger
from app.config import get_settings
from app.services import fedapay as fedapay_service
from app.services import orders as order_service
from app.services.ingest import ingest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks")


@router.get("/messenger")
def verify_subscription(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Appelé une fois par Meta quand on configure le webhook."""
    expected = get_settings().messenger_verify_token
    if mode == "subscribe" and hmac.compare_digest(token, expected):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/messenger")
async def receive(request: Request):
    raw = await request.body()
    # 1. Signature AVANT tout traitement
    if not messenger.verify_signature(
        get_settings().meta_app_secret, raw, request.headers.get("X-Hub-Signature-256")
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if payload.get("object") != "page":
        raise HTTPException(status_code=404, detail="not a page event")

    # 2. Normalisation + enregistrement idempotent. Une erreur => 500 => Meta renverra
    #    l'événement, et l'idempotence évitera les doublons.
    for msg in messenger.parse_payload(payload):
        result = await run_in_threadpool(ingest, msg)
        log.info("messenger %s -> %s", msg.idempotency_key, result.status)  # jamais le contenu

    # 3. Toujours 200 si tout s'est bien passé (même canal inconnu), pour éviter les renvois en boucle.
    return {"status": "ok"}


@router.post("/evolution")
async def receive_evolution(request: Request):
    """Webhook WhatsApp (non officiel) envoyé par Evolution API. Authentifié par un secret partagé (pas de signature Meta)."""
    if not evolution.verify_secret(get_settings().evolution_webhook_secret, request.headers.get(evolution.SECRET_HEADER)):
        raise HTTPException(status_code=403, detail="invalid secret")
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    for msg in evolution.parse_payload(payload):
        result = await run_in_threadpool(ingest, msg)
        log.info("whatsapp %s -> %s", msg.idempotency_key, result.status)  # jamais le contenu
    return {"status": "ok"}


@router.post("/fedapay")
async def receive_fedapay(request: Request):
    """Webhook de paiement FedaPay : signature -> événement -> revérification auprès de l'API -> mise à jour.

    Répond 2xx vite quand l'événement est traité ou sans objet ; 5xx si une erreur transitoire demande un renvoi.
    """
    raw = await request.body()
    secret = get_settings().fedapay_webhook_secret
    if not secret:
        log.error("FEDAPAY_WEBHOOK_SECRET non configuré : webhook refusé")
        raise HTTPException(status_code=503, detail="webhook not configured")
    header = request.headers.get("X-FEDAPAY-SIGNATURE")
    if not fedapay_service.verify_signature(secret, raw, header):
        # On ne journalise jamais le secret ; la forme de l'en-tête aide à diagnostiquer un format inattendu.
        log.warning("signature FedaPay invalide (en-tête présent: %s, longueur: %s)", header is not None, len(header or ""))
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    try:
        status = await run_in_threadpool(order_service.handle_fedapay_event, payload, fedapay_service.get_client())
    except fedapay_service.FedaPayError:
        log.exception("webhook FedaPay : revérification impossible")
        raise HTTPException(status_code=502, detail="verification failed")  # FedaPay réessaiera
    log.info("fedapay %s -> %s", payload.get("name"), status)
    return {"status": "ok"}
