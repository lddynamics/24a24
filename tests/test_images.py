"""Images : lecture des images reçues (vision), photos des produits (console) et envoi de photos par l'agent."""
import html
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from app.agent.orchestrator import STICKER_TEXT, to_llm_messages
from app.agent.prompt import build_system_prompt
from app.agent.text import clean_for_messenger
from app.channels.messenger import parse_payload
from app.channels.sender import MessengerSender, SendError
from app.config import get_settings
from app.console import auth
from app.db import tenant_session
from app.main import app
from app.services import catalog as catalog_service
from app.services import images as image_service
from app.services import vision
from app.services.ingest import ingest
from app.services.media import HttpMediaFetcher, Media, MediaError
from app.services.vision import AnthropicDescriber, OpenAICompatDescriber, VisionError, build_describer
from app.worker import run_once
from scripts.send_test_webhook import build_payload
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeLLM, FakeSender, text_reply, tool_reply
from tests.seed import seed_catalog
from tests.test_agent import _in, _job_statuses, _rows

KORA = json.loads(Path("tenants/maison-kora.json").read_text(encoding="utf-8"))
PASSWORD = "mot-de-passe-test"


def make_image(fmt="PNG", size=(120, 80), color=(200, 40, 40), mode="RGB", **save):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt, **save)
    return buf.getvalue()


PNG = make_image("PNG")
JPG = make_image("JPEG")


# --- normalisation et validation ---------------------------------------------------------------------

def test_normalize_turns_any_supported_image_into_a_bounded_jpeg():
    for fmt, mode, color in (("PNG", "RGBA", (255, 0, 0, 0)), ("GIF", "P", 3), ("WEBP", "RGB", (1, 2, 3)), ("JPEG", "RGB", (9, 9, 9))):
        out, mime = image_service.normalize_image(make_image(fmt, mode=mode, color=color))
        assert mime == "image/jpeg" and image_service.sniff_image(out) == "image/jpeg"
    big, _ = image_service.normalize_image(make_image("PNG", size=(3200, 2000)))
    assert max(Image.open(io.BytesIO(big)).size) == image_service.MAX_SIDE
    small, _ = image_service.normalize_image(make_image("PNG", size=(100, 50)))
    assert Image.open(io.BytesIO(small)).size == (100, 50)           # jamais agrandie


