"""Commande + paiement FedaPay : flux complet avec un faux FedaPay, webhook signé, garde-fous."""
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent.llm import to_openai_tools
from app.agent.tools import CreateOrderInput
from app.db import tenant_session
from app.main import app
from app.services import fedapay as fp
from app.services import orders as order_service
from scripts.set_tenant_config import set_tenant_config
from tests.seed import seed_catalog
from tests.fakes import FakeSender, text_reply, tool_reply
from tests.test_agent import _drain, _in, _rows

KORA = json.loads(Path("tenants/maison-kora.json").read_text(encoding="utf-8"))
SECRET = "whsec_test"
KORA_CFG = {**KORA, "payment": {"invoice_email": "boutique@maisonkora.test"}}


class FakeFeda:
    def __init__(self):
        self.txs: dict[str, fp.Transaction] = {}
        self.created: list[dict] = []

    def create_transaction(self, *, amount, description, currency="XOF", callback_url=None, metadata=None, customer=None):
        tid = str(1000 + len(self.txs))
        self.txs[tid] = fp.Transaction(tid, "pending", amount, f"ref_{tid}")
        self.created.append({"amount": amount, "description": description, "metadata": metadata, "customer": customer})
        return self.txs[tid]

    def generate_token(self, tid):
        return f"tok{tid}", f"https://sandbox-process.fedapay.com/tok{tid}"

    def get_transaction(self, tid):
        return self.txs[tid]


@pytest.fixture
def feda(monkeypatch):
    fake = FakeFeda()
    monkeypatch.setattr(fp, "get_client", lambda: fake)
    return fake


@pytest.fixture
def kora(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA_CFG)
    seed_catalog(tenant_id, KORA_CFG)
    return tenant_id


def _result_of_last_tool(messages) -> dict:
    return json.loads(messages[-1]["content"][0]["content"])


class PurchaseLLM:
    """Scénario : create_order -> create_payment_link -> réponse avec le lien (la référence vient des résultats d'outils)."""

    ORDER = {
        "items": [{"product_id": "KOR-R01", "size": "M", "quantity": 1}],
        "customer_name": "Awa K.", "customer_phone": "0100000000",
        "delivery_zone": "Abomey-Calavi", "delivery_address": "Quartier Zoca, près du marché",
    }

    def __init__(self):
        self.n, self.calls = 0, []

    def create(self, *, model, system, tools, messages, max_tokens):
        self.calls.append({"tools": tools, "messages": list(messages)})
        n, self.n = self.n, self.n + 1
        if n == 0:
            return tool_reply("create_order", self.ORDER, "t1")
        if n == 1:
            return tool_reply("create_payment_link", {"order_ref": _result_of_last_tool(messages)["order_ref"]}, "t2")
        out = _result_of_last_tool(messages)
        # Le modèle ne reçoit jamais l'URL, seulement le jeton : c'est le backend qui le remplace avant l'envoi.
        assert "payment_url" not in out and "http" not in json.dumps(out)
        return text_reply("Voici votre lien : " + out["payment_link"] if "payment_link" in out else "INDISPO")


def _purchase(kora, feda):
    _in("U1", "Je confirme la robe Sika en M", "m1")
    llm, sender = PurchaseLLM(), FakeSender()
    _drain(llm, sender)
    return llm, sender


# --- calcul de commande (pur) ---------------------------------------------------------------------

def _build(items, zone="Cotonou"):
    return order_service.build_order(KORA, items, zone)


def test_server_computes_totals_and_delivery_fee():
    out = _build([{"product_id": "KOR-R01", "size": "M", "quantity": 2}, {"product_id": "KOR-A01", "color": "Indigo", "quantity": 1}], "abomey-calavi")
    assert out["subtotal"] == 2 * 32000 + 8000 and out["delivery_fee"] == 2000 and out["total"] == 74000
    assert out["zone"] == "Abomey-Calavi"


@pytest.mark.parametrize("item,zone,fragment", [
    ({"product_id": "KOR-R01", "size": "XL", "quantity": 1}, "Cotonou", "indisponible"),        # taille à 0 en stock
    ({"product_id": "KOR-R01", "size": "L", "quantity": 5}, "Cotonou", "stock insuffisant"),
    ({"product_id": "KOR-A01", "quantity": 1}, "Cotonou", "précise"),                          # plusieurs couleurs
    ({"product_id": "NOPE", "size": "M", "quantity": 1}, "Cotonou", "inconnu"),
    ({"product_id": "KOR-R01", "size": "M", "quantity": 1}, "Parakou", "non desservie"),
])
def test_invalid_orders_are_refused_with_readable_reason(item, zone, fragment):
    with pytest.raises(order_service.OrderError, match=fragment):
        _build([item], zone)


