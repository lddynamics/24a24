"""Transcription des messages vocaux (Whisper via une API compatible OpenAI, Groq par défaut).

Le client envoie un vocal -> Meta fournit une URL temporaire -> on télécharge l'audio, on le
transcrit, et le texte est enregistré dans le message. L'agent répond toujours par écrit.
Aucune transaction de base de données ne doit être ouverte pendant ces appels réseau.
"""
import logging
import mimetypes
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# Extensions acceptées par l'API ; le nom de fichier sert à identifier le format.
_EXT_BY_TYPE = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/webm": "webm",
    "audio/flac": "flac", "video/mp4": "mp4", "video/webm": "webm",
}
_ALLOWED_EXT = {"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "flac", "ogg"}


class TranscriptionError(Exception):
    pass


class Transcriber(Protocol):
    def transcribe_url(self, url: str) -> str | None:
        """Télécharge l'audio à `url` et retourne son texte (None si aucun texte reconnu)."""

    def transcribe_bytes(self, audio: bytes, content_type: str | None, source_url: str = "") -> str | None:
        """Transcrit un audio déjà téléchargé (None si aucun texte reconnu)."""


def _filename(url: str, content_type: str | None) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = _EXT_BY_TYPE.get(ctype)
    if not ext:
        guessed = mimetypes.guess_extension(ctype) if ctype else None
        path_ext = urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else ""
        ext = path_ext if path_ext in _ALLOWED_EXT else (guessed or "").lstrip(".")
    if ext not in _ALLOWED_EXT:
        ext = "mp4"  # valeur par défaut raisonnable pour les vocaux Messenger
    return f"voice.{ext}"


class WhisperTranscriber:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "whisper-large-v3-turbo",
        language: str = "fr",
        max_bytes: int = 10_000_000,
        endpoint: str = GROQ_URL,
        client: httpx.Client | None = None,
    ):
        self._key, self._model, self._language = api_key, model, language
        self._max_bytes, self._endpoint = max_bytes, endpoint
        self._client = client or httpx.Client(timeout=30, follow_redirects=True)

    def _download(self, url: str) -> tuple[bytes, str | None]:
        if urlparse(url).scheme != "https":
            raise TranscriptionError("URL audio non HTTPS")
        try:
            r = self._client.get(url)
        except httpx.HTTPError as e:
            raise TranscriptionError(f"téléchargement impossible: {type(e).__name__}") from e
        if r.status_code != 200:
            raise TranscriptionError(f"téléchargement refusé ({r.status_code})")
        if len(r.content) > self._max_bytes:
            raise TranscriptionError("audio trop volumineux")
        if not r.content:
            raise TranscriptionError("audio vide")
        return r.content, r.headers.get("content-type")

    def transcribe_url(self, url: str) -> str | None:
        audio, ctype = self._download(url)
        return self.transcribe_bytes(audio, ctype, url)

    def transcribe_bytes(self, audio: bytes, content_type: str | None, source_url: str = "") -> str | None:
        url, ctype = source_url, content_type
        data = {"model": self._model, "response_format": "json", "temperature": "0"}
        if self._language:
            data["language"] = self._language
        try:
            r = self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._key}"},
                data=data,
                files={"file": (_filename(url, ctype), audio)},
            )
        except httpx.HTTPError as e:
            raise TranscriptionError(f"API de transcription injoignable: {type(e).__name__}") from e
        if r.status_code != 200:
            # Le corps peut contenir un message d'erreur utile, jamais le contenu audio.
            raise TranscriptionError(f"API de transcription: {r.status_code} {r.text[:150]}")
        text = (r.json().get("text") or "").strip()
        return text or None


def build_transcriber() -> Transcriber | None:
    s = get_settings()
    if not s.groq_api_key:
        return None
    return WhisperTranscriber(
        s.groq_api_key, model=s.transcription_model, language=s.transcription_language, max_bytes=s.transcription_max_bytes
    )
