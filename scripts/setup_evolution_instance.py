"""Crée l'instance Evolution (le numéro WhatsApp), configure son webhook, puis affiche le code d'appairage ou enregistre le QR.

Autonome : n'a besoin ni de la base ni du .env complet de l'application (utilisable depuis GitHub Actions).

  # QR code (à scanner dans les 60 secondes) :
  python -m scripts.setup_evolution_instance --instance agent24 --app-url https://<app>.onrender.com
  # Code d'appairage (pas d'image : WhatsApp > Appareils connectés > Connecter avec un numéro de téléphone) :
  python -m scripts.setup_evolution_instance --instance agent24 --app-url https://<app>.onrender.com --number 22901XXXXXXXX

Variables : EVOLUTION_API_URL, EVOLUTION_API_KEY, EVOLUTION_WEBHOOK_SECRET (ou options --evolution-url / --api-key / --webhook-secret).
Ensuite, relier l'instance à un client :
  ACCESS_TOKEN=inutile python -m scripts.add_channel --tenant-id <UUID> --channel whatsapp --external-id agent24
"""
import argparse
import base64
import os
import sys

import httpx

from app.channels.evolution import SECRET_HEADER


def build_body(instance: str, app_url: str, secret: str, number: str | None) -> dict:
    body = {
        "instanceName": instance,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "webhook": {
            "url": f"{app_url.rstrip('/')}/webhooks/evolution",
            "byEvents": False,
            "base64": False,
            "headers": {SECRET_HEADER: secret, "Content-Type": "application/json"},
            "events": ["MESSAGES_UPSERT"],
        },
    }
    if number:
        body["number"] = number     # demande un code d'appairage en plus du QR
    return body


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--instance", required=True, help="Nom de l'instance (sera l'external-id du canal)")
    p.add_argument("--app-url", default="http://host.docker.internal:8000", help="URL de l'application VUE PAR EVOLUTION")
    p.add_argument("--evolution-url", default=os.environ.get("EVOLUTION_API_URL", "http://localhost:8080"))
    p.add_argument("--api-key", default=os.environ.get("EVOLUTION_API_KEY", ""))
    p.add_argument("--webhook-secret", default=os.environ.get("EVOLUTION_WEBHOOK_SECRET", ""))
    p.add_argument("--number", help="Numéro WhatsApp en chiffres avec indicatif (ex. 22901XXXXXXXX) : active le code d'appairage")
    p.add_argument("--qr-file", default="qrcode.png")
    a = p.parse_args()

    if not a.api_key or not a.webhook_secret:
        print("Définir EVOLUTION_API_KEY et EVOLUTION_WEBHOOK_SECRET", file=sys.stderr)
        return 1

    # Render gratuit : le service peut dormir, le premier appel est long.
    r = httpx.post(
        f"{a.evolution_url.rstrip('/')}/instance/create", headers={"apikey": a.api_key},
        json=build_body(a.instance, a.app_url, a.webhook_secret, a.number), timeout=120,
    )
    if r.status_code >= 400:
        print(f"Échec ({r.status_code}) : {r.text[:500]}", file=sys.stderr)
        return 1

    qrcode = r.json().get("qrcode") or {}
    if qrcode.get("pairingCode"):
        print(f"CODE D'APPAIRAGE : {qrcode['pairingCode']}  (WhatsApp > Appareils connectés > Connecter un appareil > "
              f"Connecter avec un numéro de téléphone)")
    qr = qrcode.get("base64") or ""
    if "," in qr:
        with open(a.qr_file, "wb") as f:
            f.write(base64.b64decode(qr.split(",", 1)[1]))
        print(f"QR code enregistré dans {a.qr_file} : le scanner dans les 60 secondes (WhatsApp > Appareils connectés).")
    elif not qrcode.get("pairingCode"):
        print("Instance créée, mais ni QR ni code dans la réponse. Utiliser le gestionnaire : <url Evolution>/manager "
              "ou GET /instance/connect/" + a.instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