def test_model_cannot_send_prices_or_tenant():
    with pytest.raises(Exception):
        CreateOrderInput.model_validate({**PurchaseLLM.ORDER, "total": 1})
    with pytest.raises(Exception):
        CreateOrderInput.model_validate({**PurchaseLLM.ORDER, "items": [{"product_id": "KOR-R01", "price": 1}]})


def test_nested_order_schema_is_inlined_for_open_models():
    [tool] = to_openai_tools([{"name": "create_order", "description": "d", "input_schema": CreateOrderInput.model_json_schema()}])
    assert "$ref" not in json.dumps(tool) and "$defs" not in json.dumps(tool)
    assert "product_id" in tool["function"]["parameters"]["properties"]["items"]["items"]["properties"]


# --- flux complet ---------------------------------------------------------------------------------

def test_full_purchase_creates_order_and_payment_link(kora, feda):
    llm, sender = _purchase(kora, feda)
    assert sender.sent[0][1].startswith("Voici votre lien : https://sandbox-process.fedapay.com/tok1000")
    assert feda.created[0]["amount"] == 34000 and len(feda.created) == 1
    assert feda.created[0]["customer"] == {"firstname": "Awa", "lastname": "K.", "email": "boutique@maisonkora.test"}
    [o] = _rows(kora, "SELECT reference, status, total, delivery_fee FROM orders")
    assert (o.status, o.total, o.delivery_fee) == ("awaiting_payment", 34000, 2000) and o.reference.startswith("KOR-")
    [p] = _rows(kora, "SELECT status, amount, provider_transaction_id FROM payments")
    assert (p.status, p.amount, p.provider_transaction_id) == ("pending", 34000, "1000")
    assert {t.tool_name for t in _rows(kora, "SELECT tool_name FROM tool_calls")} == {"create_order", "create_payment_link"}


def test_payment_link_is_reused_not_duplicated(kora, feda):
    _purchase(kora, feda)
    with tenant_session(kora) as s:
        row = s.execute(text("SELECT reference, conversation_id FROM orders")).one()
        again = order_service.start_payment(s, kora, row.conversation_id, row.reference, feda, KORA_CFG)
    assert again["reused"] is True and len(feda.txs) == 1


def test_missing_invoice_email_creates_no_transaction(make_tenant, owner_engine, feda, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "fedapay_customer_email", "")
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)          # aucun e-mail de facturation configuré
    seed_catalog(tenant_id, KORA)
    _, sender = _purchase(tenant_id, feda)
    assert feda.created == []                                  # pas de transaction orpheline chez FedaPay
    assert sender.sent[0][1] == "INDISPO"
    assert _rows(tenant_id, "SELECT status FROM payments") == []


@pytest.mark.parametrize("full,expected", [("Awa Koffi", ("Awa", "Koffi")), ("Awa", ("Awa", "Client")),
                                           ("  Jean Marc Dossou ", ("Jean", "Marc Dossou")), ("", ("Client", "Client"))])
def test_customer_name_is_split_for_fedapay(full, expected):
    assert order_service.split_name(full) == expected


def test_client_cannot_touch_another_conversations_order(kora, feda):
    _purchase(kora, feda)
    _in("U2", "Bonjour", "m2")
    with tenant_session(kora) as s:
        ref = s.execute(text("SELECT reference FROM orders")).scalar_one()
        other = s.execute(text("SELECT id FROM conversations WHERE id NOT IN (SELECT conversation_id FROM orders)")).scalar_one()
        with pytest.raises(order_service.OrderError):
            order_service.payment_status_for_order(s, other, ref, feda)
        with pytest.raises(order_service.OrderError):
            order_service.start_payment(s, kora, other, ref, feda, KORA_CFG)


# --- webhook ---------------------------------------------------------------------------------------

def _signed(payload: dict, secret=SECRET, ts=None):
    body = json.dumps(payload).encode()
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-FEDAPAY-SIGNATURE": f"t={ts},s={sig}"}


def _event(tid="1000", name="transaction.approved"):
    return {"name": name, "object": "event", "entity": {"id": int(tid), "status": "approved"}}


class CheckStatusLLM:
    def __init__(self):
        self.n, self.user_msgs = 0, []

    def create(self, *, model, system, tools, messages, max_tokens):
        n, self.n = self.n, self.n + 1
        if n == 0:
            self.user_msgs.append(messages[-1]["content"])
            with tenant_session(self.tenant) as s:
                ref = s.execute(text("SELECT reference FROM orders")).scalar_one()
            return tool_reply("check_payment_status", {"order_ref": ref}, "t9")
        out = _result_of_last_tool(messages)
        return text_reply("Paiement confirmé, merci !" if out["paid"] else "Pas encore reçu.")


