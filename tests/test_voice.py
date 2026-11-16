"""Vocaux : archivage à la réception, lecture dans la console, envoi d'un vocal par l'équipe."""
import json
import struct
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.channels.sender import MessengerSender, SendError
from app.config import get_settings
from app.console import auth
from app.db import tenant_session
from app.main import app
from app.services import console as console_service
from app.services import media as media_service
from app.services.media import Audio, HttpMediaFetcher, MediaError, parse_wav, sniff_audio
from app.services.transcribe import TranscriptionError
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeLLM, FakeSender, text_reply
from tests.seed import seed_catalog
from tests.test_agent import _drain, _in, _rows

KORA = json.loads(Path("tenants/maison-kora.json").read_text(encoding="utf-8"))
PASSWORD = "mot-de-passe-test"
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x01" * 64


def make_wav(seconds=1.0, rate=16000, channels=1, bits=16, fmt=1):
    n = int(seconds * rate) * channels * (bits // 8)
    hdr = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, fmt, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    return hdr + b"data" + struct.pack("<I", n) + b"\x00" * n


# --- reconnaissance et validation des formats ----------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    (M4A, "audio/mp4"), (make_wav(), "audio/wav"), (b"OggS" + b"\0" * 20, "audio/ogg"), (b"\x1a\x45\xdf\xa3" + b"\0" * 20, "audio/webm"),
    (b"fLaC" + b"\0" * 20, "audio/flac"), (b"ID3\x03" + b"\0" * 20, "audio/mpeg"), (b"\xff\xfb\x90\x00" + b"\0" * 20, "audio/mpeg"),
    (b"<html><script>alert(1)</script></html>", None), (b"\x89PNG\r\n\x1a\n" + b"\0" * 20, None), (b"short", None), (b"", None),
])
def test_audio_is_recognised_by_content_not_by_header(data, expected):
    assert sniff_audio(data) == expected


def test_valid_wav_is_described():
    assert parse_wav(make_wav(2.0)) == {"seconds": 2.0, "rate": 16000, "channels": 1}


@pytest.mark.parametrize("data,fragment", [
    (b"not a wav at all" * 10, "invalide"), (make_wav(0.1), "trop court"), (make_wav(131), "trop long"),
    (make_wav(1, bits=8), "non pris en charge"), (make_wav(1, fmt=3), "non pris en charge"), (make_wav(1, rate=4000), "non pris en charge"),
    (make_wav(1)[:30], "invalide"),
])
def test_invalid_wav_is_refused(data, fragment):
    with pytest.raises(MediaError, match=fragment):
        parse_wav(data)


def _fetcher(handler, **kw):
    return HttpMediaFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_fetcher_downloads_and_types_by_content():
    got = _fetcher(lambda r: httpx.Response(200, content=M4A, headers={"content-type": "text/html"})).fetch("https://cdn.example/a.mp4")
    assert got.content_type == "audio/mp4" and got.data == M4A          # l'en-tête mensonger est ignoré


@pytest.mark.parametrize("url,handler,fragment", [
    ("http://cdn.example/a", lambda r: httpx.Response(200, content=M4A), "HTTPS"),
    ("https://cdn.example/a", lambda r: httpx.Response(404), "refusé"),
    ("https://cdn.example/a", lambda r: httpx.Response(200, content=b"<html>pas de l'audio" + b" " * 30), "pas un audio"),
    ("https://cdn.example/a", lambda r: httpx.Response(200, content=M4A * 1000), "volumineux"),
])
def test_fetcher_refuses_bad_downloads(url, handler, fragment):
    with pytest.raises(MediaError, match=fragment):
        _fetcher(handler, max_bytes=2000).fetch(url)


# --- réception : archivage + transcription ----------------------------------------------------------------

@pytest.fixture
def tenant(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)
    seed_catalog(tenant_id, KORA)
    return tenant_id


