"""Client FedaPay (API v1) et vérification des webhooks.

Source de vérité d'un paiement = l'API FedaPay (GET /transactions/{id}), JAMAIS le contenu d'un webhook
ni ce que dit le client. Le webhook sert de déclencheur : il est signé, puis on revérifie via l'API.
Documentation : https://docs.fedapay.com (création de transaction, token de paiement, webhooks).
"""
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BASE_URLS = {"sandbox": "https://sandbox-api.fedapay.com/v1", "live": "https://api.fedapay.com/v1"}


class FedaPayError(Exception):
    pass


@dataclass
class Transaction:
    id: str
    status: str
    amount: int | None = None
    reference: str | None = None


def map_status(provider_status: str | None) -> str:
    """Statut FedaPay -> statut interne : 'pending' | 'succeeded' | 'failed'."""
    s = (provider_status or "").lower()
    if s in {"approved", "transferred"}:
        return "succeeded"
    if s in {"declined", "canceled", "cancelled", "refunded", "expired"}:
        return "failed"
    return "pending"  # pending, created, ou statut inconnu : on ne conclut jamais « payé » par défaut


def _unwrap(data: dict, key: str) -> dict:
    """L'API peut envelopper l'objet (« v1/transaction ») ou le renvoyer à plat : on accepte les deux."""
    for k in (f"v1/{key}", key):
        if isinstance(data.get(k), dict):
            return data[k]
    return data


class FedaPayClient:
    def __init__(self, secret_key: str, environment: str = "sandbox", client: httpx.Client | None = None):
        if environment not in BASE_URLS:
            raise FedaPayError(f"environnement FedaPay inconnu: {environment}")
        self._base = BASE_URLS[environment]
        self._headers = {"Authorization": f"Bearer {secret_key}", "Content-Type": "application/json"}
        self._http = client or httpx.Client(timeout=20)

    def _request(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._http.request(method, f"{self._base}{path}", headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise FedaPayError(f"FedaPay injoignable: {type(e).__name__}") from e
        if r.status_code not in (200, 201):
            raise FedaPayError(f"FedaPay {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise FedaPayError("réponse FedaPay illisible") from e

    @staticmethod
    def _parse_tx(data: dict) -> Transaction:
        tx = _unwrap(data, "transaction")
        if "id" not in tx:
            raise FedaPayError("réponse FedaPay sans identifiant de transaction")
        amount = tx.get("amount")
        return Transaction(
            id=str(tx["id"]),
            status=str(tx.get("status") or "pending"),
            amount=int(amount) if amount is not None else None,
            reference=tx.get("reference"),
        )

    def create_transaction(
        self, *, amount: int, description: str, currency: str = "XOF", callback_url: str | None = None,
        metadata: dict | None = None, customer: dict | None = None,
    ) -> Transaction:
        body: dict = {"description": description, "amount": amount, "currency": {"iso": currency}}
        if customer:
            body["customer"] = customer
        if callback_url:
            body["callback_url"] = callback_url
        if metadata:
            body["custom_metadata"] = metadata
        return self._parse_tx(self._request("POST", "/transactions", json=body))

    def generate_token(self, transaction_id: str) -> tuple[str, str]:
        data = self._request("POST", f"/transactions/{transaction_id}/token")
        token, url = data.get("token"), data.get("url")
        if not token or not url:
            raise FedaPayError("réponse FedaPay sans lien de paiement")
        return token, url

    def get_transaction(self, transaction_id: str) -> Transaction:
        return self._parse_tx(self._request("GET", f"/transactions/{transaction_id}"))


@lru_cache
def get_client() -> FedaPayClient:
    s = get_settings()
    if not s.fedapay_secret_key:
        raise FedaPayError("FEDAPAY_SECRET_KEY manquante")
    return FedaPayClient(s.fedapay_secret_key, s.fedapay_environment)


# --- Webhooks -----------------------------------------------------------------------------------

def verify_signature(secret: str, raw_body: bytes, header: str | None, *, tolerance: int = 300, now: float | None = None) -> bool:
    """Vérifie l'en-tête X-FEDAPAY-SIGNATURE (HMAC-SHA256, secret propre à chaque endpoint).

    Format attendu (type « t=<horodatage>,s=<signature> », signature = HMAC-SHA256 de « <t>.<corps> ») ; un en-tête
    réduit à la signature hexadécimale du corps est aussi accepté. Le format exact n'est pas détaillé dans la
    documentation publique : À CONFIRMER avec un vrai webhook du bac à sable. Échec = on rejette (fermé par défaut).
    Même valide, un webhook ne prouve rien : le statut est toujours revérifié auprès de l'API.
    """
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.replace(" ", "").split(",") if "=" in p)
    sig = parts.get("s") or parts.get("v1")
    if "t" in parts and sig:
        try:
            ts = int(parts["t"])
        except ValueError:
            return False
        if abs((now if now is not None else time.time()) - ts) > tolerance:
            return False
        signed = f"{ts}.".encode() + raw_body
    else:
        sig, signed = header.strip(), raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def extract_transaction_id(payload: dict) -> str | None:
    """Retrouve l'identifiant de transaction dans un événement (structure tolérante)."""
    candidates = [
        (payload.get("entity") or {}),
        ((payload.get("data") or {}).get("object") or {}) if isinstance(payload.get("data"), dict) else {},
        payload.get("object") if isinstance(payload.get("object"), dict) else {},
    ]
    for c in candidates:
        if isinstance(c, dict) and isinstance(c.get("id"), (int, str)) and c.get("id") != "":
            return str(c["id"])
    return None