def test_signed_approved_webhook_marks_paid_notifies_agent_once(kora, feda):
    _purchase(kora, feda)
    feda.txs["1000"].status = "approved"
    body, headers = _signed(_event())
    with TestClient(app) as c:
        assert c.post("/webhooks/fedapay", content=body, headers=headers).status_code == 200
        body2, headers2 = _signed(_event())          # FedaPay renvoie parfois le même événement
        assert c.post("/webhooks/fedapay", content=body2, headers=headers2).status_code == 200
    assert _rows(kora, "SELECT status FROM orders")[0].status == "paid"
    [p] = _rows(kora, "SELECT status, paid_at FROM payments")
    assert p.status == "succeeded" and p.paid_at is not None
    notes = _rows(kora, "SELECT text FROM messages WHERE text LIKE '[Notification système]%'")
    assert len(notes) == 1                           # idempotent : pas de doublon

    class NoModel:                                   # le backend confirme lui-même : le modèle n'est JAMAIS appelé
        def create(self, **_):
            raise AssertionError("le modèle ne doit pas être appelé pour confirmer un paiement")

    sender = FakeSender()
    _drain(NoModel(), sender)
    assert "paiement est confirmé" in sender.sent[-1][1] and "KOR-" in sender.sent[-1][1]


def test_webhook_is_only_a_trigger_status_comes_from_the_api(kora, feda):
    _purchase(kora, feda)                            # la transaction reste « pending » côté FedaPay
    body, headers = _signed(_event())                # un faux « approved » dans le webhook ne suffit pas
    with TestClient(app) as c:
        assert c.post("/webhooks/fedapay", content=body, headers=headers).status_code == 200
    assert _rows(kora, "SELECT status FROM orders")[0].status == "awaiting_payment"
    assert _rows(kora, "SELECT status FROM payments")[0].status == "pending"


def test_amount_mismatch_is_never_marked_paid(kora, feda):
    _purchase(kora, feda)
    feda.txs["1000"].status, feda.txs["1000"].amount = "approved", 100
    body, headers = _signed(_event())
    with TestClient(app) as c:
        assert c.post("/webhooks/fedapay", content=body, headers=headers).status_code == 200
    assert _rows(kora, "SELECT status FROM payments")[0].status == "pending"
    assert _rows(kora, "SELECT status FROM orders")[0].status == "awaiting_payment"


def test_declined_payment_marks_failed_and_allows_new_link(kora, feda):
    _purchase(kora, feda)
    feda.txs["1000"].status = "declined"
    body, headers = _signed(_event(name="transaction.declined"))
    with TestClient(app) as c:
        c.post("/webhooks/fedapay", content=body, headers=headers)
    assert _rows(kora, "SELECT status FROM orders")[0].status == "failed"
    with tenant_session(kora) as s:
        row = s.execute(text("SELECT reference, conversation_id FROM orders")).one()
        new = order_service.start_payment(s, kora, row.conversation_id, row.reference, feda, KORA_CFG)
    assert new["payment_url"].endswith("tok1001") and len(feda.txs) == 2


@pytest.mark.parametrize("make", [
    lambda: (b"{}", {"X-FEDAPAY-SIGNATURE": "t=1,s=deadbeef"}),
    lambda: (b"{}", {}),
    lambda: _signed({"name": "x"}, secret="autre_secret"),
    lambda: _signed({"name": "x"}, ts=int(time.time()) - 3600),   # rejeu d'un vieux webhook
])
def test_bad_signatures_are_rejected(kora, feda, make):
    body, headers = make()
    with TestClient(app) as c:
        assert c.post("/webhooks/fedapay", content=body, headers=headers).status_code == 400


def test_unknown_transaction_and_other_events_are_acknowledged_and_ignored(kora, feda):
    with TestClient(app) as c:
        for payload in (_event("999999"), {"name": "customer.created", "entity": {"id": 1}}):
            body, headers = _signed(payload)
            assert c.post("/webhooks/fedapay", content=body, headers=headers).status_code == 200


# --- briques FedaPay -------------------------------------------------------------------------------