class FakeFetcher:
    def __init__(self, audio=None, error=None):
        self.audio, self.error, self.calls = audio, error, []

    def fetch(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.audio


class FakeTranscriber:
    def __init__(self, text="Je voudrais la robe Sika", error=None):
        self.text, self.error, self.bytes_calls, self.url_calls = text, error, [], []

    def transcribe_bytes(self, audio, content_type, source_url=""):
        self.bytes_calls.append((audio, content_type))
        if self.error:
            raise self.error
        return self.text

    def transcribe_url(self, url):
        self.url_calls.append(url)
        if self.error:
            raise self.error
        return self.text


def _run(llm, sender, tr=None, fetcher=None):
    from app.worker import run_once
    while run_once(llm, sender.factory(), tr, fetcher):
        pass


def test_incoming_voice_is_archived_then_transcribed_from_the_same_bytes(tenant):
    _in("U1", "", "m1", kind="audio")
    tr, fetcher = FakeTranscriber(), FakeFetcher(Audio(M4A, "audio/mp4"))
    _run(FakeLLM(text_reply("Avec plaisir !")), FakeSender(), tr, fetcher)
    assert fetcher.calls == ["https://cdn.example/x"]
    assert tr.bytes_calls == [(M4A, "audio/mp4")] and tr.url_calls == []     # un seul téléchargement
    [m] = _rows(tenant, "SELECT content_type, size, data FROM message_media")
    assert (m.content_type, m.size, bytes(m.data)) == ("audio/mp4", len(M4A), M4A)
    assert _rows(tenant, "SELECT text FROM messages WHERE direction = 'in'")[0].text == "Je voudrais la robe Sika"


def test_download_failure_still_transcribes_through_the_url(tenant):
    _in("U1", "", "m1", kind="audio")
    tr = FakeTranscriber()
    _run(FakeLLM(text_reply("Ok")), FakeSender(), tr, FakeFetcher(error=MediaError("expiré")))
    assert tr.url_calls == ["https://cdn.example/x"] and _rows(tenant, "SELECT 1 FROM message_media") == []


def test_transcription_failure_keeps_the_archived_audio(tenant):
    _in("U1", "", "m1", kind="audio")
    llm = FakeLLM(text_reply("Pourriez-vous écrire ?"))
    _run(llm, FakeSender(), FakeTranscriber(error=TranscriptionError("panne")), FakeFetcher(Audio(M4A, "audio/mp4")))
    assert len(_rows(tenant, "SELECT 1 FROM message_media")) == 1
    assert "message vocal" in llm.calls[0]["messages"][0]["content"]


# --- console : lecture et envoi -----------------------------------------------------------------------------

@pytest.fixture
def client(tenant, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "console_password", PASSWORD)
    monkeypatch.setattr(s, "console_tenant_id", str(tenant))
    auth.limiter._hits.clear()
    with TestClient(app, follow_redirects=False) as c:
        c.post("/console/login", content=urlencode({"password": PASSWORD}), headers={"Content-Type": "application/x-www-form-urlencoded"})
        yield c


def _voice_message(tenant, transcript="Bonjour, la robe Sika ?"):
    _in("U1", "", "m1", kind="audio")
    mid = _rows(tenant, "SELECT id FROM messages")[0].id
    with tenant_session(tenant) as s:
        media_service.store(s, tenant, mid, Audio(M4A, "audio/mp4"))
        if transcript:
            s.execute(text("UPDATE messages SET text = :t"), {"t": transcript})
    return mid, _rows(tenant, "SELECT id FROM conversations")[0].id


def test_console_plays_incoming_voice_with_its_transcript(client, tenant):
    mid, conv = _voice_message(tenant)
    page = client.get(f"/console/conversations/{conv}").text
    assert f'src="/console/media/{mid}"' in page and "<audio" in page and "Bonjour, la robe Sika ?" in page
    j = client.get(f"/console/api/conversations/{conv}/messages").json()["messages"][0]
    assert j["audio_url"] == f"/console/media/{mid}" and j["text"] == "Bonjour, la robe Sika ?" and j["day_label"] == "Aujourd'hui"
    assert "preview" not in j and "🎤" not in page.split('id="msgs"')[1].split("</section>")[0]


def test_media_endpoint_requires_login_and_serves_safely(client, tenant):
    mid, _ = _voice_message(tenant)
    r = client.get(f"/console/media/{mid}")
    assert r.status_code == 200 and r.content == M4A and r.headers["content-type"] == "audio/mp4"
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["accept-ranges"] == "bytes" and "private" in r.headers["cache-control"]
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get(f"/console/media/{mid}").status_code == 303
    assert client.get("/console/media/00000000-0000-0000-0000-000000000000").status_code == 404


@pytest.mark.parametrize("header,status,expected", [
    ("bytes=0-9", 206, M4A[:10]), ("bytes=10-", 206, M4A[10:]), ("bytes=-5", 206, M4A[-5:]),
    (f"bytes=0-{10**6}", 206, M4A), (f"bytes={10**6}-", 416, None), ("bytes=abc", 200, M4A),
])
def test_media_supports_range_requests(client, tenant, header, status, expected):
    mid, _ = _voice_message(tenant)
    r = client.get(f"/console/media/{mid}", headers={"Range": header})
    assert r.status_code == status
    if expected is not None:
        assert r.content == expected
    if status == 206:
        assert r.headers["content-range"].startswith("bytes ")


def test_media_of_another_tenant_is_not_reachable(client, tenant, make_tenant):
    other, _ = make_tenant("autre", "P2")
    from app.channels.messenger import parse_payload
    from app.services.ingest import ingest
    from tests.test_agent import build_payload
    payload = build_payload("P2", "UX", "", mid="mx")
    m = payload["entry"][0]["messaging"][0]["message"]
    m.pop("text"), m.update(attachments=[{"type": "audio", "payload": {"url": "https://cdn.example/y"}}])
    ingest(parse_payload(payload)[0])
    foreign = _rows(other, "SELECT id FROM messages")[0].id
    with tenant_session(other) as s:
        media_service.store(s, other, foreign, Audio(M4A, "audio/mp4"))
    assert client.get(f"/console/media/{foreign}").status_code == 404


def test_human_voice_is_sent_stored_playable_and_pauses_agent(client, tenant, monkeypatch):
    _in("U1", "Bonjour", "m1")
    conv = _rows(tenant, "SELECT id FROM conversations")[0].id
    sender = FakeSender()
    monkeypatch.setattr(console_service, "get_sender", sender.factory())
    wav = make_wav(1.5)
    r = client.post(f"/console/api/conversations/{conv}/voice", content=wav, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200 and r.json()["message"]["audio_url"].startswith("/console/media/") and r.json()["status"] == "human"
    assert sender.sent_audio == [(sender.sent_audio[0][0], wav, "audio/wav")]
    [m] = _rows(tenant, "SELECT type, text, delivery_status, raw FROM messages WHERE direction = 'out'")
    assert (m.type, m.delivery_status, m.raw, m.text) == ("audio", "sent", {"author": "human"}, console_service.VOICE_PLACEHOLDER)
    assert _rows(tenant, "SELECT status FROM conversations")[0].status == "human"
    played = client.get(r.json()["message"]["audio_url"])
    assert played.status_code == 200 and played.content == wav and played.headers["content-type"] == "audio/wav"
    page = client.get(f"/console/conversations/{conv}").text
    assert "Vous" in page and "<audio" in page and console_service.VOICE_PLACEHOLDER not in page


def test_voice_upload_validation(client, tenant):
    _in("U1", "Bonjour", "m1")
    conv = _rows(tenant, "SELECT id FROM conversations")[0].id
    url = f"/console/api/conversations/{conv}/voice"
    assert client.post(url, content=make_wav(), headers={"Content-Type": "application/json"}).status_code == 415
    assert client.post(url, content=b"RIFFxxxx", headers={"Content-Type": "audio/wav"}).status_code == 400
    assert client.post(url, content=make_wav(0.1), headers={"Content-Type": "audio/wav"}).status_code == 400
    assert client.post(url, content=b"\0" * 8_100_000, headers={"Content-Type": "audio/wav"}).status_code == 413
    assert client.post("/console/api/conversations/00000000-0000-0000-0000-000000000000/voice", content=make_wav(),
                       headers={"Content-Type": "audio/wav"}).status_code == 404
    assert _rows(tenant, "SELECT 1 FROM messages WHERE direction = 'out'") == []
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.post(url, content=make_wav(), headers={"Content-Type": "audio/wav"}).status_code == 401


def test_failed_voice_send_is_kept_flagged_and_reported(client, tenant, monkeypatch):
    _in("U1", "Bonjour", "m1")
    conv = _rows(tenant, "SELECT id FROM conversations")[0].id
    monkeypatch.setattr(console_service, "get_sender", FakeSender(fail_times=1, permanent=True).factory())
    r = client.post(f"/console/api/conversations/{conv}/voice", content=make_wav(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 502 and "24 h" in r.json()["detail"]
    assert _rows(tenant, "SELECT delivery_status FROM messages WHERE direction = 'out'")[0].delivery_status == "failed"
    assert len(_rows(tenant, "SELECT 1 FROM message_media")) == 1


def test_agent_history_never_contains_an_empty_voice_message(client, tenant, monkeypatch):
    _in("U1", "Bonjour", "m1")
    conv = _rows(tenant, "SELECT id FROM conversations")[0].id
    monkeypatch.setattr(console_service, "get_sender", FakeSender().factory())
    client.post(f"/console/api/conversations/{conv}/voice", content=make_wav(), headers={"Content-Type": "audio/wav"})
    client.post(f"/console/api/conversations/{conv}/status", json={"status": "agent"})
    _in("U1", "Merci pour le vocal", "m2")
    llm, sender = FakeLLM(text_reply("Avec plaisir")), FakeSender()
    _drain(llm, sender)
    contents = [m["content"] for m in llm.calls[0]["messages"]]
    assert all(c for c in contents) and console_service.VOICE_PLACEHOLDER in contents


# --- envoi vers Messenger -----------------------------------------------------------------------------------------

def _messenger(handler):
    return MessengerSender("PAGE_TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_messenger_audio_upload_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(auth=req.headers["authorization"], ctype=req.headers["content-type"], body=req.read(), url=str(req.url))
        return httpx.Response(200, json={"recipient_id": "U1", "message_id": "mid.1"})

    wav = make_wav()
    assert _messenger(handler).send_audio("U1", wav, "audio/wav") == "mid.1"
    assert seen["auth"] == "Bearer PAGE_TOKEN" and seen["ctype"].startswith("multipart/form-data") and "PAGE_TOKEN" not in seen["url"]
    body = seen["body"]
    assert b'name="recipient"' in body and b'{"id": "U1"}' in body
    assert b'"type": "audio"' in body and b'name="messaging_type"' in body and b"RESPONSE" in body
    assert b'filename="voice.wav"' in body and wav in body


@pytest.mark.parametrize("status,permanent", [(400, True), (403, True), (500, False), (429, False)])
def test_messenger_audio_error_mapping(status, permanent):
    with pytest.raises(SendError) as e:
        _messenger(lambda r: httpx.Response(status, text="err")).send_audio("U1", make_wav(), "audio/wav")
    assert e.value.permanent is permanent


def test_messenger_refuses_unsupported_audio_format():
    with pytest.raises(SendError, match="format audio"):
        _messenger(lambda r: httpx.Response(200, json={})).send_audio("U1", b"x", "audio/webm")
