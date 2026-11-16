"""Canal WhatsApp : parsing, webhook, envoi, médias, et parcours complet avec le worker."""
import json
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.channels import credentials
from app.channels.sender import ConsoleSender, SendError, WhatsAppSender, get_sender
from app.channels.whatsapp import MEDIA_PREFIX, parse_payload, parse_statuses
from app.crypto import decrypt
from app.db import tenant_session
from app.main import app
from app.services import console as console_service
from app.services import media as media_service
from app.services.ingest import ingest
from app.services.media import HttpMediaFetcher, MediaError
from app.worker import run_once
from scripts.add_channel import add_channel
from scripts.create_tenant import create_tenant
from scripts.send_test_whatsapp_webhook import build_payload, sign
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeLLM, FakeSender, text_reply
from tests.test_agent import CONFIG, _rows
from tests.test_voice import FakeFetcher, FakeTranscriber, make_wav

PNID = "1099887766"
TOKEN = "WA_ACCESS_TOKEN"
OGG = b"OggS\x00\x02" + b"\x01" * 64


@pytest.fixture
def kora(make_tenant, owner_engine):
    """Un client avec DEUX canaux : une page Messenger et un numéro WhatsApp."""
    tenant_id, _ = make_tenant("kora", "PAGE1")
    set_tenant_config(owner_engine, tenant_id, CONFIG)
    add_channel(owner_engine, tenant_id=tenant_id, channel_type="whatsapp", external_id=PNID, access_token=TOKEN)
    return tenant_id


def _in(wa_id="22997000001", body="Bonjour", wamid="wamid.1", **kw):
    return ingest(parse_payload(build_payload(PNID, wa_id, body, wamid=wamid, **kw))[0])


def _post(client, payload, secret="test_secret"):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(secret, body)})


class Recorder:
    """Fabrique d'expéditeurs qui mémorise (type de canal, identifiants) pour vérifier le routage."""

    def __init__(self, sender: FakeSender):
        self.sender, self.calls = sender, []

    def __call__(self, channel_type, token):
        self.calls.append((channel_type, token))
        return self.sender


# --- parsing --------------------------------------------------------------------------------------------------------

def test_parse_text_message_with_profile_name():
    [m] = parse_payload(build_payload(PNID, "22997000001", "Bonjour", wamid="wamid.A", name="Aïcha"))
    assert (m.channel_type, m.channel_external_id, m.channel_user_id) == ("whatsapp", PNID, "22997000001")
    assert (m.type, m.text, m.contact_name, m.media_ref) == ("text", "Bonjour", "Aïcha", None)
    assert m.idempotency_key == "whatsapp:wamid.A" and m.received_at.tzinfo is not None


def test_parse_voice_note_keeps_media_id_not_url():
    [m] = parse_payload(build_payload(PNID, "229", kind="audio", media_id="MID42"))
    assert (m.type, m.text, m.media_ref) == ("audio", None, f"{MEDIA_PREFIX}MID42")


def test_parse_image_with_caption():
    [m] = parse_payload(build_payload(PNID, "229", "Cette robe ?", kind="image", media_id="IMG1"))
    assert (m.type, m.text, m.media_ref) == ("image", "Cette robe ?", f"{MEDIA_PREFIX}IMG1")


def _with_message(**message):
    p = build_payload(PNID, "229", "x")
    base = p["entry"][0]["changes"][0]["value"]["messages"][0]
    p["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": base["from"], "id": base["id"], "timestamp": base["timestamp"], **message}
    return p


