"""Canal WhatsApp via Evolution API : parsing, secret du webhook, envoi. Aucun accès à la base ni au réseau."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.channels import evolution
from app.channels.credentials import encode
from app.channels.sender import EvolutionSender, SendError, get_sender
from app.config import get_settings
from app.main import app
from app.routers import webhooks
from app.services import media
from app.services.media import HttpMediaFetcher, MediaError

INSTANCE = "agent24"
CREDS = encode("whatsapp", "inutile", INSTANCE)


def payload(text="Bonjour", jid="22901970000@s.whatsapp.net", from_me=False, msg_id="3EB0A1", event="messages.upsert", message=None, **key):
    return {
        "event": event, "instance": INSTANCE,
        "data": {
            "key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id, **key},
            "pushName": "Aïcha",
            "message": message if message is not None else {"conversation": text},
            "messageTimestamp": 1763215200,
        },
    }


# --- parsing --------------------------------------------------------------------------------------------------------

def test_parse_text_message():
    [m] = evolution.parse_payload(payload())
    assert (m.channel_type, m.channel_external_id, m.channel_user_id) == ("whatsapp", INSTANCE, "22901970000")
    assert (m.type, m.text, m.media_ref) == ("text", "Bonjour", None)
    assert m.idempotency_key == "whatsapp:3EB0A1" and m.received_at.tzinfo is not None


def test_parse_extended_text_and_event_name_variants():
    p = payload(message={"extendedTextMessage": {"text": "Voir ce lien"}}, event="MESSAGES_UPSERT")
    [m] = evolution.parse_payload(p)
    assert m.text == "Voir ce lien"


def test_parse_unwraps_ephemeral_messages():
    [m] = evolution.parse_payload(payload(message={"ephemeralMessage": {"message": {"conversation": "Salut"}}}))
    assert m.text == "Salut"


def test_parse_voice_note_carries_a_downloadable_reference_other_media_do_not():
    [img] = evolution.parse_payload(payload(message={"imageMessage": {"caption": "Cette robe ?"}}))
    [aud] = evolution.parse_payload(payload(message={"audioMessage": {"seconds": 5, "ptt": True}}, msg_id="A2"))
    assert (img.type, img.text, img.media_ref) == ("image", "Cette robe ?", None)
    assert (aud.type, aud.text, aud.media_ref) == ("audio", None, f"{evolution.MEDIA_PREFIX}{INSTANCE}:A2")


def test_parse_ignores_own_messages_groups_status_reactions_and_other_events():
    assert evolution.parse_payload(payload(from_me=True)) == []
    assert evolution.parse_payload(payload(jid="120363000000@g.us")) == []
    assert evolution.parse_payload(payload(jid="status@broadcast")) == []
    assert evolution.parse_payload(payload(message={"reactionMessage": {"text": "👍"}})) == []
    assert evolution.parse_payload(payload(message={"protocolMessage": {"type": 0}})) == []
    assert evolution.parse_payload(payload(event="connection.update")) == []
    assert evolution.parse_payload({"event": "messages.upsert", "data": {}}) == []   # pas d'instance


def test_parse_lid_uses_alternate_number_when_given():
    [m] = evolution.parse_payload(payload(jid="9876543210@lid", remoteJidAlt="22901970000@s.whatsapp.net"))
    assert m.channel_user_id == "22901970000"
    [m] = evolution.parse_payload(payload(jid="9876543210@lid"))
    assert m.channel_user_id == "9876543210@lid"


def test_verify_secret():
    assert evolution.verify_secret("s3cret", "s3cret")
    assert not evolution.verify_secret("s3cret", "autre") and not evolution.verify_secret("s3cret", None)
    assert not evolution.verify_secret("", "") and not evolution.verify_secret("", None)   # non configuré = tout refusé


# --- webhook --------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "evolution_webhook_secret", "s3cret")
    ingested = []
    monkeypatch.setattr(webhooks, "ingest", lambda msg: ingested.append(msg) or type("R", (), {"status": "stored"})())
    c = TestClient(app)
    c.ingested = ingested
    return c


def test_webhook_rejects_missing_or_wrong_secret(client):
    assert client.post("/webhooks/evolution", json=payload()).status_code == 403
    assert client.post("/webhooks/evolution", json=payload(), headers={evolution.SECRET_HEADER: "faux"}).status_code == 403
    assert client.ingested == []


def test_webhook_ingests_with_secret(client):
    r = client.post("/webhooks/evolution", json=payload(), headers={evolution.SECRET_HEADER: "s3cret"})
    assert r.status_code == 200 and len(client.ingested) == 1 and client.ingested[0].text == "Bonjour"


def test_webhook_bad_json_is_400(client):
    r = client.post("/webhooks/evolution", content=b"pas du json", headers={evolution.SECRET_HEADER: "s3cret"})
    assert r.status_code == 400


# --- envoi ----------------------------------------------------------------------------------------------------------

def sender(handler, monkeypatch, key="EVO_KEY"):
    monkeypatch.setattr(get_settings(), "evolution_api_key", key)
    monkeypatch.setattr(get_settings(), "evolution_api_url", "http://evo:8080/")
    return EvolutionSender(CREDS, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_send_text_posts_to_instance_with_apikey(monkeypatch):
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.headers["apikey"], json.loads(req.content)))
        return httpx.Response(201, json={"key": {"id": "WAID1"}})

    assert sender(handler, monkeypatch).send_text("22901970000", "Bonjour") == "WAID1"
    assert seen == [("http://evo:8080/message/sendText/agent24", "EVO_KEY", {"number": "22901970000", "text": "Bonjour"})]


def test_send_text_splits_long_messages(monkeypatch):
    bodies = []
    s = sender(lambda req: bodies.append(json.loads(req.content)["text"]) or httpx.Response(201, json={}), monkeypatch)
    s.send_text("229", ("mot " * 1200).strip())
    assert len(bodies) >= 2 and all(len(b) <= 1900 for b in bodies)


@pytest.mark.parametrize("status,permanent", [(400, True), (401, True), (404, True), (429, False), (500, False), (503, False)])
def test_send_errors_are_classified(monkeypatch, status, permanent):
    with pytest.raises(SendError) as e:
        sender(lambda req: httpx.Response(status, text="x"), monkeypatch).send_text("229", "Salut")
    assert e.value.permanent is permanent


def test_send_network_error_is_retryable(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("refusé")
    with pytest.raises(SendError) as e:
        sender(boom, monkeypatch).send_text("229", "Salut")
    assert e.value.permanent is False


def test_send_without_api_key_is_permanent(monkeypatch):
    with pytest.raises(SendError) as e:
        sender(lambda req: httpx.Response(201), monkeypatch, key="").send_text("229", "Salut")
    assert e.value.permanent is True


def test_bad_credentials_and_unsupported_media(monkeypatch):
    with pytest.raises(SendError):
        EvolutionSender("pas du json")
    s = sender(lambda req: httpx.Response(201), monkeypatch)
    with pytest.raises(SendError):
        s.send_audio("229", b"x", "audio/wav")
    with pytest.raises(SendError):
        s.send_image("229", b"x", "image/jpeg")


def test_get_sender_routes_whatsapp_to_evolution(monkeypatch):
    monkeypatch.setattr(get_settings(), "send_mode", "messenger")
    assert isinstance(get_sender("whatsapp", CREDS), EvolutionSender)


# --- téléchargement des vocaux --------------------------------------------------------------------------------------
import base64  # noqa: E402

OGG = b"OggS\x00\x02" + b"\x01" * 64
REF = f"{evolution.MEDIA_PREFIX}{INSTANCE}:3EB0A1"


def fetcher(handler, monkeypatch, key="EVO_KEY"):
    monkeypatch.setattr(get_settings(), "evolution_api_key", key)
    monkeypatch.setattr(get_settings(), "evolution_api_url", "http://evo:8080")
    monkeypatch.setattr(media.time, "sleep", lambda _: None)
    return HttpMediaFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_fetch_voice_note_from_evolution(monkeypatch):
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.headers["apikey"], json.loads(req.content)))
        return httpx.Response(201, json={"mediaType": "audioMessage", "mimetype": "audio/ogg; codecs=opus", "base64": base64.b64encode(OGG).decode()})

    audio = fetcher(handler, monkeypatch).fetch(REF)
    assert (audio.data, audio.content_type) == (OGG, "audio/ogg")
    assert seen == [("http://evo:8080/chat/getBase64FromMediaMessage/agent24", "EVO_KEY",
                     {"message": {"key": {"id": "3EB0A1"}}, "convertToMp4": False})]


def test_fetch_retries_while_evolution_has_not_stored_the_message(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(404, text="not found")
        return httpx.Response(201, json={"base64": "data:audio/ogg;base64," + base64.b64encode(OGG).decode()})

    assert fetcher(handler, monkeypatch).fetch(REF).data == OGG and len(calls) == 3


def test_fetch_gives_up_after_three_tries_with_a_media_error(monkeypatch):
    calls = []
    with pytest.raises(MediaError):
        fetcher(lambda req: calls.append(1) or httpx.Response(500), monkeypatch).fetch(REF)
    assert len(calls) == 3


def test_fetch_rejects_non_audio_bad_reference_and_missing_key(monkeypatch):
    ok_html = lambda req: httpx.Response(201, json={"base64": base64.b64encode(b"<html>pas un audio</html>").decode()})  # noqa: E731
    with pytest.raises(MediaError):
        fetcher(ok_html, monkeypatch).fetch(REF)
    with pytest.raises(MediaError):
        fetcher(ok_html, monkeypatch).fetch(f"{evolution.MEDIA_PREFIX}sans-identifiant")
    with pytest.raises(MediaError):
        fetcher(ok_html, monkeypatch, key="").fetch(REF)


def test_regular_https_urls_still_go_through_the_normal_download(monkeypatch):
    f = fetcher(lambda req: httpx.Response(200, content=OGG), monkeypatch)
    assert f.fetch("https://cdn.example.com/voice.ogg").data == OGG
    with pytest.raises(MediaError):
        f.fetch("http://cdn.example.com/voice.ogg")   # toujours refusé : HTTPS obligatoire
