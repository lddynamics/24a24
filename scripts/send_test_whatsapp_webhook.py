"""Envoie un faux événement WhatsApp SIGNÉ au serveur local (sans passer par Meta).

  python -m scripts.send_test_whatsapp_webhook --phone-number-id 1234567890 --from 22997000000 --text "Bonjour"
  python -m scripts.send_test_whatsapp_webhook --phone-number-id 1234567890 --kind audio --media-id 987   # vocal
"""
import argparse
import hashlib
import hmac
import json
import time

import httpx

from app.config import get_settings


def build_payload(
    phone_number_id: str, wa_id: str, text: str = "", *, wamid: str | None = None, name: str | None = "Client Test",
    kind: str = "text", media_id: str | None = None,
) -> dict:
    message: dict = {"from": wa_id, "id": wamid or f"wamid.{time.time_ns()}", "timestamp": str(int(time.time())), "type": kind}
    if kind == "text":
        message["text"] = {"body": text}
    elif kind in ("image", "audio", "video", "document"):
        message[kind] = {"id": media_id or "media_test_1", "mime_type": "audio/ogg; codecs=opus" if kind == "audio" else "image/jpeg"}
        if kind == "audio":
            message[kind]["voice"] = True
        if text and kind != "audio":
            message[kind]["caption"] = text
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "22900000000", "phone_number_id": phone_number_id},
        "messages": [message],
    }
    if name:
        value["contacts"] = [{"profile": {"name": name}, "wa_id": wa_id}]
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA_TEST", "changes": [{"field": "messages", "value": value}]}]}


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/webhooks/whatsapp")
    p.add_argument("--phone-number-id", required=True)
    p.add_argument("--from", dest="wa_id", default="22997000000")
    p.add_argument("--text", default="Bonjour, quels sont vos horaires ?")
    p.add_argument("--kind", default="text", choices=("text", "image", "audio", "video", "document"))
    p.add_argument("--media-id")
    p.add_argument("--wamid")
    a = p.parse_args()

    body = json.dumps(build_payload(a.phone_number_id, a.wa_id, a.text, wamid=a.wamid, kind=a.kind, media_id=a.media_id)).encode()
    r = httpx.post(
        a.url,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(get_settings().meta_app_secret, body)},
    )
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
