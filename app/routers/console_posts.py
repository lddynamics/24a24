"""Console : publications sur la page Facebook (liste, calendrier éditorial, éditeur, rédaction IA)."""
import calendar as pycal
import logging
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text

from app.agent.llm import LLMError
from app.config import get_settings
from app.db import tenant_session
from app.routers.console import _page, _render, session_tenant
from app.services import catalog as catalog_service
from app.services import console as svc
from app.services import facebook as fb
from app.services import social
from app.services import social_ai

log = logging.getLogger(__name__)
router = APIRouter(prefix="/console")

MAX_IMAGE_REQUEST = 8_500_000
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


async def _json(request: Request) -> dict:
    # JSON obligatoire : un formulaire inter-sites ne peut pas émettre ce type de contenu sans autorisation préalable (CORS).
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "application/json attendu")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON invalide")
    if not isinstance(body, dict):
        raise HTTPException(400, "objet JSON attendu")
    return body


def _offset() -> int:
    return get_settings().console_utc_offset_hours


def _local_dt(dt: datetime | None) -> str | None:
    return dt.astimezone(svc.tz(_offset())).strftime("%Y-%m-%dT%H:%M") if dt else None


def _public(post: dict) -> dict:
    return {
        "id": str(post["id"]), "status": post["status"], "label": post["label"], "message": post["message"],
        "source": post["source"], "product_sku": post["product_sku"], "has_image": post["has_image"],
        "scheduled_local": _local_dt(post["scheduled_for"]), "permalink": post["permalink"], "error": post["error"],
        "reactions": post["reactions"], "comments": post["comments"], "shares": post["shares"],
    }


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


# --- pages ---------------------------------------------------------------------------------------------------

def _month_grid(posts: list[dict], year: int, month: int) -> list[list[dict]]:
    zone = svc.tz(_offset())
    by_day: dict[date, list[dict]] = {}
    for p in posts:
        by_day.setdefault(p["day_at"].astimezone(zone).date(), []).append(p)
    today = datetime.now(zone).date()
    weeks = []
    for week in pycal.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append([{"date": d, "in_month": d.month == month, "today": d == today, "posts": by_day.get(d, [])} for d in week])
    return weeks


@router.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request, view: str = "list", status: str | None = None, month: str | None = None,
               tid: UUID = Depends(session_tenant)):
    zone = svc.tz(_offset())
    ctx: dict = {"view": "calendar" if view == "calendar" else "list", "status": status, "labels": social.STATUS_LABELS}
    with tenant_session(tid) as s:
        if ctx["view"] == "calendar":
            try:
                year, mon = (int(x) for x in (month or datetime.now(zone).strftime("%Y-%m")).split("-"))
                assert 2000 <= year <= 2100 and 1 <= mon <= 12
            except (ValueError, AssertionError):
                raise HTTPException(400, "mois invalide")
            start = datetime(year, mon, 1, tzinfo=zone).astimezone(timezone.utc) - timedelta(days=7)
            end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=zone).astimezone(timezone.utc) + timedelta(days=7)
            posts = social.posts_between(s, start, end)
            prev_y, prev_m = (year, mon - 1) if mon > 1 else (year - 1, 12)
            next_y, next_m = (year, mon + 1) if mon < 12 else (year + 1, 1)
            ctx.update(weeks=_month_grid(posts, year, mon), title=f"{MONTHS[mon - 1].capitalize()} {year}",
                       prev=f"{prev_y:04d}-{prev_m:02d}", next=f"{next_y:04d}-{next_m:02d}")
        else:
            ctx["posts"] = social.list_posts(s, status)
            ctx["counts"] = {r.status: r.n for r in s.execute(text("SELECT status, count(*) AS n FROM social_posts GROUP BY status")).all()}
    return _render(request, "posts.html", **_page(request, tid, "posts", **ctx))


def _editor_context(s, post: dict | None) -> dict:
    products = [p for p in catalog_service.list_products(s) if p["active"]]
    return {"products": products}


@router.get("/posts/new", response_class=HTMLResponse)
def post_new(request: Request, date_: str | None = None, tid: UUID = Depends(session_tenant)):
    prefill = None
    raw = request.query_params.get("date")
    if raw:
        try:
            prefill = f"{date.fromisoformat(raw).isoformat()}T09:00"
        except ValueError:
            prefill = None
    with tenant_session(tid) as s:
        ctx = _editor_context(s, None)
    return _render(request, "post_form.html", **_page(request, tid, "posts", post=None, prefill=prefill, readonly=False, **ctx))