def test_signature_formats_and_tolerance():
    body = b'{"a":1}'
    ts = 1_700_000_000
    sig = hmac.new(b"k", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert fp.verify_signature("k", body, f"t={ts},s={sig}", now=ts + 10)
    assert fp.verify_signature("k", body, f"t={ts},v1={sig}", now=ts + 10)
    assert not fp.verify_signature("k", body, f"t={ts},s={sig}", now=ts + 1000)      # trop ancien
    assert not fp.verify_signature("k", body + b" ", f"t={ts},s={sig}", now=ts)        # corps modifié
    assert not fp.verify_signature("", body, f"t={ts},s={sig}", now=ts)                # pas de secret : fermé
    assert fp.verify_signature("k", body, hmac.new(b"k", body, hashlib.sha256).hexdigest())  # format réduit


@pytest.mark.parametrize("status,expected", [
    ("approved", "succeeded"), ("transferred", "succeeded"), ("declined", "failed"), ("canceled", "failed"),
    ("pending", "pending"), ("", "pending"), ("quelque_chose_d_inconnu", "pending"),
])
def test_status_mapping_never_assumes_paid(status, expected):
    assert fp.map_status(status) == expected


def test_extract_transaction_id_variants():
    assert fp.extract_transaction_id({"entity": {"id": 12}}) == "12"
    assert fp.extract_transaction_id({"data": {"object": {"id": "13"}}}) == "13"
    assert fp.extract_transaction_id({"name": "x"}) is None


def _client(handler):
    return fp.FedaPayClient("sk_test", "sandbox", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_client_accepts_wrapped_and_flat_responses_and_sends_expected_request():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url), req.headers["authorization"], req.read()))
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"token": "T", "url": "https://pay/T"})
        if req.method == "POST":
            return httpx.Response(201, json={"v1/transaction": {"id": 7, "status": "pending", "amount": 34000, "reference": "r"}})
        return httpx.Response(200, json={"id": 7, "status": "approved", "amount": 34000})

    c = _client(handler)
    tx = c.create_transaction(amount=34000, description="Commande X", callback_url="https://cb", metadata={"k": "v"})
    assert (tx.id, tx.status, tx.amount) == ("7", "pending", 34000)
    assert c.generate_token("7") == ("T", "https://pay/T")
    assert c.get_transaction("7").status == "approved"
    method, url, auth, body = seen[0]
    assert url == "https://sandbox-api.fedapay.com/v1/transactions" and auth == "Bearer sk_test"
    sent = json.loads(body)
    assert sent["currency"] == {"iso": "XOF"} and sent["amount"] == 34000 and sent["custom_metadata"] == {"k": "v"}
    assert seen[1][1].endswith("/transactions/7/token")


@pytest.mark.parametrize("status", [401, 422, 500])
def test_client_errors_raise_fedapay_error(status):
    with pytest.raises(fp.FedaPayError):
        _client(lambda r: httpx.Response(status, text="no")).get_transaction("1")


# --- garde-fou : jamais de « paiement confirmé » sans commande payée en base ------------------------

from app.agent.guards import PAYMENT_NOT_CONFIRMED, claims_payment_received  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402


@pytest.mark.parametrize("reply,expected", [
    ("Merci, votre paiement a bien été confirmé. Votre commande KOR-1 est enregistrée.", True),
    ("Bonne nouvelle : nous avons bien reçu votre paiement !", True),
    ("Votre paiement est confirmé, nous préparons l'expédition.", True),
    ("Votre commande a été payée, merci.", True),
    ("Voici votre lien de paiement sécurisé : https://x", False),
    ("Dès réception du paiement, nous préparerons votre robe.", False),
    ("Je n'ai pas encore reçu la confirmation de votre paiement.", False),
    ("Une fois le paiement effectué, je vérifie et je vous confirme.", False),
    ("Pour confirmer votre commande, procédez au paiement.", False),
    ("Quel mode de paiement préférez-vous ?", False),
])
def test_payment_claim_detection(reply, expected):
    assert claims_payment_received(reply) is expected


def test_unfounded_payment_claim_is_replaced_before_sending(kora, feda):
    _purchase(kora, feda)                                     # commande en attente de paiement
    _in("U1", "voilà c'est fait", "m2")
    sender = FakeSender()
    _drain(FakeLLM(text_reply("Merci, votre paiement a bien été confirmé !")), sender)
    assert sender.sent[-1][1] == PAYMENT_NOT_CONFIRMED


def test_payment_claim_passes_when_order_is_really_paid(kora, feda):
    _purchase(kora, feda)
    with tenant_session(kora) as s:
        s.execute(text("UPDATE orders SET status = 'paid'"))
    _in("U1", "merci", "m2")
    sender = FakeSender()
    _drain(FakeLLM(text_reply("Merci, votre paiement a bien été confirmé !")), sender)
    assert sender.sent[-1][1] == "Merci, votre paiement a bien été confirmé !"