def test_parse_location_and_button_reply():
    [loc] = parse_payload(_with_message(type="location", location={"latitude": 6.37, "longitude": 2.39, "name": "Fidjrossè"}))
    assert loc.type == "other" and "6.37" in loc.text and "Fidjrossè" in loc.text
    [btn] = parse_payload(_with_message(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": "1", "title": "Oui"}}))
    assert (btn.type, btn.text) == ("text", "Oui")


def test_parse_ignores_reactions_statuses_and_foreign_fields():
    assert parse_payload(_with_message(type="reaction", reaction={"emoji": "👍", "message_id": "wamid.0"})) == []
    status_only = {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PNID}, "statuses": [{"id": "wamid.9", "status": "delivered"}]}}]}]}
    assert parse_payload(status_only) == []
    other_field = build_payload(PNID, "229", "x")
    other_field["entry"][0]["changes"][0]["field"] = "account_update"
    assert parse_payload(other_field) == []
    no_sender = _with_message(type="text", text={"body": "x"})
    del no_sender["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
    assert parse_payload(no_sender) == []


def test_parse_statuses_extracts_failure_details():
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PNID},
        "statuses": [{"id": "wamid.7", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]},
                     {"id": "wamid.8", "status": "read"}]}}]}]}
    failed, read = parse_statuses(payload)
    assert (failed.phone_number_id, failed.message_id, failed.status, failed.error_code) == (PNID, "wamid.7", "failed", 131047)
    assert read.status == "read" and read.error_code is None


# --- identifiants ---------------------------------------------------------------------------------------------------

def test_credentials_roundtrip_and_rejections():
    assert credentials.encode("messenger", "tok", "PAGE") == "tok"
    assert credentials.whatsapp_credentials(credentials.encode("whatsapp", "tok", "123")) == ("tok", "123")
    for bad in ("", "pas du json", "{}", '{"access_token": "t"}', '{"access_token": "", "phone_number_id": "1"}'):
        with pytest.raises(credentials.CredentialsError):
            credentials.whatsapp_credentials(bad)


def test_add_channel_encrypts_credentials_and_creates_route(kora, owner_engine):
    with owner_engine.connect() as c:
        row = c.execute(text("SELECT tenant_id, channel_id FROM channel_routes WHERE type='whatsapp' AND external_id=:e"), {"e": PNID}).one()
        assert row.tenant_id == kora
        enc = c.execute(text("SELECT credentials_encrypted FROM channels WHERE id=:i"), {"i": row.channel_id}).scalar()
    assert TOKEN not in enc and json.loads(decrypt(enc)) == {"access_token": TOKEN, "phone_number_id": PNID}


def test_a_phone_number_cannot_belong_to_two_clients(kora, make_tenant, owner_engine):
    other, _ = make_tenant("autre", "PAGE2")
    with pytest.raises(IntegrityError):
        add_channel(owner_engine, tenant_id=other, channel_type="whatsapp", external_id=PNID, access_token="x")


def test_create_tenant_can_start_with_whatsapp(owner_engine):
    tenant_id, channel_id = create_tenant(owner_engine, name="Z", slug="z", sector=None, page_id="555", page_token="tk", channel_type="whatsapp")
    with tenant_session(tenant_id) as s:
        enc = s.execute(text("SELECT credentials_encrypted FROM channels WHERE id=:i"), {"i": channel_id}).scalar()
    assert credentials.whatsapp_credentials(decrypt(enc)) == ("tk", "555")


# --- webhook --------------------------------------------------------------------------------------------------------

def test_verification_handshake_uses_messenger_token_as_fallback():
    with TestClient(app) as c:
        ok = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "test_verify", "hub.challenge": "77"})
        assert ok.status_code == 200 and ok.text == "77"
        bad = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "faux", "hub.challenge": "77"})
        assert bad.status_code == 403
        assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "77"}).status_code == 403


def test_valid_signature_stores_message_once_with_contact_name(kora):
    with TestClient(app) as c:
        payload = build_payload(PNID, "22997000001", "Bonjour", wamid="wamid.1", name="Aïcha")
        assert _post(c, payload).status_code == 200
        assert _post(c, payload).status_code == 200  # renvoi par Meta
    with tenant_session(kora) as s:
        assert s.execute(text("SELECT text FROM messages")).scalars().all() == ["Bonjour"]
        assert s.execute(text("SELECT name, channel_user_id FROM contacts")).one() == ("Aïcha", "22997000001")


def test_invalid_signature_rejected_and_nothing_stored(kora):
    with TestClient(app) as c:
        assert _post(c, build_payload(PNID, "229", "x"), secret="mauvais").status_code == 403
    with tenant_session(kora) as s:
        assert s.execute(text("SELECT count(*) FROM messages")).scalar() == 0