@router.get("/posts/{post_id}", response_class=HTMLResponse)
def post_page(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        post = social.get_post(s, post_id)
        if post is None:
            raise HTTPException(404, "Publication introuvable")
        ctx = _editor_context(s, post)
    readonly = post["status"] not in social.EDITABLE
    return _render(request, "post_form.html", **_page(request, tid, "posts", post=post, prefill=None, readonly=readonly, **ctx))


@router.get("/post-media/{post_id}")
def post_media(post_id: UUID, tid: UUID = Depends(session_tenant)):
    with tenant_session(tid) as s:
        img = social.get_image(s, post_id)
    if img is None:
        raise HTTPException(404, "Image introuvable")
    return Response(img[0], media_type=img[1], headers={
        "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


# --- API ----------------------------------------------------------------------------------------------------------

@router.post("/api/posts")
async def api_create(request: Request, tid: UUID = Depends(session_tenant)):
    body = await _json(request)
    try:
        with tenant_session(tid) as s:
            pid = social.create_post(s, tid, message=str(body.get("message", "")), source=str(body.get("source", "human")),
                                     product_sku=body.get("product_sku") or None)
    except social.PostError as e:
        raise _bad(e)
    return {"id": str(pid)}


@router.put("/api/posts/{post_id}")
async def api_update(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    body = await _json(request)
    try:
        with tenant_session(tid) as s:
            social.update_post(s, post_id, message=str(body.get("message", "")), product_sku=body.get("product_sku") or None,
                               source=body.get("source"))
            post = social.get_post(s, post_id)
    except social.PostError as e:
        raise _bad(e)
    return _public(post)


@router.post("/api/posts/{post_id}/image")
async def api_image(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    if not request.headers.get("content-type", "").split(";")[0].strip().startswith("image/"):
        raise HTTPException(415, "image/* attendu")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_IMAGE_REQUEST:
        raise HTTPException(413, "Image trop volumineuse (8 Mo maximum)")
    data = await request.body()
    try:
        with tenant_session(tid) as s:
            mime = social.set_image(s, tid, post_id, data)
    except social.PostError as e:
        raise _bad(e)
    return {"content_type": mime}


@router.post("/api/posts/{post_id}/image/remove")
async def api_image_remove(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    await _json(request)
    with tenant_session(tid) as s:
        social.remove_image(s, post_id)
    return {"ok": True}


@router.post("/api/posts/{post_id}/schedule")
async def api_schedule(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    body = await _json(request)
    try:
        when = social.parse_local(str(body.get("when_local", "")), _offset())
        with tenant_session(tid) as s:
            social.schedule(s, post_id, when)
            post = social.get_post(s, post_id)
    except social.PostError as e:
        raise _bad(e)
    return _public(post)


@router.post("/api/posts/{post_id}/unschedule")
async def api_unschedule(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    await _json(request)
    try:
        with tenant_session(tid) as s:
            social.unschedule(s, post_id)
            post = social.get_post(s, post_id)
    except social.PostError as e:
        raise _bad(e)
    return _public(post)


@router.post("/api/posts/{post_id}/publish")
async def api_publish(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    await _json(request)
    try:
        out = await run_in_threadpool(social.publish, tid, post_id)
    except social.PostError as e:
        raise _bad(e)
    with tenant_session(tid) as s:
        post = social.get_post(s, post_id)
    return {**out, "post": _public(post)}


@router.post("/api/posts/{post_id}/delete")
async def api_delete(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    await _json(request)
    try:
        with tenant_session(tid) as s:
            post = social.get_post(s, post_id)
            if post is None:
                raise HTTPException(404, "Publication introuvable")
            if post["status"] != "published":
                social.delete_draft(s, post_id)
                return {"deleted": True, "from_facebook": False}
        await run_in_threadpool(social.delete_from_facebook, tid, post_id)
    except social.PostError as e:
        raise _bad(e)
    except fb.FacebookError as e:
        raise HTTPException(502, e.user_message)
    return {"deleted": True, "from_facebook": True}


@router.post("/api/posts/{post_id}/stats")
async def api_stats(post_id: UUID, request: Request, tid: UUID = Depends(session_tenant)):
    await _json(request)
    try:
        return await run_in_threadpool(social.refresh_stats, tid, post_id)
    except social.PostError as e:
        raise _bad(e)
    except fb.FacebookError as e:
        raise HTTPException(502, e.user_message)


@router.get("/api/social/connection")
async def api_connection(tid: UUID = Depends(session_tenant)):
    try:
        info = await run_in_threadpool(social.test_connection, tid)
    except social.PostError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except fb.FacebookError as e:
        return JSONResponse({"ok": False, "error": e.user_message})
    return {"ok": True, **info}


@router.post("/api/social/generate")
async def api_generate(request: Request, tid: UUID = Depends(session_tenant)):
    body = await _json(request)
    sku = body.get("product_sku") or None
    with tenant_session(tid) as s:
        name = svc.tenant_name(s, tid)
        config = s.execute(text("SELECT config FROM tenants WHERE id = :t"), {"t": tid}).scalar() or {}
        products = catalog_service.list_products(s)
    product = next((p for p in products if p["id"] == sku), None) if sku else None
    if sku and product is None:
        raise HTTPException(404, "Produit introuvable")
    zones = (((config.get("business_info") or {}).get("livraison") or {}).get("zones") or {}).values()
    allowed = {p["price"] for p in products} | {int(z["frais_fcfa"]) for z in zones}
    try:
        return await run_in_threadpool(
            lambda: social_ai.generate_post(tenant_name=name, config=config, brief=str(body.get("brief", "")),
                                            product=product, catalog_prices=allowed))
    except ValueError as e:
        raise _bad(e)
    except LLMError as e:
        log.warning("rédaction IA impossible: %s", e)
        raise HTTPException(502, "Le modèle IA n'a pas répondu. Réessayez dans un instant.")
