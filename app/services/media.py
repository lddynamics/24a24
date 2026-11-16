"""Médias de messages : téléchargement des vocaux reçus, validation, archivage, relecture par la console.

On ne fait confiance ni à l'en-tête Content-Type ni à l'extension : le format est reconnu aux premiers octets du
fichier, et un contenu qui n'est pas de l'audio n'est jamais stocké (donc jamais resservi par la console).
"""
import base64
import binascii
import logging
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services import images as image_service

log = logging.getLogger(__name__)

MAX_INBOUND_BYTES = 10_000_000
EVOLUTION_PREFIX = "evo-media:"   # référence d'un média WhatsApp reçu via Evolution : evo-media:<instance>:<id du message>
EVOLUTION_TRIES = 3             # Evolution peut ne pas avoir encore enregistré le message quand le webhook part
MAX_VOICE_SECONDS = 130
EXT = {"audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/wav": "wav", "audio/webm": "webm",
       "audio/flac": "flac", "audio/aac": "aac", "image/jpeg": "jpg"}


class MediaError(Exception):
    pass


@dataclass
class Audio:
    """Média archivé d'un message : un audio, ou (pour les images) un JPEG normalisé. Le nom est historique."""
    data: bytes
    content_type: str


Media = Audio


def sniff_audio(data: bytes) -> str | None:
    """Type MIME reconnu d'après les premiers octets, ou None si ce n'est pas de l'audio reconnu."""
    h = data[:16]
    if len(h) < 12:
        return None
    if h[4:8] == b"ftyp":
        return "audio/mp4"                       # m4a / mp4 (vocaux Messenger)
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "audio/wav"
    if h[:4] == b"OggS":
        return "audio/ogg"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if h[:4] == b"fLaC":
        return "audio/flac"
    if h[:3] == b"ID3" or (h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06) != 0x00):
        return "audio/aac" if (h[1] & 0xF6) == 0xF0 else "audio/mpeg"   # ADTS (AAC) ou MPEG audio
    return None