def test_normalize_applies_orientation_and_strips_metadata():
    img = Image.new("RGB", (200, 100), (10, 200, 10))
    exif = Image.Exif()
    exif[0x0112] = 6                                                   # à faire pivoter de 90°
    exif[0x010F] = "AppareilSecret"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out, _ = image_service.normalize_image(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    assert result.size == (100, 200) and len(result.getexif()) == 0 and b"AppareilSecret" not in out


@pytest.mark.parametrize("data", [b"", b"<html><script>alert(1)</script></html>", b"\xff\xd8\xff" + b"\x00" * 20, PNG[:60], b"GIF89a" + b"\x00" * 10])
def test_normalize_refuses_things_that_are_not_real_images(data):
    with pytest.raises(image_service.ImageError):
        image_service.normalize_image(data)


def test_vision_copy_fits_the_provider_limit_even_for_noisy_photos():
    noisy = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    assert len(image_service.compress_for_vision(buf.getvalue(), max_bytes=130_000)) <= 130_000


# --- téléchargement d'une image reçue -----------------------------------------------------------------

def _fetcher(handler, **kw):
    return HttpMediaFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_fetch_image_is_typed_by_content_and_reencoded():
    got = _fetcher(lambda r: httpx.Response(200, content=PNG, headers={"content-type": "text/html"})).fetch_image("https://cdn.example/a")
    assert got.content_type == "image/jpeg" and image_service.sniff_image(got.data) == "image/jpeg" and got.data != PNG


@pytest.mark.parametrize("url,handler,fragment", [
    ("http://cdn.example/a", lambda r: httpx.Response(200, content=PNG), "HTTPS"),
    ("https://cdn.example/a", lambda r: httpx.Response(404), "refusé"),
    ("https://cdn.example/a", lambda r: httpx.Response(200, content=b"<html>pas une image" + b" " * 30), "pas une image"),
    ("https://cdn.example/a", lambda r: httpx.Response(200, content=b"\x00\x00\x00\x20ftypM4A " + b"\x01" * 60), "pas une image"),
    ("https://cdn.example/a", lambda r: httpx.Response(200, content=PNG * 500), "volumineux"),
])
def test_fetch_image_refuses_bad_downloads(url, handler, fragment):
    with pytest.raises(MediaError, match=fragment):
        _fetcher(handler, max_bytes=2000).fetch_image(url)


# --- modèle de vision ---------------------------------------------------------------------------------

def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_openai_compatible_describer_sends_a_standard_image_request():
    seen = {}

    def handler(req):
        seen.update(auth=req.headers["authorization"], body=json.loads(req.read()), url=str(req.url))
        return _ok("<think>je réfléchis</think>Robe wax jaune, coupe évasée.")

    d = OpenAICompatDescriber("KEY", "https://api.example/v1/", "vision-model", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert d.describe(PNG, "image/png") == "Robe wax jaune, coupe évasée."
    assert seen["auth"] == "Bearer KEY" and seen["url"] == "https://api.example/v1/chat/completions"
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "DONNÉE" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and seen["body"]["model"] == "vision-model"


def test_describer_retries_once_with_the_img_tag_format_when_the_provider_rejects_image_url():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.read()))
        return httpx.Response(400, text="image_url non supporté") if len(bodies) == 1 else _ok("Un foulard rouge.")

    d = OpenAICompatDescriber("K", "https://api.example/v1", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert d.describe(PNG) == "Un foulard rouge."
    assert isinstance(bodies[0]["messages"][0]["content"], list)
    assert isinstance(bodies[1]["messages"][0]["content"], str) and '<img src="data:image/jpeg;base64,' in bodies[1]["messages"][0]["content"]


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(500, text="panne"), lambda r: httpx.Response(429, text="quota"),
                                     lambda r: httpx.Response(200, json={"unexpected": True})])
