"""Confirmation de paiement envoyée par le backend : sans modèle, sans que le client ait à écrire, même sans webhook."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent.confirmation import delivery_delay, fcfa, payment_confirmation_text
from app.agent.orchestrator import handle_job
from app.channels.sender import SendError
from app.db import tenant_session
from app.main import app
from app.services import orders as order_service
from app.services.jobs import claim_job
from tests.fakes import FakeSender
from tests.test_agent import _drain, _rows
from tests.test_payments import KORA_CFG, _event, _purchase, _signed, feda, kora  # noqa: F401  (fixtures)


class NoModel:
    def __init__(self):
        self.calls = 0

    def create(self, **_):
        self.calls += 1
        raise AssertionError("le modèle ne doit pas être appelé pour confirmer un paiement")


def _approve_by_webhook(feda):
    feda.txs["1000"].status = "approved"
    body, headers = _signed(_event())
    with TestClient(app) as c:
        assert c.post("/webhooks/fedapay", content=body, headers=headers).status_code == 200


def _confirmations(kora):
    return _rows(kora, "SELECT text, delivery_status FROM messages WHERE idempotency_key LIKE 'payment_confirmed:%'")


# --- texte (pur) -------------------------------------------------------------------------------------

ORDER = {
    "reference": "KOR-261109-1706", "customer_name": "Awa Koffi", "delivery_zone": "Cotonou",
    "delivery_address": "Fidjrossè, près de l'église", "delivery_fee": 1500, "total": 91500,
    "items": [
        {"name": "Robe Fiogbé de soirée", "size": "S", "color": "Émeraude", "quantity": 2, "unit_price": 45000, "line_total": 90000},
    ],
}


def test_confirmation_text_is_complete_and_exact():
    msg = payment_confirmation_text(ORDER, KORA_CFG, "Maison Kora")
    assert msg.startswith("Merci Awa, votre paiement est confirmé")
    for expected in ("KOR-261109-1706", "2 × Robe Fiogbé de soirée (taille S, Émeraude) — 90 000 FCFA",
                     "Frais de livraison (Cotonou) : 1 500 FCFA", "Total payé : 91 500 FCFA",
                     "Fidjrossè, près de l'église", "Délai estimé : 24 à 48 heures", "À bientôt chez Maison Kora"):
        assert expected in msg


def test_unknown_delay_is_omitted_never_invented():
    assert delivery_delay(KORA_CFG, "Parakou") is None
    msg = payment_confirmation_text({**ORDER, "delivery_zone": "Parakou"}, KORA_CFG)
    assert "Délai" not in msg and fcfa(91500) == "91 500 FCFA"


# --- flux : webhook -> message du backend -----------------------------------------------------------

def test_webhook_triggers_confirmation_without_model_and_exactly_once(kora, feda):  # noqa: F811
    _purchase(kora, feda)
    _approve_by_webhook(feda)
    _approve_by_webhook(feda)                                  # FedaPay renvoie parfois l'événement
    model, sender = NoModel(), FakeSender()
    _drain(model, sender)
    _drain(model, sender)
    assert model.calls == 0
    [(body, status)] = _confirmations(kora)
    assert status == "sent" and sender.sent[-1][1] == body
    assert "paiement est confirmé" in body and "Total payé : 34 000 FCFA" in body and "Quartier Zoca" in body
    assert sum("paiement est confirmé" in m for _, m in sender.sent) == 1


def test_confirmation_is_sent_even_if_a_human_has_the_conversation(kora, feda):  # noqa: F811
    _purchase(kora, feda)
    with tenant_session(kora) as s:
        s.execute(text("UPDATE conversations SET status = 'human'"))
    _approve_by_webhook(feda)
    sender = FakeSender()
    _drain(NoModel(), sender)
    assert "paiement est confirmé" in sender.sent[-1][1]


def test_failed_send_is_retried_without_duplicate(kora, feda):  # noqa: F811
    _purchase(kora, feda)
    _approve_by_webhook(feda)
    job = claim_job()
    with pytest.raises(SendError):
        handle_job(job, llm=NoModel(), sender_factory=FakeSender(fail_times=1).factory())
    ok = FakeSender()
    assert handle_job(job, llm=NoModel(), sender_factory=ok.factory()) == "payment_confirmed"
    assert handle_job(job, llm=NoModel(), sender_factory=ok.factory()) == "skipped:already_confirmed"
    assert len(ok.sent) == 1 and len(_confirmations(kora)) == 1


# --- filet de sécurité : pas de webhook ---------------------------------------------------------------

def test_reconciliation_confirms_payment_when_the_webhook_never_arrives(kora, feda):  # noqa: F811
    _purchase(kora, feda)
    feda.txs["1000"].status = "approved"                       # le client a payé, mais AUCUN webhook reçu
    assert order_service.reconcile_pending_payments(feda, min_age_seconds=0) == 1
    assert order_service.reconcile_pending_payments(feda, min_age_seconds=0) == 0     # une seule fois
    model, sender = NoModel(), FakeSender()
    _drain(model, sender)
    assert "paiement est confirmé" in sender.sent[-1][1] and model.calls == 0
    assert _rows(kora, "SELECT status FROM orders")[0].status == "paid"
    assert _rows(kora, "SELECT status FROM payment_routes")[0].status == "succeeded"
    assert len(_confirmations(kora)) == 1


def test_reconciliation_leaves_unpaid_old_and_fresh_payments_alone(kora, feda):  # noqa: F811
    _purchase(kora, feda)
    assert order_service.reconcile_pending_payments(feda, min_age_seconds=0) == 0     # toujours « pending » chez FedaPay
    feda.txs["1000"].status = "approved"
    assert order_service.reconcile_pending_payments(feda) == 0                        # trop récent (< 15 s) : on attend
    assert order_service.reconcile_pending_payments(feda, min_age_seconds=0, window_minutes=0) == 0   # hors fenêtre
    assert _rows(kora, "SELECT status FROM payments")[0].status == "pending"


def test_reconciliation_survives_a_fedapay_error_on_one_payment(kora, feda):  # noqa: F811
    _purchase(kora, feda)

    class Broken:
        def get_transaction(self, tid):
            raise order_service.fp.FedaPayError("API indisponible")

    assert order_service.reconcile_pending_payments(Broken(), min_age_seconds=0) == 0
    assert _rows(kora, "SELECT status FROM payments")[0].status == "pending"
