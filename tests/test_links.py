"""Le modèle ne touche jamais aux liens de paiement : le backend les injecte, tout le reste est neutralisé."""
import json

import pytest
from sqlalchemy import text

from app.agent.links import LINK_TOKEN, finalize_reply, has_link_reference, mask_urls
from app.agent.orchestrator import FALLBACK_PAYMENT_LINK, to_llm_messages
from tests.fakes import FakeLLM, FakeSender, text_reply
from tests.test_agent import _drain, _in, _rows
from tests.test_payments import _purchase, feda, kora  # noqa: F401  (fixtures)

# Vrai lien FedaPay : jeton JWT avec « _ » et « - », exactement ce que le modèle déformait.
REAL = (
    "https://sandbox-process.fedapay.com/eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJzdWIiOjU0MDA0OSwiZXhwIjoxMTk0MzAyNDEzfQ.2Uy4E-wRqpcTCcrwJuuxB04P6-1Rwxn_tQW"
)


# --- finalize_reply (pur) -----------------------------------------------------------------------

def test_token_is_replaced_by_the_exact_real_url():
    out = finalize_reply(f"Voici votre lien de paiement : {LINK_TOKEN}\nMerci !", REAL)
    assert out.ok and out.text == f"Voici votre lien de paiement : {REAL}\nMerci !"


@pytest.mark.parametrize("variant", ["{{ LIEN_PAIEMENT }}", "[LIEN_PAIEMENT]", "<LIEN_PAIEMENT>", "{LIEN PAIEMENT}"])
def test_token_variants_written_by_models_are_accepted(variant):
    assert finalize_reply(f"Lien : {variant}", REAL).text == f"Lien : {REAL}"


@pytest.mark.parametrize("fake", [
    "https://pay.maisonkora.com/pay/KOR-261109-2779",                      # lien inventé (capture 3)
    REAL.replace("fedapay", "fedipay"),                                    # domaine déformé (capture 4)
    REAL[:-10],                                                            # lien tronqué
    REAL + "AAA",                                                          # lien complété à tort
    "pay.maisonkora.com/pay/KOR-261109-2779",                              # sans schéma
])
def test_invented_or_altered_url_is_replaced_by_the_real_one(fake):
    out = finalize_reply(f"Voici le nouveau lien de paiement : {fake}\nMerci.", REAL)
    assert out.ok and out.text == f"Voici le nouveau lien de paiement : {REAL}\nMerci."
    assert out.removed_urls


def test_real_url_written_correctly_is_kept_once_and_trailing_punctuation_is_preserved():
    out = finalize_reply(f"Voici le lien ({REAL}).", REAL)
    assert out.text == f"Voici le lien ({REAL})." and not out.removed_urls


def test_link_generated_but_missing_from_reply_is_appended():
    out = finalize_reply("Votre commande est enregistrée, total 34 000 FCFA.", REAL)
    assert out.ok and out.text.endswith(f"\n\n{REAL}")


def test_duplicate_links_are_collapsed():
    out = finalize_reply(f"{LINK_TOKEN} puis encore {REAL}", REAL)
    assert out.text.count(REAL) == 1


def test_url_without_any_real_link_is_not_sendable():
    out = finalize_reply("Voici le lien : https://pay.maisonkora.com/x", None)
    assert not out.ok and "maisonkora" not in out.text
    assert not finalize_reply(f"Voici le lien : {LINK_TOKEN}", None).ok


def test_replies_without_links_are_untouched():
    msg = "La robe Sika est à 32 000 FCFA, tailles S/M/L. Ratio 3.5/5 chez nos clientes."
    assert not has_link_reference(msg)
    assert finalize_reply(msg, None).text == msg


def test_allowed_domains_survive():
    out = finalize_reply("Suivez-nous : https://www.maisonkora.com/boutique", None, allowed_domains=["maisonkora.com"])
    assert out.ok and "https://www.maisonkora.com/boutique" in out.text


def test_email_addresses_are_not_mistaken_for_links():
    assert not has_link_reference("Écrivez-nous à contact@maisonkora.com")


# --- historique : le modèle ne voit jamais une vraie URL --------------------------------------------

def test_history_masks_real_urls_in_assistant_messages_only():
    rows = [
        ("in", "text", "Je veux payer"),
        ("out", "text", f"Voici le lien : {REAL}\nVous pouvez régler par mobile money."),
        ("in", "text", f"Je préfère regarder {REAL}"),
    ]
    msgs = to_llm_messages(rows)
    assert REAL not in json.dumps(msgs[1], ensure_ascii=False)
    assert LINK_TOKEN in msgs[1]["content"]
    assert REAL in msgs[2]["content"]          # le message du CLIENT n'est pas modifié


# --- bout en bout (base de test) -------------------------------------------------------------------

def test_model_never_sees_the_url_and_client_gets_the_exact_real_link(kora, feda):  # noqa: F811
    llm, sender = _purchase(kora, feda)
    with_url = [c for c in llm.calls if "sandbox-process" in json.dumps(c["messages"], ensure_ascii=False)]
    assert not with_url                                              # aucun appel au modèle ne contient l'URL
    [pay] = _rows(kora, "SELECT payment_url FROM payments")
    assert sender.sent[0][1].count(pay.payment_url) == 1             # le client reçoit l'URL exacte de la base
    [msg] = _rows(kora, "SELECT text FROM messages WHERE direction = 'out'")
    assert pay.payment_url in msg.text                               # et la console l'affiche aussi


def test_invented_link_after_a_purchase_is_replaced_by_the_pending_real_one(kora, feda):  # noqa: F811
    _purchase(kora, feda)
    _in("U1", "le lien ne marche pas", "m2")
    sender = FakeSender()
    _drain(FakeLLM(text_reply("Voici le nouveau lien : https://pay.maisonkora.com/pay/KOR-261109-2779")), sender)
    [pay] = _rows(kora, "SELECT payment_url FROM payments")
    assert sender.sent[-1][1] == f"Voici le nouveau lien : {pay.payment_url}"


def test_invented_link_with_no_real_link_escalates_instead_of_sending(kora):  # noqa: F811
    # Conversation sans aucune commande : un lien annoncé ne peut être qu'inventé.
    _in("U9", "je veux payer", "m9")
    sender = FakeSender()
    _drain(FakeLLM(text_reply("Voici votre lien : https://pay.maisonkora.com/pay/ABC")), sender)
    assert sender.sent[-1][1] == FALLBACK_PAYMENT_LINK
    assert _rows(kora, "SELECT status FROM conversations WHERE status = 'human'")
