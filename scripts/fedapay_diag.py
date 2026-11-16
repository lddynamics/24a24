"""Diagnostic d'une transaction FedaPay (bac à sable) : état réel côté FedaPay + nouveau lien de paiement.

  python -m scripts.fedapay_diag --transaction-id 539470 [--new-link]

N'affiche jamais la clé secrète. Sert à comprendre pourquoi une page de paiement ne s'ouvre pas.
"""
import argparse
import json

import httpx

from app.config import get_settings
from app.services.fedapay import BASE_URLS


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--transaction-id", required=True)
    p.add_argument("--new-link", action="store_true", help="génère un nouveau lien pour cette transaction")
    args = p.parse_args()
    s = get_settings()
    if not s.fedapay_secret_key:
        raise SystemExit("FEDAPAY_SECRET_KEY manquante dans .env")
    base = BASE_URLS[s.fedapay_environment]
    h = {"Authorization": f"Bearer {s.fedapay_secret_key}", "Content-Type": "application/json"}
    print(f"environnement: {s.fedapay_environment} ({base})")

    r = httpx.get(f"{base}/transactions/{args.transaction_id}", headers=h, timeout=20)
    print("GET transaction ->", r.status_code)
    try:
        data = r.json()
    except ValueError:
        print(r.text[:500])
        return
    tx = data.get("v1/transaction") or data.get("transaction") or data
    for k in ("id", "reference", "status", "amount", "currency_id", "mode", "customer_id", "callback_url", "expired_at", "created_at"):
        if k in tx:
            print(f"  {k}: {tx[k]}")
    if r.status_code != 200:
        print(json.dumps(data, ensure_ascii=False)[:500])

    if args.new_link:
        r2 = httpx.post(f"{base}/transactions/{args.transaction_id}/token", headers=h, timeout=20)
        print("POST token ->", r2.status_code)
        print(r2.json().get("url") if r2.status_code in (200, 201) else r2.text[:300])


if __name__ == "__main__":
    main()