def parse_wav(data: bytes) -> dict:
    """Valide un WAV PCM 16 bits (celui que produit la console) et retourne sa description. Lève MediaError sinon."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaError("Format audio invalide (WAV attendu).")
    pos, fmt, data_len = 12, None, None
    while pos + 8 <= len(data):
        chunk_id, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt " and size >= 16 and body + 16 <= len(data):
            fmt = struct.unpack("<HHIIHH", data[body:body + 16])
        elif chunk_id == b"data":
            data_len = min(size, len(data) - body)
            break
        pos = body + size + (size & 1)
    if fmt is None or data_len is None:
        raise MediaError("Format audio invalide (en-tête WAV incomplet).")
    audio_format, channels, rate, _byte_rate, _align, bits = fmt
    if audio_format != 1 or bits != 16 or channels not in (1, 2) or not 8000 <= rate <= 48000:
        raise MediaError("Format audio non pris en charge (PCM 16 bits, 8 à 48 kHz).")
    seconds = data_len / (rate * channels * 2)
    if seconds < 0.5:
        raise MediaError("Enregistrement trop court.")
    if seconds > MAX_VOICE_SECONDS:
        raise MediaError(f"Enregistrement trop long ({MAX_VOICE_SECONDS} s maximum).")
    return {"seconds": seconds, "rate": rate, "channels": channels}


class HttpMediaFetcher:
    """Télécharge le média (audio ou image) d'un message entrant depuis l'URL (temporaire) fournie par la plateforme."""

    def __init__(self, max_bytes: int = MAX_INBOUND_BYTES, client: httpx.Client | None = None):
        self._max = max_bytes
        self._client = client or httpx.Client(timeout=30, follow_redirects=True)

    def _download_evolution(self, ref: str, what: str) -> bytes:
        """Média d'un message WhatsApp reçu via Evolution : l'API le renvoie en base64 (elle gère le déchiffrement WhatsApp)."""
        try:
            instance, message_id = ref[len(EVOLUTION_PREFIX):].split(":", 1)
        except ValueError:
            raise MediaError("référence de média Evolution invalide") from None
        s = get_settings()
        if not s.evolution_api_key:
            raise MediaError("EVOLUTION_API_KEY non configurée")
        url = f"{s.evolution_api_url.rstrip('/')}/chat/getBase64FromMediaMessage/{quote(instance, safe='')}"
        last = "inconnu"
        for attempt in range(EVOLUTION_TRIES):
            if attempt:
                time.sleep(2)
            try:
                r = self._client.post(url, headers={"apikey": s.evolution_api_key},
                                      json={"message": {"key": {"id": message_id}}, "convertToMp4": False})
            except httpx.HTTPError as e:
                last = type(e).__name__
                continue
            if r.status_code >= 400:
                last = f"evolution {r.status_code}"
                continue
            try:
                encoded = (r.json() or {}).get("base64") or ""
                data = base64.b64decode(encoded.split(",", 1)[-1], validate=False)
            except (ValueError, binascii.Error, AttributeError):
                last = "réponse illisible"
                continue
            if not data:
                last = "média vide"
                continue
            if len(data) > self._max:
                raise MediaError(f"{what} trop volumineux")
            return data
        raise MediaError(f"téléchargement impossible via Evolution ({last})")

    def _download(self, url: str, what: str) -> bytes:
        if url.startswith(EVOLUTION_PREFIX):
            return self._download_evolution(url, what)
        if urlparse(url).scheme != "https":
            raise MediaError(f"URL {what} non HTTPS")
        buf = bytearray()
        try:
            with self._client.stream("GET", url) as r:
                if r.status_code != 200:
                    raise MediaError(f"téléchargement refusé ({r.status_code})")
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self._max:
                        raise MediaError(f"{what} trop volumineux")
        except httpx.HTTPError as e:
            raise MediaError(f"téléchargement impossible: {type(e).__name__}") from e
        return bytes(buf)

    def fetch(self, url: str) -> Audio:
        data = self._download(url, "audio")
        mime = sniff_audio(data)
        if mime is None:
            raise MediaError("le fichier reçu n'est pas un audio reconnu")
        return Audio(data, mime)

    def fetch_image(self, url: str) -> Media:
        """Image reçue, ré-encodée en JPEG (taille bornée, métadonnées retirées) : c'est cette copie qu'on archive."""
        data = self._download(url, "image")
        if image_service.sniff_image(data) is None:
            raise MediaError("le fichier reçu n'est pas une image reconnue")
        try:
            jpeg, mime = image_service.normalize_image(data)
        except image_service.ImageError as e:
            raise MediaError(str(e)) from e
        return Media(jpeg, mime)


def exists(s: Session, message_id: UUID) -> bool:
    return s.execute(text("SELECT 1 FROM message_media WHERE message_id = :m"), {"m": message_id}).first() is not None


def store(s: Session, tenant_id: UUID, message_id: UUID, audio: Audio) -> None:
    s.execute(
        text("""INSERT INTO message_media (tenant_id, message_id, content_type, size, data)
                VALUES (:t, :m, :ct, :size, :data) ON CONFLICT (message_id) DO NOTHING"""),
        {"t": tenant_id, "m": message_id, "ct": audio.content_type, "size": len(audio.data), "data": audio.data},
    )


def load(s: Session, message_id: UUID) -> Audio | None:
    r = s.execute(text("SELECT content_type, data FROM message_media WHERE message_id = :m"), {"m": message_id}).first()
    return Audio(bytes(r.data), r.content_type) if r else None


def description(s: Session, message_id: UUID) -> str | None:
    return s.execute(text("SELECT description FROM message_media WHERE message_id = :m"), {"m": message_id}).scalar()


def set_description(s: Session, message_id: UUID, value: str) -> None:
    s.execute(text("UPDATE message_media SET description = :d WHERE message_id = :m"), {"d": value, "m": message_id})
