"""Envoi des réponses vers les canaux."""
import json
import logging
from typing import Protocol
from urllib.parse import quote

import httpx

from app.channels.credentials import CredentialsError, whatsapp_credentials
from app.config import get_settings

log = logging.getLogger(__name__)
MAX_CHARS = 1900  # Messenger limite un message à 2000 caractères


class SendError(Exception):
    def __init__(self, message: str, *, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


class Sender(Protocol):
    def send_text(self, recipient_id: str, text: str) -> str | None:
        """Envoie le texte ; retourne l'identifiant du message côté plateforme."""

    def send_audio(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        """Envoie un message vocal (fichier audio) ; retourne l'identifiant du message côté plateforme."""

    def send_image(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        """Envoie une image (JPEG) ; retourne l'identifiant du message côté plateforme."""


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Découpe en morceaux <= limit, de préférence aux fins de paragraphe, de ligne, puis d'espace."""
    text, chunks = text.strip(), []
    while len(text) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, 0, limit)
            if cut >= limit // 2:
                break
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


class ConsoleSender:
    """Développement : affiche la réponse au lieu de l'envoyer."""

    def send_text(self, recipient_id: str, text: str) -> str | None:
        print(f"\n[console] -> {recipient_id}:\n{text}\n")
        return "console"


    def send_audio(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        print(f"\n[console] -> {recipient_id}: [vocal {len(data)} octets, {content_type}]\n")
        return "console"


    def send_image(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        print(f"\n[console] -> {recipient_id}: [image {len(data)} octets, {content_type}]\n")
        return "console"


class MessengerSender:
    def __init__(self, page_token: str, client: httpx.Client | None = None):
        self._token = page_token
        self._client = client or httpx.Client(timeout=15)

    def send_text(self, recipient_id: str, text: str) -> str | None:
        s = get_settings()
        url = f"https://graph.facebook.com/{s.graph_api_version}/me/messages"
        last_id = None
        for chunk in split_text(text):
            try:
                r = self._client.post(
                    url,
                    headers={"Authorization": f"Bearer {self._token}"},  # jeton hors URL (donc hors logs)
                    json={
                        "recipient": {"id": recipient_id},
                        "messaging_type": "RESPONSE",
                        "message": {"text": chunk},
                    },
                )
            except httpx.TransportError as e:
                raise SendError(f"réseau: {type(e).__name__}", permanent=False) from e
            if r.status_code >= 500 or r.status_code == 429:
                raise SendError(f"meta {r.status_code}", permanent=False)
            if r.status_code >= 400:
                # 4xx : jeton invalide, fenêtre de 24 h dépassée, utilisateur bloqué... inutile de réessayer.
                raise SendError(f"meta {r.status_code}: {r.text[:200]}", permanent=True)
            last_id = r.json().get("message_id")
        return last_id

    def send_audio(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        """Téléverse et envoie un fichier audio (WAV, M4A, AAC ou MP4 : formats acceptés par l'API d'envoi de Messenger)."""
        s = get_settings()
        url = f"https://graph.facebook.com/{s.graph_api_version}/me/messages"
        ext = {"audio/wav": "wav", "audio/mp4": "m4a", "audio/aac": "aac"}.get(content_type)
        if ext is None:
            raise SendError(f"format audio non accepté par Messenger: {content_type}", permanent=True)
        try:
            r = self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                data={
                    "recipient": json.dumps({"id": recipient_id}),
                    "message": json.dumps({"attachment": {"type": "audio", "payload": {"is_reusable": False}}}),
                    "messaging_type": "RESPONSE",
                },
                files={"filedata": (f"voice.{ext}", data, content_type)},   # nom ASCII : exigé par l'API
            )
        except httpx.TransportError as e:
            raise SendError(f"réseau: {type(e).__name__}", permanent=False) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise SendError(f"meta {r.status_code}", permanent=False)
        if r.status_code >= 400:
            raise SendError(f"meta {r.status_code}: {r.text[:200]}", permanent=True)
        return r.json().get("message_id")

    def send_image(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        """Téléverse et envoie une image (JPEG, PNG ou GIF : formats acceptés par l'API d'envoi de Messenger)."""
        s = get_settings()
        url = f"https://graph.facebook.com/{s.graph_api_version}/me/messages"
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}.get(content_type)
        if ext is None:
            raise SendError(f"format d'image non accepté par Messenger: {content_type}", permanent=True)
        try:
            r = self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                data={
                    "recipient": json.dumps({"id": recipient_id}),
                    "message": json.dumps({"attachment": {"type": "image", "payload": {"is_reusable": False}}}),
                    "messaging_type": "RESPONSE",
                },
                files={"filedata": (f"photo.{ext}", data, content_type)},   # nom ASCII : exigé par l'API
            )
        except httpx.TransportError as e:
            raise SendError(f"réseau: {type(e).__name__}", permanent=False) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise SendError(f"meta {r.status_code}", permanent=False)
        if r.status_code >= 400:
            raise SendError(f"meta {r.status_code}: {r.text[:200]}", permanent=True)
        return r.json().get("message_id")


class EvolutionSender:
    """WhatsApp via Evolution API (non officiel). Les identifiants du canal sont le JSON de `credentials.py` :
    `phone_number_id` = NOM DE L'INSTANCE Evolution ; `access_token` n'est pas utilisé (la clé API est dans la config)."""

    def __init__(self, credentials: str, client: httpx.Client | None = None):
        try:
            _, self._instance = whatsapp_credentials(credentials)
        except CredentialsError as e:
            raise SendError(str(e), permanent=True) from e
        self._client = client or httpx.Client(timeout=20)

    def send_text(self, recipient_id: str, text: str) -> str | None:
        s = get_settings()
        if not s.evolution_api_key:
            raise SendError("EVOLUTION_API_KEY non configurée", permanent=True)
        url = f"{s.evolution_api_url.rstrip('/')}/message/sendText/{quote(self._instance, safe='')}"
        last_id = None
        for chunk in split_text(text):
            try:
                r = self._client.post(url, headers={"apikey": s.evolution_api_key}, json={"number": recipient_id, "text": chunk})
            except httpx.TransportError as e:
                raise SendError(f"réseau: {type(e).__name__}", permanent=False) from e
            if r.status_code >= 500 or r.status_code == 429:
                raise SendError(f"evolution {r.status_code}", permanent=False)
            if r.status_code >= 400:
                # 4xx : clé invalide, instance déconnectée, numéro inexistant... inutile de réessayer tel quel.
                raise SendError(f"evolution {r.status_code}: {r.text[:200]}", permanent=True)
            try:
                last_id = (r.json().get("key") or {}).get("id")
            except ValueError:
                last_id = None
        return last_id

    def send_audio(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        raise SendError("envoi de vocaux non géré par le canal Evolution (prototype : texte seulement)", permanent=True)

    def send_image(self, recipient_id: str, data: bytes, content_type: str) -> str | None:
        raise SendError("envoi d'images non géré par le canal Evolution (prototype : texte seulement)", permanent=True)


def get_sender(channel_type: str, token: str) -> Sender:
    if get_settings().send_mode == "console":
        return ConsoleSender()
    if channel_type == "messenger":
        return MessengerSender(token)
    if channel_type == "whatsapp":
        return EvolutionSender(token)
    raise SendError(f"canal non supporté pour l'envoi: {channel_type}", permanent=True)