def test_wrong_object_unknown_number_and_messenger_payload(kora):
    from scripts.send_test_webhook import build_payload as messenger_payload
    with TestClient(app) as c:
        assert _post(c, messenger_payload("PAGE1", "U1", "x")).status_code == 404       # un événement Messenger n'a rien à faire ici
        assert _post(c, build_payload("NUMERO_INCONNU", "229", "x")).status_code == 200  # canal inconnu : 200, rien d'enregistré
    with tenant_session(kora) as s:
        assert s.execute(text("SELECT count(*) FROM messages")).scalar() == 0


def test_failed_status_marks_the_outbound_message_only(kora, make_tenant):
    _in(wamid="wamid.in")
    with tenant_session(kora) as s:
        s.execute(text("""INSERT INTO messages (tenant_id, conversation_id, channel_id, direction, type, text, idempotency_key, delivery_status, external_id)
                          SELECT tenant_id, conversation_id, channel_id, 'out', 'text', 'Réponse', 'reply:x', 'sent', 'wamid.out' FROM messages LIMIT 1"""))

    def status(wamid, state):
        return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "statuses": [{"id": wamid, "status": state, "errors": [{"code": 131047, "title": "x"}]}]}}]}]}

    def delivery():
        return _rows(kora, "SELECT delivery_status FROM messages WHERE external_id = 'wamid.out'")[0].delivery_status

    with TestClient(app) as c:
        assert _post(c, status("wamid.out", "delivered")).status_code == 200
        assert delivery() == "sent"
        assert _post(c, status("wamid.inconnu", "failed")).status_code == 200
        assert delivery() == "sent"
        assert _post(c, status("wamid.out", "failed")).status_code == 200
        assert delivery() == "failed"


# --- envoi ----------------------------------------------------------------------------------------------------------

def _wa(handler):
    return WhatsAppSender(TOKEN, PNID, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_send_text_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.read()))
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT1"}]})

    assert _wa(handler).send_text("22997000001", "Bonjour") == "wamid.OUT1"
    assert seen["url"].endswith(f"/{PNID}/messages") and TOKEN not in seen["url"] and seen["auth"] == f"Bearer {TOKEN}"
    assert seen["body"] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "22997000001",
                            "type": "text", "text": {"preview_url": False, "body": "Bonjour"}}


def test_long_text_is_split_in_order():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.read())["text"]["body"])
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(bodies)}"}]})

    last = _wa(handler).send_text("229", ("mot " * 1500).strip())
    assert len(bodies) == 2 and all(len(b) <= 4000 for b in bodies) and last == "wamid.2"
    assert " ".join(bodies).split() == ["mot"] * 1500


@pytest.mark.parametrize("status,code,permanent", [
    (400, 131047, True),    # fenêtre de 24 h dépassée
    (401, 190, True),       # jeton expiré
    (400, 131030, True),    # numéro hors liste en mode test
    (400, 131056, False),   # limite de débit entre deux numéros
    (400, 130429, False),   # limite de débit
    (500, None, False), (429, None, False), (503, 133004, False),
])
def test_send_error_mapping(status, code, permanent):
    body = {"error": {"message": "boom", "code": code}} if code else {}
    with pytest.raises(SendError) as e:
        _wa(lambda r: httpx.Response(status, json=body)).send_text("229", "x")
    assert e.value.permanent is permanent and TOKEN not in str(e.value)


def test_transport_error_is_transient():
    def handler(req):
        raise httpx.ConnectError("down")
    with pytest.raises(SendError) as e:
        _wa(handler).send_text("229", "x")
    assert e.value.permanent is False


def test_invalid_credentials_are_a_permanent_error():
    with pytest.raises(SendError) as e:
        WhatsAppSender.from_credentials("pas du json")
    assert e.value.permanent is True


def test_get_sender_dispatch(monkeypatch):
    assert isinstance(get_sender("whatsapp", credentials.encode("whatsapp", "t", "1")), WhatsAppSender)
    monkeypatch.setenv("SEND_MODE", "console")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        assert isinstance(get_sender("whatsapp", "n'importe quoi"), ConsoleSender)
    finally:
        monkeypatch.delenv("SEND_MODE")
        get_settings.cache_clear()


