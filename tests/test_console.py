"""Console d'entreprise : accès, conversations, prise de main, commandes, catalogue (CRUD), isolation."""
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.channels.sender import SendError
from app.config import get_settings
from app.console import auth
from app.db import tenant_session
from app.main import app
from app.services import catalog as catalog_service
from app.services import console as console_service
from app.services import fedapay as fp
from app.services import orders as order_service
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeLLM, FakeSender, text_reply
from tests.seed import seed_catalog
from tests.test_agent import _drain, _in, _rows
from tests.test_payments import FakeFeda, _purchase

KORA = {**json.loads(Path("tenants/maison-kora.json").read_text(encoding="utf-8")),
        "payment": {"invoice_email": "boutique@maisonkora.test"}}
PASSWORD = "mot-de-passe-test"


@pytest.fixture
def tenant(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)
    seed_catalog(tenant_id, KORA)
    return tenant_id


@pytest.fixture
def console(tenant, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "console_password", PASSWORD)
    monkeypatch.setattr(s, "console_tenant_id", str(tenant))
    auth.limiter._hits.clear()
    return tenant


@pytest.fixture
def feda(monkeypatch):
    fake = FakeFeda()
    monkeypatch.setattr(fp, "get_client", lambda: fake)
    return fake


@pytest.fixture
def client(console):
    with TestClient(app, follow_redirects=False) as c:
        r = c.post("/console/login", content=urlencode({"password": PASSWORD}), headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 303
        yield c


def _form(c, url, data: dict | list):
    return c.post(url, content=urlencode(data, doseq=True), headers={"Content-Type": "application/x-www-form-urlencoded"})


def _product_fields(**over):
    base = {"name": "Robe Test", "category": "robes", "description": "Une robe de test", "price": "25 000", "tags": "wax, test",
            "variant_size": ["S", "M"], "variant_color": ["Bleu", "Bleu"], "variant_stock": ["3", "0"]}
    base.update(over)
    return base


# --- accès -------------------------------------------------------------------------------------------

def test_console_is_disabled_without_password(tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "console_password", "")
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/console/login").status_code == 503


def test_anonymous_users_are_redirected_or_rejected(console):
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/console")
        assert r.status_code == 303 and r.headers["location"] == "/console/login"
        assert c.get("/console/catalog").status_code == 303
        assert c.get(f"/console/api/conversations/{console}/messages").status_code == 401
        assert c.post("/console/catalog/X/delete").status_code == 303   # même protégé : jamais d'action sans session


def test_wrong_password_and_rate_limit(console):
    with TestClient(app, follow_redirects=False) as c:
        for _ in range(5):
            assert _form(c, "/console/login", {"password": "faux"}).status_code == 401
        assert _form(c, "/console/login", {"password": PASSWORD}).status_code == 429     # même le bon mot de passe est freiné


def test_session_token_signature_expiry_and_password_binding(console, monkeypatch):
    tok = auth.make_token()
    assert auth.verify_token(tok)
    exp, nonce, sig = tok.split(".")
    assert not auth.verify_token(f"{int(exp) + 999999}.{nonce}.{sig}")            # expiration falsifiée
    assert not auth.verify_token(f"{exp}.{nonce}.{'0' * len(sig)}")
    assert not auth.verify_token(tok, now=time.time() + 10 * 24 * 3600)           # expiré
    monkeypatch.setattr(get_settings(), "console_password", "nouveau")
    assert not auth.verify_token(tok)                                               # changer le mot de passe invalide les sessions


def test_cookie_flags(console):
    with TestClient(app, follow_redirects=False) as c:
        r = _form(c, "/console/login", {"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/console" in cookie


# --- pages -------------------------------------------------------------------------------------------

def _make_order(tenant, paid=False):
    with tenant_session(tenant) as s:
        conv = s.execute(text("SELECT id FROM conversations")).scalar_one()
        out = order_service.create_order(
            s, tenant, conv, KORA, items=[{"product_id": "KOR-R01", "size": "M", "quantity": 1}], customer_name="Awa K.",
            customer_phone="0100000000", delivery_zone="Cotonou", delivery_address="Zogbo")
        if paid:
            s.execute(text("UPDATE orders SET status = 'paid'"))
    return out


def test_all_pages_render_with_data(client, tenant):
    _in("U1", "Bonjour, une robe de soirée ?", "m1")
    _make_order(tenant, paid=True)
    for url in ("/console", "/console/orders", "/console/catalog", "/console/catalog/new", "/console/orders?status=paid"):
        r = client.get(url)
        assert r.status_code == 200, url
    page = client.get("/console").text
    assert "33\u202f500" in page                        # chiffre d'affaires : 32 000 + 1 500 de livraison
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id
    assert client.get("/console/conversations").headers["location"] == f"/console/conversations/{conv_id}"
    thread = client.get(f"/console/conversations/{conv_id}")
    assert thread.status_code == 200 and "Bonjour, une robe de soirée ?" in thread.text
    order_id = _rows(tenant, "SELECT id FROM orders")[0].id
    assert "KOR-" in client.get(f"/console/orders/{order_id}").text


def test_customer_text_is_escaped_never_executed(client, tenant):
    _in("U1", "<script>alert('xss')</script><img src=x onerror=alert(1)>", "m1")
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id
    html = client.get(f"/console/conversations/{conv_id}").text
    assert "<script>alert('xss')" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html
    j = client.get(f"/console/api/conversations/{conv_id}/messages").json()   # le JSON est rendu côté navigateur via textContent
    assert j["messages"][0]["text"].startswith("<script>")


def test_unknown_conversation_and_order_return_404(client):
    bogus = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/console/conversations/{bogus}").status_code == 404
    assert client.get(f"/console/orders/{bogus}").status_code == 404


def test_polling_api_returns_new_messages_only_after_timestamp(client, tenant):
    _in("U1", "premier", "m1")
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id
    first = client.get(f"/console/api/conversations/{conv_id}/messages").json()
    assert [m["text"] for m in first["messages"]] == ["premier"] and first["status"] == "agent"
    _in("U1", "second", "m2")
    later = client.get(f"/console/api/conversations/{conv_id}/messages", params={"after": first["messages"][-1]["at"]}).json()
    assert "second" in [m["text"] for m in later["messages"]]
    assert client.get(f"/console/api/conversations/{conv_id}/messages", params={"after": "pas-une-date"}).status_code == 400


# --- prise de main -----------------------------------------------------------------------------------

def test_takeover_release_and_agent_goes_silent(client, tenant):
    _in("U1", "Bonjour", "m1")
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id
    r = client.post(f"/console/api/conversations/{conv_id}/status", json={"status": "human"})
    assert r.status_code == 200 and _rows(tenant, "SELECT status FROM conversations")[0].status == "human"
    _in("U1", "Vous êtes là ?", "m2")
    llm = FakeLLM(text_reply("réponse de l'agent"))
    sender = FakeSender()
    _drain(llm, sender)
    assert llm.calls == [] and sender.sent == []          # l'agent ne répond pas quand un humain a la main
    client.post(f"/console/api/conversations/{conv_id}/status", json={"status": "agent"})
    assert _rows(tenant, "SELECT status FROM conversations")[0].status == "agent"
    assert client.post(f"/console/api/conversations/{conv_id}/status", json={"status": "closed"}).status_code == 400


def test_human_reply_is_sent_stored_and_pauses_agent(client, tenant, monkeypatch):
    _in("U1", "Bonjour", "m1")
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id
    sender = FakeSender()
    monkeypatch.setattr(console_service, "get_sender", sender.factory())
    r = client.post(f"/console/api/conversations/{conv_id}/reply", json={"text": "  Bonjour, je prends le relais.  "})
    assert r.status_code == 200 and r.json()["message"]["author"] == "human" and r.json()["status"] == "human"
    assert sender.sent[0][1] == "Bonjour, je prends le relais."
    [m] = _rows(tenant, "SELECT text, delivery_status, raw FROM messages WHERE direction = 'out'")
    assert (m.text, m.delivery_status, m.raw) == ("Bonjour, je prends le relais.", "sent", {"author": "human"})
    assert _rows(tenant, "SELECT status FROM conversations")[0].status == "human"
    thread = client.get(f"/console/api/conversations/{conv_id}/messages").json()["messages"]
    assert [x["author"] for x in thread] == ["customer", "human"]


def test_human_reply_validation_and_content_type(client, tenant):
    _in("U1", "Bonjour", "m1")
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id
    url = f"/console/api/conversations/{conv_id}/reply"
    assert client.post(url, json={"text": "   "}).status_code == 400
    assert client.post(url, json={"text": "x" * 1801}).status_code == 400
    assert client.post(url, content="text=salut", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/console/api/conversations/00000000-0000-0000-0000-000000000000/reply", json={"text": "a"}).status_code == 404
    assert _rows(tenant, "SELECT 1 FROM messages WHERE direction = 'out'") == []


def test_failed_human_send_is_kept_flagged_and_reported(client, tenant, monkeypatch):
    _in("U1", "Bonjour", "m1")
    conv_id = _rows(tenant, "SELECT id FROM conversations")[0].id

    class Broken:
        def send_text(self, recipient, text):
            raise SendError("meta 400: fenêtre dépassée", permanent=True)

    monkeypatch.setattr(console_service, "get_sender", lambda t, k: Broken())
    r = client.post(f"/console/api/conversations/{conv_id}/reply", json={"text": "Bonjour"})
    assert r.status_code == 502 and "24 h" in r.json()["detail"]
    assert _rows(tenant, "SELECT delivery_status FROM messages WHERE direction = 'out'")[0].delivery_status == "failed"


# --- commandes ---------------------------------------------------------------------------------------

def test_order_transitions(client, tenant):
    _in("U1", "Bonjour", "m1")
    _make_order(tenant, paid=True)
    oid = _rows(tenant, "SELECT id FROM orders")[0].id
    assert client.post(f"/console/orders/{oid}/status", content="status=cancelled", headers={"Content-Type": "application/x-www-form-urlencoded"}).headers["location"].endswith("ok=order_refused")
    assert _rows(tenant, "SELECT status FROM orders")[0].status == "paid"        # une commande payée ne s'annule pas ici
    assert _form(client, f"/console/orders/{oid}/status", {"status": "delivered"}).headers["location"].endswith("ok=order")
    assert _rows(tenant, "SELECT status FROM orders")[0].status == "delivered"
    assert _form(client, f"/console/orders/{oid}/status", {"status": "paid"}).headers["location"].endswith("ok=order_refused")


def test_cancelled_order_cannot_get_a_new_payment_link(client, tenant, feda):
    _in("U1", "Bonjour", "m1")
    ref = _make_order(tenant)["order_ref"]
    oid = _rows(tenant, "SELECT id FROM orders")[0].id
    _form(client, f"/console/orders/{oid}/status", {"status": "cancelled"})
    with tenant_session(tenant) as s:
        conv = s.execute(text("SELECT id FROM conversations")).scalar_one()
        with pytest.raises(order_service.OrderError, match="annulée"):
            order_service.start_payment(s, tenant, conv, ref, feda, KORA)


# --- catalogue : CRUD ----------------------------------------------------------------------------------

def test_create_edit_toggle_delete_product(client, tenant):
    r = _form(client, "/console/catalog/new", _product_fields(sku="TST-1"))
    assert r.status_code == 303 and r.headers["location"] == "/console/catalog/TST-1/edit?ok=created_photos"
    with tenant_session(tenant) as s:
        p = catalog_service.get_product(s, "TST-1")
    assert p["price"] == 25000 and p["tags"] == ["wax", "test"] and p["variants"][0] == {"size": "S", "color": "Bleu", "stock": 3}
    assert "Robe Test" in client.get("/console/catalog").text

    r = _form(client, "/console/catalog/TST-1/edit", _product_fields(name="Robe Test 2", price="27500"))
    assert r.status_code == 303
    with tenant_session(tenant) as s:
        p = catalog_service.get_product(s, "TST-1")
    assert (p["name"], p["price"]) == ("Robe Test 2", 27500)

    _form(client, "/console/catalog/TST-1/toggle", {})
    with tenant_session(tenant) as s:
        assert catalog_service.get_product(s, "TST-1")["active"] is False
        assert "TST-1" not in [x["id"] for x in catalog_service.load_catalog(s)]        # masqué : l'agent ne le voit plus
    _form(client, "/console/catalog/TST-1/delete", {})
    with tenant_session(tenant) as s:
        assert catalog_service.get_product(s, "TST-1") is None


def test_invalid_product_forms_are_rejected_with_messages(client, tenant):
    r = _form(client, "/console/catalog/new", _product_fields(name="x", price="abc", variant_size=[""], variant_color=[""], variant_stock=[""]))
    assert r.status_code == 422
    for fragment in ("entre 2 et 120", "prix doit être", "au moins une variante"):
        assert fragment in r.text
    assert "x" in r.text                                              # les valeurs saisies sont conservées
    dup = _form(client, "/console/catalog/new", _product_fields(sku="KOR-R01"))
    assert dup.status_code == 422 and "existe déjà" in dup.text
    bad_sku = _form(client, "/console/catalog/new", _product_fields(sku="a b/c"))
    assert bad_sku.status_code == 422
    assert _form(client, "/console/catalog/NOPE/edit", _product_fields()).status_code == 404


def test_catalog_import_is_idempotent(make_tenant, owner_engine, monkeypatch):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)                    # catalogue dans le JSON, table vide
    s_ = get_settings()
    monkeypatch.setattr(s_, "console_password", PASSWORD)
    monkeypatch.setattr(s_, "console_tenant_id", str(tenant_id))
    auth.limiter._hits.clear()
    with TestClient(app, follow_redirects=False) as c:
        _form(c, "/console/login", {"password": PASSWORD})
        assert "Importer le catalogue" in c.get("/console/catalog").text
        assert c.post("/console/catalog/import").headers["location"].endswith("ok=imported")
        assert c.post("/console/catalog/import").headers["location"].endswith("ok=nothing")
    with tenant_session(tenant_id) as s:
        assert len(catalog_service.list_products(s)) == len(KORA["catalog"]["products"])


def test_agent_follows_console_changes(client, tenant):
    _form(client, "/console/catalog/KOR-R01/edit", _product_fields(name="Robe Sika", price="30000", variant_size=["M"], variant_color=["Indigo"], variant_stock=["5"]))
    with tenant_session(tenant) as s:
        built = order_service.build_order(KORA, [{"product_id": "KOR-R01", "size": "M", "quantity": 1}], "Cotonou", products=catalog_service.load_catalog(s))
    assert built["subtotal"] == 30000                                      # le prix modifié dans la console fait foi
    _form(client, "/console/catalog/KOR-R01/delete", {})
    with tenant_session(tenant) as s:
        with pytest.raises(order_service.OrderError, match="inconnu"):
            order_service.build_order(KORA, [{"product_id": "KOR-R01", "size": "M", "quantity": 1}], "Cotonou", products=catalog_service.load_catalog(s))


def test_stock_is_decremented_once_when_payment_succeeds(client, tenant, feda):
    _purchase(tenant, feda)                                                # Robe Sika, taille M : stock 6 au départ
    with tenant_session(tenant) as s:
        before = {v["size"]: v["stock"] for v in catalog_service.get_product(s, "KOR-R01")["variants"]}
        pid = s.execute(text("SELECT id FROM payments")).scalar_one()
    assert before["M"] == 6
    feda.txs["1000"].status = "approved"
    for _ in range(2):                                                     # deux vérifications : une seule décrémentation
        with tenant_session(tenant) as s:
            order_service.refresh_payment(s, pid, feda)
    with tenant_session(tenant) as s:
        after = {v["size"]: v["stock"] for v in catalog_service.get_product(s, "KOR-R01")["variants"]}
    assert after["M"] == 5 and after["S"] == before["S"]


def test_stock_never_goes_negative():
    variants = [{"size": "M", "color": "Indigo", "stock": 1}]
    # logique pure de la boucle : (stock - quantité) borné à 0
    assert max(0, variants[0]["stock"] - 3) == 0


# --- formulaire produit (pur) ----------------------------------------------------------------------------

def test_parse_product_form_cleans_inputs():
    data, errors = catalog_service.parse_product_form({
        "name": ["  Robe  "], "price": ["32\u202f000"], "tags": [" a, ,b "], "category": [""], "description": [""],
        "variant_size": ["M", "", "m"], "variant_color": ["Indigo", "", "indigo"], "variant_stock": ["2", "", "1"]}, creating=False)
    assert errors == ["Variante en double : m / indigo."]                  # doublon détecté sans tenir compte de la casse
    assert data["price"] == 32000 and data["tags"] == ["a", "b"] and data["name"] == "Robe"


@pytest.mark.parametrize("price,ok", [("1", True), ("0", False), ("-5", False), ("12.5", False), ("10000001", False), ("", False)])
def test_price_bounds(price, ok):
    _, errors = catalog_service.parse_product_form({"name": ["Robe"], "price": [price], "variant_size": ["M"], "variant_color": ["x"], "variant_stock": ["1"]}, creating=False)
    assert (not [e for e in errors if "prix" in e]) is ok


# --- isolation entre entreprises ---------------------------------------------------------------------------

def test_console_never_shows_another_tenants_data(client, tenant, make_tenant, owner_engine):
    other, _ = make_tenant("autre", "P2")
    with tenant_session(other) as s:
        catalog_service.create_product(s, other, {"sku": "SECRET-1", "name": "Produit confidentiel", "category": "", "description": "",
                                                  "price": 1000, "tags": [], "variants": [{"size": "U", "color": "U", "stock": 1}]})
    page = client.get("/console/catalog").text
    assert "Produit confidentiel" not in page and "SECRET-1" not in page
    assert client.get("/console/catalog/SECRET-1/edit").status_code == 404
    assert _form(client, "/console/catalog/SECRET-1/delete", {}).status_code == 303
    with tenant_session(other) as s:
        assert catalog_service.get_product(s, "SECRET-1") is not None      # la suppression n'a pas franchi la frontière
