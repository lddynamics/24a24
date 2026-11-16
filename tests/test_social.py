"""Publications Facebook : client Graph, cycle de vie, planificateur, rédaction IA, console."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent.llm import LLMError
from app.config import get_settings
from app.console import auth
from app.crypto import decrypt
from app.db import tenant_session
from app.main import app
from app.services import catalog as catalog_service
from app.services import facebook as fb
from app.services import social, social_ai
from scripts.set_page_token import set_page_token
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeLLM, text_reply
from tests.seed import seed_catalog
from tests.test_agent import _rows

KORA = json.loads(Path("tenants/maison-kora.json").read_text(encoding="utf-8"))
PASSWORD = "mot-de-passe-test"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
FUTURE = datetime.now(timezone.utc) + timedelta(days=2)


class FakeFB:
    """Faux client Facebook : mémorise les appels, peut échouer à la demande."""

    def __init__(self):
        self.calls, self.fail, self.counter = [], None, 0

    def factory(self, page_id, token):
        self.calls.append(("factory", page_id, token))
        return self

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    def _res(self):
        self.counter += 1
        pid = f"P1_{1000 + self.counter}"
        return fb.PostResult(pid, f"https://www.facebook.com/{pid}")

    def publish_text(self, message, link=None):
        self._maybe_fail()
        self.calls.append(("text", message))
        return self._res()

    def publish_photo(self, data, content_type, caption):
        self._maybe_fail()
        self.calls.append(("photo", caption, content_type, len(data)))
        return self._res()

    def get_stats(self, post_id):
        self._maybe_fail()
        return {"reactions": 12, "comments": 3, "shares": 1}

    def delete_post(self, post_id):
        self._maybe_fail()
        self.calls.append(("delete", post_id))

    def whoami(self):
        self._maybe_fail()
        return {"id": "P1", "name": "Maison Kora"}


@pytest.fixture
def tenant(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)
    seed_catalog(tenant_id, KORA)
    return tenant_id


@pytest.fixture
def fake(monkeypatch):
    f = FakeFB()
    monkeypatch.setattr(social, "default_client_factory", f.factory)
    return f


def _new(tenant, message="Nouvelle robe disponible", **kw):
    with tenant_session(tenant) as s:
        return social.create_post(s, tenant, message=message, **kw)


def _status(tenant, pid):
    return _rows(tenant, f"SELECT status, attempts, error, fb_post_id, permalink FROM social_posts WHERE id = '{pid}'")[0]


# --- erreurs et client Graph -----------------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,kind,permanent", [
    (400, {"error": {"code": 190, "message": "Error validating access token"}}, "token", True),
    (403, {"error": {"code": 200, "message": "(#200) Requires pages_manage_posts permission"}}, "permission", True),
    (400, {"error": {"code": 10, "message": "Application does not have permission"}}, "permission", True),
    (400, {"error": {"code": 4, "message": "Application request limit reached"}}, "rate", False),
    (400, {"error": {"code": 368, "message": "blocked"}}, "blocked", True),
    (400, {"error": {"code": 506, "message": "Duplicate status message"}}, "duplicate", True),
    (500, {"error": {"code": 2, "message": "Service temporarily unavailable"}}, "api", False),
    (502, None, "api", False),
    (400, {"error": {"code": 100, "message": "Invalid parameter"}}, "api", True),
])
def test_facebook_errors_are_classified(status, body, kind, permanent):
    e = fb.classify(status, body)
    assert (e.kind, e.permanent) == (kind, permanent)
    assert e.user_message and "Traceback" not in e.user_message


def test_user_messages_tell_what_to_do():
    assert "pages_manage_posts" in fb.classify(403, {"error": {"code": 200, "message": "x"}}).user_message
    assert "set_page_token" in fb.classify(400, {"error": {"code": 190, "message": "x"}}).user_message
    assert "identique" in fb.classify(400, {"error": {"code": 506, "message": "x"}}).user_message


def _client(handler):
    return fb.FacebookPageClient("PAGE1", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_publish_text_request_shape_and_permalink():
    seen = {}

    def handler(req):
        seen.update(method=req.method, url=str(req.url), auth=req.headers["authorization"], body=req.read().decode())
        return httpx.Response(200, json={"id": "PAGE1_999"})

    out = _client(handler).publish_text("Bonjour à tous", link="https://exemple.test")
    assert out.post_id == "PAGE1_999" and out.permalink == "https://www.facebook.com/PAGE1_999"
    assert seen["method"] == "POST" and seen["url"].endswith("/PAGE1/feed") and seen["auth"] == "Bearer TOKEN"
    assert "TOKEN" not in seen["url"] and "message=Bonjour" in seen["body"] and "link=https" in seen["body"]


def test_publish_photo_uploads_file_and_uses_post_id():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), ctype=req.headers["content-type"], body=req.read())
        return httpx.Response(200, json={"id": "PHOTO7", "post_id": "PAGE1_555"})

    out = _client(handler).publish_photo(PNG, "image/png", "Ma légende")
    assert out.post_id == "PAGE1_555"                                  # on garde l'identifiant de la PUBLICATION, pas de la photo
    assert seen["url"].endswith("/PAGE1/photos") and seen["ctype"].startswith("multipart/form-data")
    assert b'name="source"' in seen["body"] and b'filename="photo.png"' in seen["body"] and PNG in seen["body"]
    assert b'name="caption"' in seen["body"] and "Ma légende".encode() in seen["body"]


def test_graph_errors_become_typed_exceptions():
    def handler(req):
        return httpx.Response(400, json={"error": {"code": 200, "message": "(#200) permission denied"}})

    with pytest.raises(fb.FacebookError) as e:
        _client(handler).publish_text("x")
    assert e.value.kind == "permission" and e.value.permanent
    with pytest.raises(fb.FacebookError) as e2:     # 200 avec un corps d'erreur est aussi une erreur
        _client(lambda r: httpx.Response(200, json={"error": {"code": 190, "message": "x"}})).whoami()
    assert e2.value.kind == "token"


def test_network_failure_is_transient():
    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(fb.FacebookError) as e:
        _client(handler).publish_text("x")
    assert e.value.kind == "network" and not e.value.permanent


def test_stats_delete_and_whoami():
    def handler(req):
        if req.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "PAGE1", "name": "Maison Kora"})
        return httpx.Response(200, json={"reactions": {"summary": {"total_count": 12}}, "comments": {"summary": {"total_count": 3}}, "shares": {"count": 2}})

    c = _client(handler)
    assert c.get_stats("PAGE1_9") == {"reactions": 12, "comments": 3, "shares": 2}
    assert c.whoami() == {"id": "PAGE1", "name": "Maison Kora"}
    c.delete_post("PAGE1_9")


def test_stats_default_to_zero_when_facebook_omits_fields():
    assert _client(lambda r: httpx.Response(200, json={})).get_stats("x") == {"reactions": 0, "comments": 0, "shares": 0}


# --- validation ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data,mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (b"GIF89a" + b"\0" * 20, "image/gif"),
                                       (b"<html>pas une image</html>", None), (b"RIFF\0\0\0\0WEBPVP8 ", None), (b"", None)])
def test_images_are_recognised_by_content(data, mime):
    assert social.sniff_image(data) == mime


def test_message_and_schedule_validation():
    assert social.clean_message("  bonjour\r\nà tous  ") == "bonjour\nà tous"
    with pytest.raises(social.PostError):
        social.clean_message("x" * 3001)
    now = datetime.now(timezone.utc)
    social.check_schedule(now + timedelta(hours=1))
    for bad in (now - timedelta(minutes=5), now + timedelta(seconds=20), now + timedelta(days=400)):
        with pytest.raises(social.PostError):
            social.check_schedule(bad)


def test_local_time_is_converted_to_utc():
    assert social.parse_local("2030-01-01T09:00", 1) == datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(social.PostError):
        social.parse_local("demain", 1)


# --- cycle de vie ------------------------------------------------------------------------------------------------

def test_publish_text_marks_published_with_permalink(tenant, fake):
    pid = _new(tenant)
    out = social.publish(tenant, pid, fake.factory)
    assert out == {"status": "published", "permalink": "https://www.facebook.com/P1_1001"}
    st = _status(tenant, pid)
    assert (st.status, st.attempts, st.fb_post_id, st.error) == ("published", 1, "P1_1001", None)
    assert ("text", "Nouvelle robe disponible") in fake.calls
    assert ("factory", "P1", "T") in [c for c in fake.calls if c[0] == "factory"] or fake.calls[0][0] == "factory"


def test_publish_with_image_uses_photo_endpoint(tenant, fake):
    pid = _new(tenant)
    with tenant_session(tenant) as s:
        social.set_image(s, tenant, pid, PNG)
    social.publish(tenant, pid, fake.factory)
    assert ("photo", "Nouvelle robe disponible", "image/png", len(PNG)) in fake.calls


def test_empty_post_cannot_be_published_or_scheduled(tenant, fake):
    pid = _new(tenant, message="")
    with pytest.raises(social.PostError, match="texte ou une image"):
        social.publish(tenant, pid, fake.factory)
    assert _status(tenant, pid).status == "draft"                       # libérée, pas bloquée en « publishing »
    with tenant_session(tenant) as s, pytest.raises(social.PostError):
        social.schedule(s, pid, FUTURE)


def test_permanent_failure_is_recorded_with_a_readable_reason(tenant, fake):
    pid = _new(tenant)
    fake.fail = fb.classify(403, {"error": {"code": 200, "message": "(#200) permissions"}})
    out = social.publish(tenant, pid, fake.factory)
    assert out["status"] == "failed" and "pages_manage_posts" in out["error"]
    st = _status(tenant, pid)
    assert st.status == "failed" and "pages_manage_posts" in st.error
    fake.fail = None
    assert social.publish(tenant, pid, fake.factory)["status"] == "published"      # « Réessayer » fonctionne


def test_transient_failures_are_retried_then_given_up(tenant, fake):
    pid = _new(tenant)
    fake.fail = fb.FacebookError("réseau", kind="network", permanent=False)
    first = social.publish(tenant, pid, fake.factory, retry_transient=True)
    assert first["status"] == "scheduled" and first["retry_in_minutes"] == 1
    st = _rows(tenant, f"SELECT status, attempts, next_attempt_at > now() AS later FROM social_posts WHERE id = '{pid}'")[0]
    assert (st.status, st.attempts, st.later) == ("scheduled", 1, True)
    for _ in range(2):
        with tenant_session(tenant) as s:
            s.execute(text("UPDATE social_posts SET next_attempt_at = now() - interval '1 second', scheduled_for = now() - interval '1 hour'"))
        out = social.publish(tenant, pid, fake.factory, retry_transient=True)
    assert out["status"] == "failed" and _status(tenant, pid).attempts == 3


def test_console_click_fails_immediately_without_silent_retries(tenant, fake):
    pid = _new(tenant)
    fake.fail = fb.FacebookError("réseau", kind="network", permanent=False)
    assert social.publish(tenant, pid, fake.factory)["status"] == "failed"


def test_publishing_is_claimed_atomically(tenant, fake):
    pid = _new(tenant)
    with tenant_session(tenant) as s:
        s.execute(text("UPDATE social_posts SET status = 'publishing'"))          # un autre worker l'a déjà réclamée
    with pytest.raises(social.PostError, match="publiable"):
        social.publish(tenant, pid, fake.factory)
    assert [c for c in fake.calls if c[0] in ("text", "photo")] == []
    with tenant_session(tenant) as s:                                          # worker disparu depuis plus de 10 minutes : reprise
        s.execute(text("UPDATE social_posts SET updated_at = now() - interval '11 minutes'"))
    assert social.publish(tenant, pid, fake.factory)["status"] == "published"


def test_published_posts_are_frozen(tenant, fake):
    pid = _new(tenant)
    social.publish(tenant, pid, fake.factory)
    with tenant_session(tenant) as s:
        with pytest.raises(social.PostError):
            social.update_post(s, pid, message="modifié")
        with pytest.raises(social.PostError):
            social.delete_draft(s, pid)
        with pytest.raises(social.PostError):
            social.set_image(s, tenant, pid, PNG)
    with pytest.raises(social.PostError):
        social.publish(tenant, pid, fake.factory)                              # jamais de double publication


def test_schedule_unschedule_and_schedule_table(tenant):
    pid = _new(tenant)
    with tenant_session(tenant) as s:
        social.schedule(s, pid, FUTURE)
    assert len(_rows(tenant, f"SELECT 1 FROM social_schedule WHERE post_id = '{pid}'")) == 1
    with tenant_session(tenant) as s:
        social.unschedule(s, pid)
        with pytest.raises(social.PostError):
            social.unschedule(s, pid)
    assert _status(tenant, pid).status == "draft"
    assert _rows(tenant, f"SELECT 1 FROM social_schedule WHERE post_id = '{pid}'") == []


def test_scheduler_publishes_only_due_posts(tenant, fake):
    due, later, draft = _new(tenant, "due"), _new(tenant, "later"), _new(tenant, "draft")
    with tenant_session(tenant) as s:
        social.schedule(s, due, FUTURE)
        social.schedule(s, later, FUTURE)
        s.execute(text("UPDATE social_posts SET scheduled_for = now() - interval '1 minute' WHERE id = :i"), {"i": due})
    done = social.publish_due(fake.factory)
    assert done == [(due, "published")]
    assert _status(tenant, due).status == "published" and _status(tenant, later).status == "scheduled" and _status(tenant, draft).status == "draft"
    assert social.publish_due(fake.factory) == []                              # rien de plus à faire


def test_scheduler_handles_every_tenant_and_keeps_going_after_an_error(tenant, make_tenant, owner_engine, fake):
    other, _ = make_tenant("autre", "P2")
    a, b = _new(tenant, "A"), _new(other, "B")
    for t, p in ((tenant, a), (other, b)):
        with tenant_session(t) as s:
            social.schedule(s, p, FUTURE)
            s.execute(text("UPDATE social_posts SET scheduled_for = now() - interval '1 minute' WHERE id = :i"), {"i": p})
    fake.fail = fb.FacebookError("réseau", kind="network", permanent=False)
    outcomes = dict(social.publish_due(fake.factory))
    assert outcomes == {a: "scheduled", b: "scheduled"}                         # chaque client traité avec ses propres données
    fake.fail = None
    for t, p in ((tenant, a), (other, b)):
        with tenant_session(t) as s:
            s.execute(text("UPDATE social_posts SET next_attempt_at = now() - interval '1 second' WHERE id = :i"), {"i": p})
    assert sorted(st for _, st in social.publish_due(fake.factory)) == ["published", "published"]


def test_failed_scheduled_post_leaves_the_schedule_table(tenant, fake):
    pid = _new(tenant)
    with tenant_session(tenant) as s:
        social.schedule(s, pid, FUTURE)
        s.execute(text("UPDATE social_posts SET scheduled_for = now() - interval '1 minute'"))
    fake.fail = fb.classify(403, {"error": {"code": 200, "message": "x"}})
    social.publish_due(fake.factory)
    assert _status(tenant, pid).status == "failed" and _rows(tenant, "SELECT 1 FROM social_schedule") == []


def test_delete_from_facebook_and_stats(tenant, fake):
    pid = _new(tenant)
    with pytest.raises(social.PostError):
        social.delete_from_facebook(tenant, pid, fake.factory)                 # pas encore publiée
    social.publish(tenant, pid, fake.factory)
    assert social.refresh_stats(tenant, pid, fake.factory) == {"reactions": 12, "comments": 3, "shares": 1}
    st = _rows(tenant, f"SELECT reactions, comments, shares, stats_at IS NOT NULL AS ok FROM social_posts WHERE id = '{pid}'")[0]
    assert (st.reactions, st.comments, st.shares, st.ok) == (12, 3, 1, True)
    social.delete_from_facebook(tenant, pid, fake.factory)
    assert ("delete", "P1_1001") in fake.calls and _status(tenant, pid).status == "deleted"


def test_failed_facebook_deletion_keeps_the_post_published(tenant, fake):
    pid = _new(tenant)
    social.publish(tenant, pid, fake.factory)
    fake.fail = fb.FacebookError("x", kind="network", permanent=False)
    with pytest.raises(fb.FacebookError):
        social.delete_from_facebook(tenant, pid, fake.factory)
    assert _status(tenant, pid).status == "published"


def test_connection_test_uses_stored_page_credentials(tenant, fake):
    assert social.test_connection(tenant, fake.factory) == {"id": "P1", "name": "Maison Kora"}
    assert fake.calls[0][:2] == ("factory", "P1")


def test_set_page_token_encrypts_and_replaces(tenant, owner_engine):
    assert set_page_token(owner_engine, tenant, "EAAB" + "x" * 40) == 1
    with tenant_session(tenant) as s:
        enc = s.execute(text("SELECT credentials_encrypted FROM channels")).scalar_one()
    assert "EAAB" not in enc and decrypt(enc) == "EAAB" + "x" * 40
    with pytest.raises(ValueError):
        set_page_token(owner_engine, tenant, "court")


def test_scheduler_wrapper_never_crashes_the_worker(monkeypatch):
    from app import worker
    monkeypatch.setattr(social, "publish_due", lambda: [(1, "published"), (2, "failed")])
    assert worker.run_scheduler() == 2

    def boom():
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(social, "publish_due", boom)
    assert worker.run_scheduler() == 0


# --- rédaction IA ---------------------------------------------------------------------------------------------------

def _product():
    return next(p for p in KORA["catalog"]["products"] if p["id"] == "KOR-R01")


def test_ai_draft_is_cleaned_and_only_fed_verified_facts():
    llm = FakeLLM(text_reply('**Nouvelle robe Sika** 😍\n\nEn wax indigo, 32 000 FCFA.\nÉcrivez-nous en message privé !'))
    out = social_ai.generate_post(tenant_name="Maison Kora", config=KORA, brief="annonce la robe", product=_product(),
                                  catalog_prices={32000, 1500}, llm=llm)
    assert "**" not in out["text"] and out["text"].startswith("Nouvelle robe Sika") and out["warnings"] == []
    call = llm.calls[0]
    user = call["messages"][0]["content"]
    assert "Robe Sika" in user and "32000" in user and "Cotonou" in user and "1500" in user
    assert "n'invente" in call["system"].lower() and call["tools"] == []


def test_ai_draft_flags_prices_that_are_not_in_the_catalog():
    llm = FakeLLM(text_reply("Robe Sika à 28 500 FCFA seulement ! Livraison 1 500 FCFA. Promo à 20 000 F CFA."))
    out = social_ai.generate_post(tenant_name="K", config=KORA, brief="x", product=_product(), catalog_prices={32000, 28500, 1500}, llm=llm)
    assert out["warnings"] == ["Vérifiez le montant « 20 000 FCFA » : il ne correspond pas au catalogue."]


@pytest.mark.parametrize("text_,amounts", [("32 000 FCFA", [32000]), ("32000 F CFA et 1 500 XOF", [32000, 1500]),
                                           ("32.000 francs", [32000]), ("rien ici", []), ("2026 est là", [])])
def test_amount_extraction(text_, amounts):
    assert social_ai.amounts_in(text_) == amounts


def test_ai_rejects_empty_request_and_empty_answer():
    with pytest.raises(ValueError):
        social_ai.generate_post(tenant_name="K", config=KORA, brief="  ", product=None, catalog_prices=set(), llm=FakeLLM())
    with pytest.raises(ValueError):
        social_ai.generate_post(tenant_name="K", config=KORA, brief="x" * 1001, product=None, catalog_prices=set(), llm=FakeLLM())
    with pytest.raises(LLMError):
        social_ai.generate_post(tenant_name="K", config=KORA, brief="annonce", product=None, catalog_prices=set(), llm=FakeLLM(text_reply("  ")))


def test_ai_uses_the_tenant_model_override():
    llm = FakeLLM(text_reply("Bonjour"))
    cfg = {**KORA, "agent": {"model": "claude-sonnet-5-5"}}
    social_ai.generate_post(tenant_name="K", config=cfg, brief="annonce", product=None, catalog_prices=set(), llm=llm)
    assert llm.calls[0]["model"] == "claude-sonnet-5-5"


# --- console ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tenant, monkeypatch, fake):
    s = get_settings()
    monkeypatch.setattr(s, "console_password", PASSWORD)
    monkeypatch.setattr(s, "console_tenant_id", str(tenant))
    auth.limiter._hits.clear()
    with TestClient(app, follow_redirects=False) as c:
        c.post("/console/login", content=urlencode({"password": PASSWORD}), headers={"Content-Type": "application/x-www-form-urlencoded"})
        yield c


def test_posts_pages_require_login(tenant, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "console_password", PASSWORD)
    monkeypatch.setattr(s, "console_tenant_id", str(tenant))
    pid = _new(tenant)
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/console/posts").status_code == 303 and anon.get("/console/posts/new").status_code == 303
        assert anon.get(f"/console/post-media/{pid}").status_code == 303
        for url in ("/console/api/posts", f"/console/api/posts/{pid}/publish", "/console/api/social/generate"):
            assert anon.post(url, json={}).status_code == 401, url
        assert anon.get("/console/api/social/connection").status_code == 401
    assert _status(tenant, pid).status == "draft"                                 # rien n'a bougé


def test_pages_render_and_escape_user_text(client, tenant):
    pid = _new(tenant, "<script>alert('xss')</script> Promo")
    with tenant_session(tenant) as s:
        social.set_image(s, tenant, pid, PNG)
    for url in ("/console/posts", "/console/posts?status=draft", "/console/posts?view=calendar", "/console/posts/new",
                f"/console/posts/{pid}", "/console/posts/new?date=2030-05-17"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "<script>alert('xss')" not in r.text, url
    assert "&lt;script&gt;" in client.get("/console/posts").text
    assert f"/console/post-media/{pid}" in client.get("/console/posts").text
    assert 'data-prefill="2030-05-17T09:00"' in client.get("/console/posts/new?date=2030-05-17").text
    assert client.get("/console/posts?view=calendar&month=2026-13").status_code == 400
    assert client.get("/console/posts?view=calendar&month=abc").status_code == 400
    assert client.get("/console/posts/00000000-0000-0000-0000-000000000000").status_code == 404


def test_calendar_shows_scheduled_and_published_posts(client, tenant, fake):
    a, b = _new(tenant, "Programmée du jour"), _new(tenant, "Publiée du jour")
    with tenant_session(tenant) as s:
        social.schedule(s, a, datetime.now(timezone.utc) + timedelta(minutes=30))
    social.publish(tenant, b, fake.factory)
    page = client.get("/console/posts?view=calendar").text
    assert "Programmée du jour" in page and "Publiée du jour" in page


def test_api_full_flow_create_schedule_publish(client, tenant, fake):
    r = client.post("/console/api/posts", json={"message": "Hello", "source": "ai", "product_sku": "KOR-R01"})
    pid = r.json()["id"]
    assert _rows(tenant, "SELECT source, product_sku FROM social_posts")[0] == ("ai", "KOR-R01")
    u = client.put(f"/console/api/posts/{pid}", json={"message": "Hello 2"}).json()
    assert u["message"] == "Hello 2" and u["status"] == "draft"
    assert client.post(f"/console/api/posts/{pid}/image", content=PNG, headers={"Content-Type": "image/png"}).json() == {"content_type": "image/png"}
    assert client.get(f"/console/post-media/{pid}").content == PNG
    when = (datetime.now(timezone.utc) + timedelta(days=10)).replace(second=0, microsecond=0)
    local = (when + timedelta(hours=get_settings().console_utc_offset_hours)).strftime("%Y-%m-%dT%H:%M")
    sch = client.post(f"/console/api/posts/{pid}/schedule", json={"when_local": local}).json()
    assert sch["status"] == "scheduled" and sch["scheduled_local"] == local
    assert _rows(tenant, "SELECT scheduled_for FROM social_posts")[0].scheduled_for == when          # heure locale -> UTC
    assert client.post(f"/console/api/posts/{pid}/unschedule", json={}).json()["status"] == "draft"
    pub = client.post(f"/console/api/posts/{pid}/publish", json={}).json()
    assert pub["status"] == "published" and pub["permalink"].startswith("https://www.facebook.com/") and pub["post"]["status"] == "published"
    assert client.put(f"/console/api/posts/{pid}", json={"message": "trop tard"}).status_code == 400
    assert client.post(f"/console/api/posts/{pid}/stats", json={}).json() == {"reactions": 12, "comments": 3, "shares": 1}
    assert client.post(f"/console/api/posts/{pid}/delete", json={}).json() == {"deleted": True, "from_facebook": True}
    assert _status(tenant, pid).status == "deleted"


def test_api_publish_failure_is_reported_not_raised(client, tenant, fake):
    pid = client.post("/console/api/posts", json={"message": "Hello"}).json()["id"]
    fake.fail = fb.classify(403, {"error": {"code": 200, "message": "x"}})
    out = client.post(f"/console/api/posts/{pid}/publish", json={}).json()
    assert out["status"] == "failed" and "pages_manage_posts" in out["error"] and out["post"]["status"] == "failed"


def test_api_rejects_bad_requests(client, tenant):
    pid = client.post("/console/api/posts", json={"message": "Hello"}).json()["id"]
    assert client.post("/console/api/posts", content="message=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/console/api/posts", content="pas du json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/console/api/posts", json=["liste"]).status_code == 400
    assert client.post("/console/api/posts", json={"message": "x" * 3001}).status_code == 400
    assert client.post(f"/console/api/posts/{pid}/image", content=b"<html>", headers={"Content-Type": "image/png"}).status_code == 400
    assert client.post(f"/console/api/posts/{pid}/image", content=PNG, headers={"Content-Type": "application/json"}).status_code == 415
    assert client.post(f"/console/api/posts/{pid}/image", content=b"\0" * 8_600_000, headers={"Content-Type": "image/png"}).status_code == 413
    assert client.post(f"/console/api/posts/{pid}/schedule", json={"when_local": "2001-01-01T09:00"}).status_code == 400
    assert client.post(f"/console/api/posts/{pid}/schedule", json={"when_local": "pas une date"}).status_code == 400
    assert client.post(f"/console/api/posts/{pid}/unschedule", json={}).status_code == 400          # pas programmée
    assert client.post(f"/console/api/posts/{pid}/stats", json={}).status_code == 400                # pas publiée
    empty = client.post("/console/api/posts", json={"message": ""}).json()["id"]
    assert client.post(f"/console/api/posts/{empty}/publish", json={}).status_code == 400
    assert client.post("/console/api/posts/00000000-0000-0000-0000-000000000000/delete", json={}).status_code == 404


def test_api_delete_draft_and_image_removal(client, tenant):
    pid = client.post("/console/api/posts", json={"message": "Hello"}).json()["id"]
    client.post(f"/console/api/posts/{pid}/image", content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert client.post(f"/console/api/posts/{pid}/image/remove", json={}).status_code == 200
    assert client.get(f"/console/post-media/{pid}").status_code == 404
    assert client.post(f"/console/api/posts/{pid}/delete", json={}).json() == {"deleted": True, "from_facebook": False}
    assert _rows(tenant, "SELECT 1 FROM social_posts") == []


def test_post_media_is_served_safely(client, tenant):
    pid = _new(tenant)
    client.post(f"/console/api/posts/{pid}/image", content=PNG, headers={"Content-Type": "image/png"})
    r = client.get(f"/console/post-media/{pid}")
    assert r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"] and "private" in r.headers["cache-control"]


def test_api_generate_uses_catalog_facts_and_flags_wrong_prices(client, tenant, monkeypatch):
    llm = FakeLLM(text_reply("Robe Sika en wax indigo à 35 000 FCFA ! Écrivez-nous en message privé."))
    monkeypatch.setattr(social_ai, "get_llm", lambda: llm)
    out = client.post("/console/api/social/generate", json={"brief": "annonce la robe", "product_sku": "KOR-R01"}).json()
    assert out["text"].startswith("Robe Sika") and len(out["warnings"]) == 1 and "35 000" in out["warnings"][0]
    assert "32000" in llm.calls[0]["messages"][0]["content"]
    assert client.post("/console/api/social/generate", json={"brief": "", "product_sku": None}).status_code == 400
    assert client.post("/console/api/social/generate", json={"brief": "x", "product_sku": "NOPE"}).status_code == 404


def test_api_generate_reports_model_outage(client, monkeypatch):
    class Down:
        def create(self, **kw):
            raise LLMError("429 rate limit")

    monkeypatch.setattr(social_ai, "get_llm", lambda: Down())
    r = client.post("/console/api/social/generate", json={"brief": "annonce"})
    assert r.status_code == 502 and "429" not in r.text


def test_api_connection_test(client, fake):
    assert client.get("/console/api/social/connection").json() == {"ok": True, "id": "P1", "name": "Maison Kora"}
    fake.fail = fb.classify(400, {"error": {"code": 190, "message": "x"}})
    out = client.get("/console/api/social/connection").json()
    assert out["ok"] is False and "set_page_token" in out["error"]


def test_console_never_touches_another_tenants_posts(client, tenant, make_tenant):
    other, _ = make_tenant("autre", "P2")
    foreign = _new(other, "Secret d'une autre entreprise")
    page = client.get("/console/posts").text
    assert "Secret d'une autre" not in page
    assert client.get(f"/console/posts/{foreign}").status_code == 404
    assert client.put(f"/console/api/posts/{foreign}", json={"message": "piraté"}).status_code == 400
    assert client.post(f"/console/api/posts/{foreign}/publish", json={}).status_code == 400
    assert client.post(f"/console/api/posts/{foreign}/delete", json={}).status_code == 404
    assert client.get(f"/console/post-media/{foreign}").status_code == 404
    assert _rows(other, f"SELECT message, status FROM social_posts WHERE id = '{foreign}'")[0] == ("Secret d'une autre entreprise", "draft")