def _audio_recorder():
    calls = []

    def handler(req: httpx.Request):
        calls.append((str(req.url), req.headers["content-type"], req.read()))
        return httpx.Response(200, json={"id": "MEDIA9"} if str(req.url).endswith("/media") else {"messages": [{"id": "wamid.AUD"}]})
    return calls, handler


def test_send_ogg_audio_uploads_then_sends_by_media_id():
    calls, handler = _audio_recorder()
    assert _wa(handler).send_audio("229", OGG, "audio/ogg") == "wamid.AUD"
    (up_url, up_type, up_body), (msg_url, _, msg_body) = calls
    assert up_url.endswith(f"/{PNID}/media") and up_type.startswith("multipart/form-data")
    assert b'name="messaging_product"' in up_body and b'filename="voice.ogg"' in up_body and OGG in up_body
    assert msg_url.endswith(f"/{PNID}/messages")
    assert json.loads(msg_body)["audio"] == {"id": "MEDIA9"} and json.loads(msg_body)["type"] == "audio"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent")
def test_console_wav_is_converted_to_ogg_opus_before_upload():
    calls, handler = _audio_recorder()
    wav = make_wav(1.0)
    assert _wa(handler).send_audio("229", wav, "audio/wav") == "wamid.AUD"
    upload_body = calls[0][2]
    assert b'filename="voice.ogg"' in upload_body and b"OggS" in upload_body and wav not in upload_body
    assert b"RIFF" not in upload_body.split(b"OggS", 1)[0][-200:]


def test_wav_conversion_without_ffmpeg_is_a_clear_permanent_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(SendError, match="ffmpeg") as e:
        _wa(lambda r: httpx.Response(200, json={})).send_audio("229", make_wav(), "audio/wav")
    assert e.value.permanent is True


def test_unsupported_audio_formats_are_refused():
    with pytest.raises(SendError, match="format audio"):
        _wa(lambda r: httpx.Response(200, json={})).send_audio("229", b"x", "audio/webm")
    with pytest.raises(MediaError):
        media_service.wav_to_ogg_opus(b"pas un wav" * 10)


# --- médias reçus ---------------------------------------------------------------------------------------------------

def _fetcher(handler):
    return HttpMediaFetcher(client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def _creds():
    return credentials.encode("whatsapp", TOKEN, PNID)


def test_whatsapp_media_is_resolved_then_downloaded_with_the_token():
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.headers.get("authorization")))
        if "graph.facebook.com" in str(req.url):
            return httpx.Response(200, json={"url": "https://lookaside.example/audio?x=1", "mime_type": "audio/ogg"})
        return httpx.Response(200, content=OGG)

    audio = _fetcher(handler).fetch(f"{MEDIA_PREFIX}MID42", token=_creds())
    assert (audio.content_type, audio.data) == ("audio/ogg", OGG)
    assert seen[0][0].endswith("/MID42") and seen[1][0].startswith("https://lookaside.example/")
    assert [a for _, a in seen] == [f"Bearer {TOKEN}"] * 2


@pytest.mark.parametrize("ref,token,fragment", [
    (f"{MEDIA_PREFIX}../etc/passwd", "ok", "invalide"),
    (f"{MEDIA_PREFIX}MID1", None, "identifiants"),
    (f"{MEDIA_PREFIX}MID1", "pas du json", "identifiants"),
])
def test_whatsapp_media_input_validation(ref, token, fragment):
    token = _creds() if token == "ok" else token
    with pytest.raises(MediaError, match=fragment):
        _fetcher(lambda r: httpx.Response(500)).fetch(ref, token=token)


def test_whatsapp_media_refusals():
    with pytest.raises(MediaError, match="refusée"):
        _fetcher(lambda r: httpx.Response(401)).fetch(f"{MEDIA_PREFIX}M1", token=_creds())
    with pytest.raises(MediaError, match="non HTTPS"):
        _fetcher(lambda r: httpx.Response(200, json={"url": "http://insecure.example/a"})).fetch(f"{MEDIA_PREFIX}M1", token=_creds())

    def html(req):
        return httpx.Response(200, json={"url": "https://x.example/a"}) if "graph" in str(req.url) else httpx.Response(200, content=b"<html>pas de l'audio</html>")
    with pytest.raises(MediaError, match="pas un audio"):
        _fetcher(html).fetch(f"{MEDIA_PREFIX}M1", token=_creds())


