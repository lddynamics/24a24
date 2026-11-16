"""Console d'entreprise : pages HTML servies par le backend (démo). Toute donnée passe par `tenant_session` (RLS)."""
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.channels.sender import SendError
from app.config import get_settings
from app.console import auth
from app.db import tenant_session
from app.services import catalog as catalog_service
from app.services import console as svc
from app.services import media as media_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/console")

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "templates" / "console"),
    autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True,
)

MAX_VOICE_BYTES = 8_000_000
MAX_PRODUCT_IMAGE_BYTES = 12_000_000
POST_LABELS = {"draft": "Brouillon", "scheduled": "Programmée", "publishing": "Publication en cours", "published": "Publiée",
               "failed": "Échec", "deleted": "Supprimée"}
ORDER_LABELS = {"draft": "Brouillon", "awaiting_payment": "En attente de paiement", "paid": "Payée", "delivered": "Livrée",
                "failed": "Paiement échoué", "cancelled": "Annulée"}
FLASH = {
    "saved": "Enregistré.", "created": "Produit créé.", "created_photos": "Produit créé. Ajoutez maintenant ses photos : l'agent pourra les envoyer aux clients.", "deleted": "Produit supprimé.", "imported": "Catalogue importé.",
    "nothing": "Rien à importer : tous les produits existent déjà.", "toggled": "Visibilité mise à jour.",
    "post_scheduled": "Publication programmée : le worker l'enverra à l'heure choisie.", "post_published": "Publiée sur la page Facebook.",
    "post_deleted": "Publication supprimée.",
    "order": "Commande mise à jour.", "order_refused": "Cette action n'est pas possible pour l'état actuel de la commande.",
}


class LoginRequired(Exception):
    pass