def test_describer_reports_provider_failures(handler):
    d = OpenAICompatDescriber("K", "https://api.example/v1", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(VisionError):
        d.describe(PNG)


def test_describer_refuses_a_non_image_before_any_network_call():
    called = []
    d = OpenAICompatDescriber("K", "https://api.example/v1", "m", client=httpx.Client(transport=httpx.MockTransport(lambda r: called.append(1))))
    with pytest.raises(VisionError):
        d.describe(b"<html>")
    assert called == []


def test_anthropic_describer_sends_an_image_block():
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="Ensemble pagne bleu.")])

    d = AnthropicDescriber("k", "claude-sonnet-5-5", client=SimpleNamespace(messages=FakeMessages()))
    assert d.describe(PNG) == "Ensemble pagne bleu."
    block = calls[0]["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg" and calls[0]["model"] == "claude-sonnet-5-5"


def test_build_describer_follows_configuration(monkeypatch):
    s = get_settings()
    for k, v in dict(vision_model="", anthropic_api_key="", vision_api_key="", llm_api_key="", groq_api_key="", vision_base_url="",
                     llm_base_url="https://nim.example/v1").items():
        monkeypatch.setattr(s, k, v)
    assert build_describer() is None                                    # pas de modèle : images non lues
    monkeypatch.setattr(s, "vision_model", "meta/llama-vision")
    assert build_describer() is None                                    # modèle sans clé
    monkeypatch.setattr(s, "llm_api_key", "nvapi-x")
    d = build_describer()
    assert isinstance(d, OpenAICompatDescriber) and d._base == "https://nim.example/v1"
    monkeypatch.setattr(s, "vision_model", "claude-sonnet-5-5")
    assert build_describer() is None                                    # Claude sans clé Anthropic
    monkeypatch.setattr(s, "anthropic_api_key", "sk-ant")
    assert isinstance(build_describer(), AnthropicDescriber)


# --- image reçue : archive, description, historique ----------------------------------------------------

@pytest.fixture
def tenant(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)
    seed_catalog(tenant_id, KORA)
    return tenant_id


class FakeFetcher:
    def __init__(self, media=None, error=None):
        self.media, self.error, self.calls = media, error, []

    def fetch_image(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.media


class FakeDescriber:
    def __init__(self, text="Photo d'une robe en wax jaune, coupe évasée, sans manches.", error=None):
        self.text, self.error, self.calls = text, error, []

    def describe(self, data, content_type="image/jpeg"):
        self.calls.append((len(data), content_type))
        if self.error:
            raise self.error
        return self.text


def _run(llm, sender, fetcher=None, describer=None):
    n = 0
    while run_once(llm, sender.factory(), None, fetcher, describer):
        n += 1
        assert n < 20


JPEG_MEDIA = Media(image_service.normalize_image(PNG)[0], "image/jpeg")


def test_incoming_image_is_archived_described_and_shown_to_the_agent_as_text(tenant):
    _in("U1", "", "m1", kind="image")
    fetcher, describer, llm = FakeFetcher(JPEG_MEDIA), FakeDescriber(), FakeLLM(text_reply("Jolie robe ! Cherchez-vous une taille précise ?"))
    _run(llm, FakeSender(), fetcher, describer)
    assert fetcher.calls == ["https://cdn.example/x"] and len(describer.calls) == 1
    [m] = _rows(tenant, "SELECT content_type, data, description FROM message_media")
    assert m.content_type == "image/jpeg" and bytes(m.data) == JPEG_MEDIA.data and "wax jaune" in m.description
    first = llm.calls[0]["messages"][0]["content"]
    assert first.startswith("[Image envoyée par le client — description automatique : Photo d'une robe en wax jaune")
    assert "que je ne peux pas encore voir" not in first


def test_description_is_computed_once_and_replayed_in_later_turns(tenant):
    _in("U1", "", "m1", kind="image")
    describer, llm, sender = FakeDescriber(), FakeLLM(text_reply("Belle robe !"), text_reply("Avec plaisir.")), FakeSender()
    _run(llm, sender, FakeFetcher(JPEG_MEDIA), describer)
    _in("U1", "Vous l'avez en M ?", "m2")
    _run(llm, sender, FakeFetcher(JPEG_MEDIA), describer)
    assert len(describer.calls) == 1                                   # pas de second appel au modèle de vision
    history = llm.calls[1]["messages"]
    assert "wax jaune" in history[0]["content"] and history[-1]["content"] == "Vous l'avez en M ?"


def test_vision_failure_keeps_the_archive_and_falls_back_to_asking_for_text(tenant):
    _in("U1", "", "m1", kind="image")
    llm = FakeLLM(text_reply("Pourriez-vous décrire votre demande ?"))
    _run(llm, FakeSender(), FakeFetcher(JPEG_MEDIA), FakeDescriber(error=VisionError("panne")))
    assert len(_rows(tenant, "SELECT 1 FROM message_media")) == 1
    assert _rows(tenant, "SELECT description FROM message_media")[0].description is None
    assert "ne peux pas encore voir" in llm.calls[0]["messages"][0]["content"]


def test_download_failure_does_not_call_the_vision_model(tenant):
    _in("U1", "", "m1", kind="image")
    describer, llm = FakeDescriber(), FakeLLM(text_reply("Pourriez-vous écrire ?"))
    _run(llm, FakeSender(), FakeFetcher(error=MediaError("expiré")), describer)
    assert describer.calls == [] and _rows(tenant, "SELECT 1 FROM message_media") == []
    assert "ne peux pas encore voir" in llm.calls[0]["messages"][0]["content"]


def test_without_vision_configured_behaviour_is_unchanged(tenant):
    _in("U1", "", "m1", kind="image")
    llm = FakeLLM(text_reply("Pourriez-vous écrire ?"))
    _run(llm, FakeSender())
    assert "ne peux pas encore voir" in llm.calls[0]["messages"][0]["content"]


def test_a_caption_travels_with_the_description():
    msgs = to_llm_messages([("in", "image", "La même en rouge ?", "Robe jaune évasée")])
    assert msgs[0]["content"] == "[Image envoyée par le client — description automatique : Robe jaune évasée] Légende du client : La même en rouge ?"


def test_a_sticker_is_not_sent_to_the_vision_model(tenant):
    payload = build_payload("P1", "U1", "x", mid="s1")
    m = payload["entry"][0]["messaging"][0]["message"]
    m.pop("text")
    m["attachments"] = [{"type": "image", "payload": {"url": "https://cdn.example/like", "sticker_id": 369239263222822}}]
    ingest(parse_payload(payload)[0])
    fetcher, describer, llm = FakeFetcher(JPEG_MEDIA), FakeDescriber(), FakeLLM(text_reply("Avec plaisir !"))
    _run(llm, FakeSender(), fetcher, describer)
    assert fetcher.calls == [] and describer.calls == []
    assert llm.calls[0]["messages"][0]["content"] == STICKER_TEXT


def test_system_annotations_are_never_sent_to_the_customer():
    assert clean_for_messenger("[Photo envoyée : Robe Sika]\nVoici la robe.") == "Voici la robe."
    assert clean_for_messenger("[Image envoyée par le client — description automatique : x] Bien reçu.") == "Bien reçu."


# --- prompt --------------------------------------------------------------------------------------------

def test_photo_module_is_only_in_the_prompt_when_the_tool_is_enabled():
    on = build_system_prompt("Maison Kora", "commerce", KORA)
    off = build_system_prompt("Maison Kora", "commerce", {**KORA, "tools_enabled": ["get_catalog", "escalate_to_human"]})
    assert "Photos des articles" in on and "Photos des articles" not in off
    assert "Images du client" in on and "Images du client" in off       # la lecture des images vaut pour tous
    assert "annotations du système" in on


# --- envoi vers Messenger ------------------------------------------------------------------------------

def _messenger(handler):
    return MessengerSender("PAGE_TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_messenger_image_upload_request_shape():
    seen = {}

    def handler(req):
        seen.update(auth=req.headers["authorization"], ctype=req.headers["content-type"], body=req.read(), url=str(req.url))
        return httpx.Response(200, json={"recipient_id": "U1", "message_id": "mid.9"})

    assert _messenger(handler).send_image("U1", JPG, "image/jpeg") == "mid.9"
    body = seen["body"]
    assert seen["auth"] == "Bearer PAGE_TOKEN" and "PAGE_TOKEN" not in seen["url"] and seen["ctype"].startswith("multipart/form-data")
    assert b'"type": "image"' in body and b'filename="photo.jpg"' in body and JPG in body and b"RESPONSE" in body


@pytest.mark.parametrize("status,permanent", [(400, True), (403, True), (500, False), (429, False)])
def test_messenger_image_error_mapping(status, permanent):
    with pytest.raises(SendError) as e:
        _messenger(lambda r: httpx.Response(status, text="err")).send_image("U1", JPG, "image/jpeg")
    assert e.value.permanent is permanent


def test_messenger_refuses_unsupported_image_formats():
    with pytest.raises(SendError) as e:
        _messenger(lambda r: httpx.Response(200)).send_image("U1", b"x", "image/webp")
    assert e.value.permanent is True


# --- console : photos des produits ---------------------------------------------------------------------

@pytest.fixture
def client(tenant, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "console_password", PASSWORD)
    monkeypatch.setattr(s, "console_tenant_id", str(tenant))
    auth.limiter._hits.clear()
    with TestClient(app, follow_redirects=False) as c:
        c.post("/console/login", content=urlencode({"password": PASSWORD}), headers={"Content-Type": "application/x-www-form-urlencoded"})
        yield c


def _upload(c, sku="KOR-R01", data=PNG, ctype="image/png"):
    return c.post(f"/console/api/catalog/{sku}/images", content=data, headers={"Content-Type": ctype})


def test_upload_stores_a_clean_jpeg_and_serves_it_to_the_console(client, tenant):
    r = _upload(client)
    assert r.status_code == 200
    img = client.get(r.json()["url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and image_service.sniff_image(img.content) == "image/jpeg"
    assert img.headers["x-content-type-options"] == "nosniff" and "private" in img.headers["cache-control"]
    with tenant_session(tenant) as s:
        p = catalog_service.get_product(s, "KOR-R01")
    assert p["image_count"] == 1 and p["cover_image_id"] == r.json()["id"]


def test_catalog_and_edit_pages_show_the_photos(client, tenant):
    image_id = _upload(client).json()["id"]
    assert f"/console/catalog/images/{image_id}" in client.get("/console/catalog").text
    page = client.get("/console/catalog/KOR-R01/edit").text
    assert f"/console/catalog/images/{image_id}" in page and 'id="photos-card"' in page and "Principale" in page
    assert "sans photo" in client.get("/console/catalog").text                      # les autres produits n'en ont pas
    assert "Les photos s'ajoutent juste après" in client.get("/console/catalog/new").text


@pytest.mark.parametrize("data,ctype,status", [
    (PNG, "application/x-www-form-urlencoded", 415), (PNG, "text/html", 415),
    (b"<html><script>alert(1)</script></html>", "image/png", 400),       # l'en-tête ment : le contenu décide
    (b"", "image/png", 400),
])
def test_upload_rejects_wrong_content(client, data, ctype, status):
    assert _upload(client, data=data, ctype=ctype).status_code == status


def test_upload_limits_and_unknown_product(client, tenant):
    assert _upload(client, sku="NOPE").status_code == 404
    assert _upload(client, data=b"\x00" * 12_000_001).status_code == 413
    for _ in range(catalog_service.MAX_PRODUCT_IMAGES):
        assert _upload(client).status_code == 200
    r = _upload(client)
    assert r.status_code == 400 and "maximum" in r.json()["detail"]


def test_upload_requires_login(client):
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.post("/console/api/catalog/KOR-R01/images", content=PNG, headers={"Content-Type": "image/png"}).status_code == 401
        image_id = _upload(client).json()["id"]
        assert anon.get(f"/console/catalog/images/{image_id}").status_code == 303


def test_cover_delete_and_cascade(client, tenant):
    a, b = _upload(client).json()["id"], _upload(client).json()["id"]
    assert client.post(f"/console/api/catalog/KOR-R01/images/{b}/cover").status_code == 200
    with tenant_session(tenant) as s:
        assert [i["id"] for i in catalog_service.list_product_images(s, "KOR-R01")] == [b, a]
    assert client.post(f"/console/api/catalog/KOR-R01/images/{b}/delete").status_code == 200
    assert client.post(f"/console/api/catalog/KOR-R01/images/{b}/delete").status_code == 404
    assert client.post(f"/console/api/catalog/KOR-R02/images/{a}/delete").status_code == 404     # pas sur un autre produit
    client.post("/console/catalog/KOR-R01/delete")
    assert _rows(tenant, "SELECT 1 FROM product_images") == []


def test_a_new_product_leads_to_its_photo_page(client):
    fields = {"name": "Robe Test", "category": "robes", "description": "d", "price": "25000", "tags": "", "sku": "TST-9",
              "variant_size": ["S"], "variant_color": ["Bleu"], "variant_stock": ["2"]}
    r = client.post("/console/catalog/new", content=urlencode(fields, doseq=True), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303 and r.headers["location"] == "/console/catalog/TST-9/edit?ok=created_photos"
    assert "Ajoutez maintenant ses photos" in client.get(r.headers["location"]).text


def test_photos_are_isolated_between_clients(client, tenant, make_tenant):
    image_id = _upload(client).json()["id"]
    other, _ = make_tenant("autre", "P2")
    with tenant_session(other) as s:
        assert catalog_service.get_product_image(s, image_id) is None
        assert catalog_service.list_product_images(s, "KOR-R01") == []


# --- l'agent envoie des photos -------------------------------------------------------------------------

@pytest.fixture
def photos(tenant):
    """KOR-R01 : 2 photos, KOR-R02 : 1, KOR-E01 : 1. KOR-A01 : aucune."""
    ids = {}
    with tenant_session(tenant) as s:
        for sku, colors in (("KOR-R01", [(200, 30, 30), (30, 30, 200)]), ("KOR-R02", [(30, 200, 30)]), ("KOR-E01", [(200, 200, 30)])):
            ids[sku] = [catalog_service.add_product_image(s, tenant, sku, make_image("PNG", color=c)) for c in colors]
    return ids


def _send(sku, **kw):
    return tool_reply("send_product_image", {"product_id": sku, **kw})


def test_agent_sends_the_photo_before_its_text(tenant, photos):
    _in("U1", "Je peux voir la robe Sika ?", "m1")
    llm, sender = FakeLLM(_send("KOR-R01"), text_reply("Voici la Robe Sika, 32 000 FCFA. Quelle taille souhaitez-vous ?")), FakeSender()
    _run(llm, sender)
    assert sender.log == ["image", "text"] and len(sender.sent_images) == 1
    recipient, data, ctype = sender.sent_images[0]
    assert recipient == "U1" and ctype == "image/jpeg" and image_service.sniff_image(data) == "image/jpeg"
    result = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert result["queued"] is True and result["product"] == "Robe Sika"
    out = _rows(tenant, "SELECT type, text, delivery_status, raw FROM messages WHERE direction='out' ORDER BY created_at, id")
    assert [(o.type, o.delivery_status) for o in out] == [("image", "sent"), ("text", "sent")]
    assert out[0].text == "[Photo envoyée : Robe Sika]" and out[0].raw["sku"] == "KOR-R01"
    assert bytes(_rows(tenant, "SELECT data FROM message_media")[0].data) == data


def test_sent_photos_show_in_the_console_and_in_the_next_turn(tenant, photos, client):
    _in("U1", "La robe Sika ?", "m1")
    llm, sender = FakeLLM(_send("KOR-R01"), text_reply("La voici."), text_reply("Avec plaisir !")), FakeSender()
    _run(llm, sender)
    conv = _rows(tenant, "SELECT id FROM conversations")[0].id
    msgs = client.get(f"/console/api/conversations/{conv}/messages").json()["messages"]
    pic = [m for m in msgs if m["image"]][0]
    assert pic["author"] == "agent" and pic["image_url"].startswith("/console/media/") and client.get(pic["image_url"]).status_code == 200
    _in("U1", "Merci", "m2")
    _run(llm, sender)
    history = llm.calls[-1]["messages"]
    assert history[1] == {"role": "assistant", "content": "[Photo envoyée : Robe Sika]\nLa voici."}


def test_console_shows_an_incoming_image_with_what_the_agent_understood(tenant, client):
    _in("U1", "", "m1", kind="image")
    assert "🖼 Photo" in client.get("/console/fragments/conversation-list").text            # aperçu avant tout traitement
    _run(FakeLLM(text_reply("Jolie robe !")), FakeSender(), FakeFetcher(JPEG_MEDIA), FakeDescriber())
    conv = _rows(tenant, "SELECT id FROM conversations")[0].id
    mid = _rows(tenant, "SELECT id FROM messages WHERE direction='in'")[0].id
    page = html.unescape(client.get(f"/console/conversations/{conv}").text)
    assert f'<img class="pic" src="/console/media/{mid}"' in page and "Vu par l'agent : Photo d'une robe en wax jaune" in page
    j = [m for m in client.get(f"/console/api/conversations/{conv}/messages").json()["messages"] if m["image"]][0]
    assert j["author"] == "customer" and j["image_url"] == f"/console/media/{mid}" and "wax jaune" in j["note"]
    r = client.get(j["image_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPEG_MEDIA.data
    assert r.headers["x-content-type-options"] == "nosniff" and 'filename="photo.jpg"' in r.headers["content-disposition"]
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get(j["image_url"]).status_code == 303


def test_get_catalog_announces_how_many_photos_exist(tenant, photos):
    _in("U1", "Les robes ?", "m1")
    llm = FakeLLM(tool_reply("get_catalog", {"query": "Sika"}), text_reply("Voici."))
    _run(llm, FakeSender())
    product = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])["products"][0]
    assert product["photos"] == 2


@pytest.mark.parametrize("args,fragment", [
    ({"product_id": "KOR-A01"}, "Aucune photo"), ({"product_id": "NOPE"}, "introuvable"),
    ({"product_id": "KOR-R02", "photo_number": 3}, "1 photo"),
])
def test_tool_refuses_what_it_cannot_send(tenant, photos, args, fragment):
    _in("U1", "Photo ?", "m1")
    llm, sender = FakeLLM(tool_reply("send_product_image", args), text_reply("Je n'ai pas de photo de cet article.")), FakeSender()
    _run(llm, sender)
    result = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert result["queued"] is False and fragment in result["error"] and sender.sent_images == []


def test_hidden_products_never_leak_through_photos(tenant, photos):
    with tenant_session(tenant) as s:
        catalog_service.set_active(s, "KOR-R01", False)
    _in("U1", "Photo de la Sika ?", "m1")
    sender = FakeSender()
    _run(FakeLLM(_send("KOR-R01"), text_reply("Je ne la trouve pas.")), sender)
    assert sender.sent_images == []


def test_at_most_three_photos_per_reply_and_no_duplicates(tenant, photos):
    _in("U1", "Montrez-moi tout", "m1")
    llm = FakeLLM(_send("KOR-R01"), _send("KOR-R01"), _send("KOR-R01", photo_number=2), _send("KOR-R02"), _send("KOR-E01"),
                  text_reply("Voici trois modèles."))
    sender = FakeSender()
    _run(llm, sender)
    assert len(sender.sent_images) == 3 and sender.log == ["image", "image", "image", "text"]
    last = json.loads(llm.calls[-1]["messages"][-1]["content"][0]["content"])
    assert last["queued"] is False and "maximum" in last["error"]
    dup = json.loads(llm.calls[2]["messages"][-1]["content"][0]["content"])
    assert dup["queued"] is True and "déjà" in dup["note"]
    stamps = _rows(tenant, "SELECT type FROM messages WHERE direction='out' ORDER BY created_at, id")
    assert [m.type for m in stamps] == ["image", "image", "image", "text"]


def test_no_photo_goes_out_when_the_agent_falls_back_to_a_human(tenant, photos, owner_engine):
    set_tenant_config(owner_engine, tenant, {**KORA, "agent": {"max_steps": 2}})
    _in("U1", "Photos ?", "m1")
    sender = FakeSender()
    _run(FakeLLM(_send("KOR-R01"), repeat_last=True), sender)
    assert sender.sent_images == [] and len(sender.sent) == 1
    assert _rows(tenant, "SELECT status FROM conversations")[0].status == "human"


def test_a_failed_photo_is_retried_without_calling_the_model_again_or_duplicating(tenant, photos, owner_engine):
    _in("U1", "La Sika ?", "m1")
    llm, sender = FakeLLM(_send("KOR-R01"), text_reply("La voici.")), FakeSender(fail_times=1, permanent=False)
    _run(llm, sender)
    assert sender.sent_images == [] and sender.sent == [] and _job_statuses(owner_engine) == ["pending"]
    with owner_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now()"))
    _run(llm, sender)
    assert len(llm.calls) == 2                                          # aucun nouvel appel au modèle
    assert sender.log == ["image", "text"] and _job_statuses(owner_engine) == ["done"]


def test_a_photo_already_sent_is_not_resent_when_only_the_text_failed(tenant, photos, owner_engine):
    class TextFailsOnce(FakeSender):
        failed = False

        def send_text(self, recipient_id, body):
            if not self.failed:
                self.failed = True
                raise SendError("échec simulé", permanent=False)
            return super().send_text(recipient_id, body)

    _in("U1", "La Sika ?", "m1")
    llm, sender = FakeLLM(_send("KOR-R01"), text_reply("La voici.")), TextFailsOnce()
    _run(llm, sender)
    assert len(sender.sent_images) == 1 and sender.sent == []
    with owner_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now()"))
    _run(llm, sender)
    assert len(sender.sent_images) == 1 and sender.sent == [("U1", "La voici.")]


def test_tool_is_not_exposed_when_not_enabled_for_the_client(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("sans", "P1")
    set_tenant_config(owner_engine, tenant_id, {**KORA, "tools_enabled": ["get_catalog", "escalate_to_human"]})
    seed_catalog(tenant_id, KORA)
    _in("U1", "Photo ?", "m1")
    llm = FakeLLM(_send("KOR-R01"), text_reply("Je n'ai pas de photo."))
    _run(llm, FakeSender())
    assert "send_product_image" not in [t["name"] for t in llm.calls[0]["tools"]]
    assert "non disponible" in llm.calls[1]["messages"][-1]["content"][0]["content"]