def test_messenger_urls_are_still_downloaded_without_credentials():
    seen = []

    def handler(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, content=OGG)
    assert _fetcher(handler).fetch("https://cdn.example/a.mp4").content_type == "audio/ogg"
    assert seen == [None]


# --- parcours complet avec le worker ----------------------------------------------------------------------------------

def test_whatsapp_message_gets_an_agent_reply_on_the_same_number(kora):
    _in("22997000001", "Vous êtes ouverts quand ?", "wamid.1")
    sender = FakeSender()
    rec = Recorder(sender)
    while run_once(FakeLLM(text_reply("Nous sommes ouverts 7j/7.")), rec):
        pass
    assert sender.sent == [("22997000001", "Nous sommes ouverts 7j/7.")]
    [(ctype, creds)] = rec.calls
    assert ctype == "whatsapp" and credentials.whatsapp_credentials(creds) == (TOKEN, PNID)
    [out] = _rows(kora, "SELECT delivery_status, external_id FROM messages WHERE direction = 'out'")
    assert (out.delivery_status, out.external_id) == ("sent", "mid_out_1")


def test_both_channels_of_one_client_stay_separate(kora):
    from app.channels.messenger import parse_payload as messenger_parse
    from scripts.send_test_webhook import build_payload as messenger_payload
    ingest(messenger_parse(messenger_payload("PAGE1", "PSID1", "Bonjour Messenger", mid="m1"))[0])
    _in("22997000001", "Bonjour WhatsApp", "wamid.1")
    assert len(_rows(kora, "SELECT 1 FROM conversations")) == 2
    sender = FakeSender()
    rec = Recorder(sender)
    llm = FakeLLM(text_reply("Bonjour !"), repeat_last=True)
    while run_once(llm, rec):
        pass
    assert sorted(c[0] for c in rec.calls) == ["messenger", "whatsapp"]
    assert sorted(r for r, _ in sender.sent) == ["22997000001", "PSID1"]


def test_incoming_whatsapp_voice_is_fetched_with_channel_token_then_transcribed(kora):
    _in("22997000001", "", "wamid.v1", kind="audio", media_id="MID7")
    fetcher, tr = FakeFetcher(media_service.Audio(OGG, "audio/ogg")), FakeTranscriber("Je voudrais la robe Sika")
    llm, sender = FakeLLM(text_reply("Avec plaisir !")), FakeSender()
    while run_once(llm, sender.factory(), tr, fetcher):
        pass
    assert fetcher.calls == [f"{MEDIA_PREFIX}MID7"]
    assert tr.bytes_calls == [(OGG, "audio/ogg")] and tr.url_calls == []
    assert "[Message vocal transcrit] Je voudrais la robe Sika" in llm.calls[0]["messages"][-1]["content"]
    assert _rows(kora, "SELECT content_type FROM message_media")[0].content_type == "audio/ogg"


def test_whatsapp_voice_without_download_never_falls_back_to_a_non_url(kora):
    _in("22997000001", "", "wamid.v2", kind="audio", media_id="MID8")
    fetcher = FakeFetcher(error=MediaError("expiré"))
    tr = FakeTranscriber()
    llm, sender = FakeLLM(text_reply("Pouvez-vous écrire votre demande ?")), FakeSender()
    while run_once(llm, sender.factory(), tr, fetcher):
        pass
    assert tr.url_calls == [] and tr.bytes_calls == []   # aucune tentative sur « wa-media:... »
    assert sender.sent[0][0] == "22997000001"


def test_console_label_and_conversation_list_for_whatsapp(kora):
    assert console_service.contact_label("Aïcha", "22997", "whatsapp") == "Aïcha"
    assert console_service.contact_label(None, "22997000001", "whatsapp") == "Client WhatsApp +22997000001"
    assert console_service.contact_label(None, "PSID1234", "messenger") == "Client Messenger …1234"
    _in("22997000001", "Bonjour", "wamid.1", name=None)
    with tenant_session(kora) as s:
        [conv] = console_service.list_conversations(s)
    assert conv["label"] == "Client WhatsApp +22997000001"