def install(app: FastAPI) -> None:
    from app.routers import console_posts  # import tardif : console_posts dépend des aides définies dans ce module

    app.include_router(router)
    app.include_router(console_posts.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/console/api/"):
            return JSONResponse({"error": "login_required"}, status_code=401)
        return RedirectResponse("/console/login", status_code=303)


def _fcfa(value) -> str:
    return f"{int(value or 0):,}".replace(",", "\u202f") + "\u00a0FCFA"


def _local(dt: datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    if dt is None:
        return "—"
    return dt.astimezone(svc.tz(get_settings().console_utc_offset_hours)).strftime(fmt)


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    secs = max(0, int((datetime.now(dt.tzinfo) - dt).total_seconds()))
    if secs < 60:
        return "à l'instant"
    if secs < 3600:
        return f"il y a {secs // 60} min"
    if secs < 86400:
        return f"il y a {secs // 3600} h"
    return _local(dt, "%d/%m")


_env.filters.update(post_label=lambda s: POST_LABELS.get(s, s), fcfa=_fcfa, local=_local, ago=_ago, order_label=lambda s: ORDER_LABELS.get(s, s),
                    initial=lambda s: (s or "?")[:1].upper())


def _render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    flash = FLASH.get(request.query_params.get("ok", ""))
    html = _env.get_template(name).render(flash=flash, **ctx)
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


def tenant_id_or_503() -> UUID:
    if not auth.enabled():
        raise HTTPException(503, "Console désactivée : définir CONSOLE_PASSWORD et CONSOLE_TENANT_ID dans .env")
    try:
        return UUID(get_settings().console_tenant_id)
    except ValueError:
        raise HTTPException(503, "CONSOLE_TENANT_ID invalide (UUID attendu)")


def session_tenant(request: Request) -> UUID:
    tid = tenant_id_or_503()
    if not auth.verify_token(request.cookies.get(auth.COOKIE)):
        raise LoginRequired()
    return tid


async def _form(request: Request) -> dict[str, list[str]]:
    body = await request.body()
    if len(body) > 200_000:
        raise HTTPException(413, "formulaire trop volumineux")
    return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


def _page(request: Request, tid: UUID, nav: str, **ctx) -> dict:
    with tenant_session(tid) as s:
        name = svc.tenant_name(s, tid)
    return {"nav": nav, "tenant_name": name, "compact": nav == "conversations", **ctx}


# --- connexion ---------------------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    tenant_id_or_503()
    return _render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request):
    tenant_id_or_503()
    key = request.client.host if request.client else "?"
    if auth.limiter.blocked(key):
        return _render(request, "login.html", 429, error="Trop de tentatives. Réessayez dans quelques minutes.")
    password = (await _form(request)).get("password", [""])[0]
    if not auth.check_password(password):
        auth.limiter.record_failure(key)
        return _render(request, "login.html", 401, error="Mot de passe incorrect.")
    auth.limiter.reset(key)
    resp = RedirectResponse("/console", status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(auth.COOKIE, auth.make_token(), httponly=True, samesite="lax", secure=secure, path="/console",
                    max_age=get_settings().console_session_hours * 3600)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/console/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/console")
    return resp


# --- tableau de bord ---------------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, tid: UUID = Depends(session_tenant)):
    off = get_settings().console_utc_offset_hours
    with tenant_session(tid) as s:
        data = svc.dashboard(s, off)
        attention = svc.list_conversations(s, limit=5, status="human")
        recent_orders = svc.list_orders(s, limit=6)
    return _render(request, "dashboard.html", **_page(request, tid, "dashboard", d=data, attention=attention, orders=recent_orders))


# --- conversations -----------------------------------------------------------------------------------

@router.get("/conversations", response_class=HTMLResponse)
def conversations(request: Request, list: int = 0, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        convs = svc.list_conversations(s)
    if convs and not list:
        return RedirectResponse(f"/console/conversations/{convs[0]['id']}", status_code=303)
    return _render(request, "conversations.html", **_page(request, tid, "conversations", convs=[], current=None, msgs=[]))


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation(conversation_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        current = svc.get_conversation(s, conversation_id)
        if current is None:
            raise HTTPException(404, "Conversation introuvable")
        convs = svc.list_conversations(s)
        msgs = [_decorate(m) for m in svc.messages(s, conversation_id)]
    return _render(request, "conversations.html", **_page(request, tid, "conversations", convs=convs, current=current, msgs=msgs))


@router.get("/fragments/conversation-list", response_class=HTMLResponse)
def conversation_list_fragment(request: Request, selected: UUID | None = None, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        convs = svc.list_conversations(s)
    html = _env.get_template("_conv_list.html").render(convs=convs, current={"id": selected} if selected else None)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def _day_label(dt: datetime) -> str:
    zone = svc.tz(get_settings().console_utc_offset_hours)
    local, today = dt.astimezone(zone), datetime.now(zone)
    if local.date() == today.date():
        return "Aujourd'hui"
    if local.date() == (today - timedelta(days=1)).date():
        return "Hier"
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}"


def _decorate(m: dict) -> dict:
    """Champs d'affichage communs au rendu serveur et au JSON (le navigateur construit le même DOM)."""
    zone = svc.tz(get_settings().console_utc_offset_hours)
    m["time"] = _local(m["at"], "%H:%M")
    m["day"] = m["at"].astimezone(zone).date().isoformat()
    m["day_label"] = _day_label(m["at"])
    m["audio_url"] = f"/console/media/{m['id']}" if m.get("audio") else None
    m["image_url"] = f"/console/media/{m['id']}" if m.get("image") else None
    return m


def _json_message(m: dict) -> dict:
    m = _decorate(dict(m))
    return {**m, "at": m["at"].isoformat()}


@router.get("/api/conversations/{conversation_id}/messages")
def api_messages(conversation_id: UUID, after: str | None = None, tid: UUID = Depends(session_tenant)):
    since = None
    if after:
        try:
            since = datetime.fromisoformat(after) - timedelta(seconds=2)   # marge : le client dédoublonne par id
        except ValueError:
            raise HTTPException(400, "paramètre 'after' invalide")
    with tenant_session(tid) as s:
        current = svc.get_conversation(s, conversation_id)
        if current is None:
            raise HTTPException(404, "Conversation introuvable")
        msgs = svc.messages(s, conversation_id, since=since)
    return JSONResponse({"status": current["status"], "messages": [_json_message(m) for m in msgs]},
                        headers={"Cache-Control": "no-store"})


@router.post("/api/conversations/{conversation_id}/status")
async def api_status(conversation_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    status = body.get("status") if isinstance(body, dict) else None
    if status not in ("agent", "human"):
        raise HTTPException(400, "statut invalide")
    with tenant_session(tid) as s:
        if not svc.set_conversation_status(s, conversation_id, status):
            raise HTTPException(404, "Conversation introuvable ou clôturée")
    return {"status": status}


@router.post("/api/conversations/{conversation_id}/reply")
async def api_reply(conversation_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    # JSON uniquement : un formulaire inter-sites ne peut pas envoyer ce type de contenu sans autorisation préalable (CORS).
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "application/json attendu")
    body = await request.json()
    try:
        msg = svc.send_human_reply(tid, conversation_id, str(body.get("text", "")) if isinstance(body, dict) else "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError:
        raise HTTPException(404, "Conversation introuvable")
    except SendError as e:
        log.warning("envoi humain échoué: %s", e)
        raise HTTPException(502, "Envoi impossible (fenêtre de 24 h dépassée ou jeton invalide ?). Le message est conservé.")
    return {"message": _json_message(msg), "status": "human"}


@router.post("/api/conversations/{conversation_id}/voice")
async def api_voice(conversation_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    """Vocal enregistré dans le navigateur (corps brut WAV). Type de contenu imposé : pas de formulaire inter-sites."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "audio/wav":
        raise HTTPException(415, "audio/wav attendu")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_VOICE_BYTES:
        raise HTTPException(413, "Enregistrement trop volumineux")
    body = await request.body()
    if len(body) > MAX_VOICE_BYTES:
        raise HTTPException(413, "Enregistrement trop volumineux")
    try:
        msg = svc.send_human_voice(tid, conversation_id, body)
    except media_service.MediaError as e:
        raise HTTPException(400, str(e))
    except LookupError:
        raise HTTPException(404, "Conversation introuvable")
    except SendError as e:
        log.warning("envoi du vocal échoué: %s", e)
        raise HTTPException(502, "Envoi du vocal impossible (fenêtre de 24 h dépassée ou jeton invalide ?). Le vocal est conservé.")
    return {"message": _json_message(msg), "status": "human"}


@router.get("/media/{message_id}")
def media(message_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    """Audio d'un message (reçu ou envoyé), servi seulement à une session authentifiée, avec prise en charge de Range."""
    audio = svc.media_for_message(tid, message_id)
    if audio is None:
        raise HTTPException(404, "Média introuvable")
    size = len(audio.data)
    is_image = audio.content_type.startswith("image/")
    headers = {
        "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox" if is_image else "default-src 'none'; sandbox",
        "Content-Disposition": f'inline; filename="{"photo" if is_image else "voice"}.{media_service.EXT.get(audio.content_type, "bin")}"',
    }
    rng = request.headers.get("range", "")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:                                            # « bytes=-N » : les N derniers octets
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        return Response(audio.data[start:end + 1], status_code=206, media_type=audio.content_type,
                        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(audio.data, media_type=audio.content_type, headers=headers)


# --- commandes ---------------------------------------------------------------------------------------

@router.get("/orders", response_class=HTMLResponse)
def orders(request: Request, status: str | None = None, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        rows = svc.list_orders(s, status)
    return _render(request, "orders.html", **_page(request, tid, "orders", orders=rows, status=status, statuses=svc.ORDER_STATUSES))


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(order_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        order = svc.get_order(s, order_id)
    if order is None:
        raise HTTPException(404, "Commande introuvable")
    actions = sorted(svc.ORDER_TRANSITIONS.get(order["status"], set()))
    return _render(request, "order_detail.html", **_page(request, tid, "orders", o=order, actions=actions, labels=ORDER_LABELS))


@router.post("/orders/{order_id}/status")
async def order_status(order_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    new = (await _form(request)).get("status", [""])[0]
    with tenant_session(tid) as s:
        ok = svc.change_order_status(s, order_id, new)
    return RedirectResponse(f"/console/orders/{order_id}?ok={'order' if ok else 'order_refused'}", status_code=303)


# --- catalogue ---------------------------------------------------------------------------------------

@router.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        products = catalog_service.list_products(s)
    return _render(request, "catalog.html", **_page(request, tid, "catalog", products=products, stock=catalog_service.stock_total))


def _blank_form() -> dict:
    return {"sku": "", "name": "", "category": "", "description": "", "price": "", "tags": "",
            "variants": [{"size": "", "color": "", "stock": ""}]}


def _form_from_fields(fields: dict[str, list[str]]) -> dict:
    one = lambda k: (fields.get(k) or [""])[0]  # noqa: E731
    sizes, colors, stocks = fields.get("variant_size", []), fields.get("variant_color", []), fields.get("variant_stock", [])
    n = max(len(sizes), len(colors), len(stocks), 1)
    pick = lambda lst, i: lst[i] if i < len(lst) else ""  # noqa: E731
    return {"sku": one("sku"), "name": one("name"), "category": one("category"), "description": one("description"),
            "price": one("price"), "tags": one("tags"),
            "variants": [{"size": pick(sizes, i), "color": pick(colors, i), "stock": pick(stocks, i)} for i in range(n)]}


def _form_from_product(p: dict) -> dict:
    return {"sku": p["id"], "name": p["name"], "category": p["category"], "description": p["description"],
            "price": str(p["price"]), "tags": ", ".join(p["tags"]),
            "variants": [{"size": v.get("size", ""), "color": v.get("color", ""), "stock": str(v.get("stock", 0))}
                         for v in p["variants"]] or [{"size": "", "color": "", "stock": ""}]}


@router.get("/catalog/new", response_class=HTMLResponse)
def product_new(request: Request, tid: UUID = Depends(session_tenant)):
    return _render(request, "product_form.html", **_page(request, tid, "catalog", f=_blank_form(), errors=[], editing=False))


@router.post("/catalog/new")
async def product_create(request: Request, tid: UUID = Depends(session_tenant)):
    fields = await _form(request)
    data, errors = catalog_service.parse_product_form(fields, creating=True)
    if not errors:
        try:
            with tenant_session(tid) as s:
                sku = catalog_service.create_product(s, tid, data)
            return RedirectResponse(f"/console/catalog/{sku}/edit?ok=created_photos", status_code=303)   # étape suivante : les photos
        except catalog_service.ProductError as e:
            errors = [str(e)]
    return _render(request, "product_form.html", 422, **_page(request, tid, "catalog", f=_form_from_fields(fields), errors=errors, editing=False))


@router.get("/catalog/{sku}/edit", response_class=HTMLResponse)
def product_edit(sku: str, request: Request, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        p = catalog_service.get_product(s, sku)
    if p is None:
        raise HTTPException(404, "Produit introuvable")
    with tenant_session(tid) as s:
        photos = catalog_service.list_product_images(s, sku)
    return _render(request, "product_form.html", **_page(request, tid, "catalog", f=_form_from_product(p), errors=[], editing=True,
                                                          photos=photos, max_photos=catalog_service.MAX_PRODUCT_IMAGES))


@router.post("/catalog/{sku}/edit")
async def product_update(sku: str, request: Request, tid: UUID = Depends(session_tenant)):
    fields = await _form(request)
    data, errors = catalog_service.parse_product_form(fields, creating=False)
    if not errors:
        with tenant_session(tid) as s:
            found = catalog_service.update_product(s, sku, data)
        if not found:
            raise HTTPException(404, "Produit introuvable")
        return RedirectResponse("/console/catalog?ok=saved", status_code=303)
    form = _form_from_fields(fields)
    form["sku"] = sku
    with tenant_session(tid) as s:
        photos = catalog_service.list_product_images(s, sku)
    return _render(request, "product_form.html", 422, **_page(request, tid, "catalog", f=form, errors=errors, editing=True,
                                                               photos=photos, max_photos=catalog_service.MAX_PRODUCT_IMAGES))


@router.post("/catalog/{sku}/delete")
def product_delete(sku: str, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        catalog_service.delete_product(s, sku)
    return RedirectResponse("/console/catalog?ok=deleted", status_code=303)


@router.post("/catalog/{sku}/toggle")
def product_toggle(sku: str, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        p = catalog_service.get_product(s, sku)
        if p is None:
            raise HTTPException(404, "Produit introuvable")
        catalog_service.set_active(s, sku, not p["active"])
    return RedirectResponse("/console/catalog?ok=toggled", status_code=303)


@router.post("/catalog/import")
def catalog_import(tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        config = s.execute(text("SELECT config FROM tenants WHERE id = :t"), {"t": tid}).scalar() or {}
        added = catalog_service.import_from_config(s, tid, config)
    return RedirectResponse(f"/console/catalog?ok={'imported' if added else 'nothing'}", status_code=303)


# --- photos des produits -----------------------------------------------------------------------------

@router.get("/catalog/images/{image_id}")
def product_image(image_id: UUID, tid: UUID = Depends(session_tenant)):
    """Photo d'un produit, servie seulement à une session authentifiée (toujours un JPEG produit par le serveur)."""
    with tenant_session(tid) as s:
        img = catalog_service.get_product_image(s, image_id)
    if img is None:
        raise HTTPException(404, "Photo introuvable")
    return Response(img[0], media_type=img[1], headers={
        "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
        "Content-Disposition": 'inline; filename="photo.jpg"',
    })


@router.post("/api/catalog/{sku}/images")
async def api_product_image_add(sku: str, request: Request, tid: UUID = Depends(session_tenant)):
    """Photo envoyée en corps brut (image/*). Type de contenu imposé : pas de formulaire inter-sites."""
    if not request.headers.get("content-type", "").split(";")[0].strip().startswith("image/"):
        raise HTTPException(415, "image/* attendu")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_PRODUCT_IMAGE_BYTES:
        raise HTTPException(413, "Photo trop volumineuse (12 Mo maximum)")
    body = await request.body()
    if len(body) > MAX_PRODUCT_IMAGE_BYTES:
        raise HTTPException(413, "Photo trop volumineuse (12 Mo maximum)")
    try:
        with tenant_session(tid) as s:
            image_id = catalog_service.add_product_image(s, tid, sku, body)
    except catalog_service.ProductError as e:
        raise HTTPException(404 if "introuvable" in str(e) else 400, str(e))
    return {"id": image_id, "url": f"/console/catalog/images/{image_id}"}


@router.post("/api/catalog/{sku}/images/{image_id}/delete")
def api_product_image_delete(sku: str, image_id: UUID, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        if not catalog_service.delete_product_image(s, sku, image_id):
            raise HTTPException(404, "Photo introuvable")
    return {"deleted": str(image_id)}


@router.post("/api/catalog/{sku}/images/{image_id}/cover")
def api_product_image_cover(sku: str, image_id: UUID, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        if not catalog_service.set_cover_image(s, sku, image_id):
            raise HTTPException(404, "Photo introuvable")
    return {"cover": str(image_id)}
