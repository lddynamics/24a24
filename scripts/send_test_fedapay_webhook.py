"""Envoie un faux webhook FedaPay SIGNÉ au serveur local, pour tester sans attendre FedaPay.

  python -m scripts.send_test_fedapay_webhook --transaction-id 123456 [--event transaction.approved]

Le statut réel est TOUJOURS revérifié auprès de l'API FedaPay (bac à sable) : ce script ne fait que déclencher
le traitement. Un vrai paiement doit donc avoir été approuvé dans le bac à sable pour que la commande passe à « payée ».
"""
import argparse
import hashlib
import hmac
import json
import time

import httpx

from app.config import get_settings


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--transaction-id", required=True)
    p.add_argument("--event", default="transaction.approved")
    p.add_argument("--url", default="http://localhost:8000/webhooks/fedapay")
    args = p.parse_args()

    secret = get_settings().fedapay_webhook_secret
    if not secret:
        raise SystemExit("FEDAPAY_WEBHOOK_SECRET manquant dans .env")
    body = json.dumps({"name": args.event, "object": "event", "entity": {"id": int(args.transaction_id)}}).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    r = httpx.post(args.url, content=body, headers={"Content-Type": "application/json", "X-FEDAPAY-SIGNATURE": f"t={ts},s={sig}"})
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
