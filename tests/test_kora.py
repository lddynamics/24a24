"""Catalogue, prompt de marque et vocaux transcrits (démo Maison Kora)."""
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.agent.orchestrator import to_llm_messages
from app.agent.prompt import build_system_prompt
from app.agent.tools import CatalogInput, search_catalog
from app.services.transcribe import TranscriptionError, WhisperTranscriber
from scripts.set_tenant_config import set_tenant_config
from tests.seed import seed_catalog
from tests.fakes import FakeLLM, FakeSender, text_reply, tool_reply
from tests.test_agent import _drain, _in, _rows

KORA_FULL = json.loads(Path("tenants/maison-kora.json").read_text(encoding="utf-8"))
# Configuration « catalogue seul » (avant l'activation des commandes) : sert aux tests du prompt de base.
KORA = {**KORA_FULL, "tools_enabled": ["get_business_info", "get_catalog", "escalate_to_human"]}


def _catalog(**kw):
    return search_catalog(KORA["catalog"]["products"], **kw)


# --- catalogue -----------------------------------------------------------------------------

def test_catalog_search_ignores_accents_and_plurals():
    out = _catalog(query="robes de soiree")
    names = [p["name"] for p in out["products"]]
    assert names[0] == "Robe Fiogbé de soirée"


def test_catalog_hides_out_of_stock_variants_and_flags_low_stock():
    [sika] = [p for p in _catalog(query="Sika")["products"]]
    assert {v["size"] for v in sika["available_variants"]} == {"S", "M", "L"}   # XL à 0 : absent
    [fiogbe] = _catalog(query="Fiogbé")["products"]
    assert fiogbe["low_stock"] is True
    assert sika["low_stock"] is False


def test_catalog_filters_by_category_and_price():
    out = _catalog(category="accessoires", max_price=10000)
    assert [p["id"] for p in out["products"]] == ["KOR-A01"]


def test_catalog_no_match_lists_categories_instead_of_inventing():
    out = _catalog(query="chaussures")
    assert out["products"] == [] and "robes" in out["categories"]


def test_catalog_rejects_unknown_parameters():
    with pytest.raises(Exception):
        CatalogInput(tenant_id="x")


# --- prompt --------------------------------------------------------------------------------

def test_prompt_contains_brand_and_sales_method_but_no_unavailable_capabilities():
    p = build_system_prompt("Maison Kora", "commerce", KORA)
    assert "Maison Kora est une maison de mode" in p and "vouvoiement" in p
    assert "conseiller de vente" in p and "[Message vocal transcrit]" in p
    assert "create_payment_link" not in p and "Prise de commande" not in p  # outils pas encore activés


def test_prompt_adds_order_and_payment_modules_only_when_tools_enabled():
    cfg = {**KORA, "tools_enabled": KORA["tools_enabled"] + ["create_order", "create_payment_link", "check_payment_status"]}
    p = build_system_prompt("Maison Kora", "commerce", cfg)
    assert "Prise de commande" in p and "Tu ne dis JAMAIS qu'un paiement est reçu" in p


# --- vocaux --------------------------------------------------------------------------------

def test_transcript_is_labelled_for_the_model():
    msgs = to_llm_messages([("in", "audio", "Je voudrais la robe Sika en M")])
    assert msgs == [{"role": "user", "content": "[Message vocal transcrit] Je voudrais la robe Sika en M"}]


def _transcriber(handler):
    return WhisperTranscriber("key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_transcriber_downloads_then_posts_audio_to_whisper():
    seen = {}

    def handler(req: httpx.Request):
        if req.url.host == "cdn.example":
            return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mp4"})
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.read()
        return httpx.Response(200, json={"text": "  Bonjour, je cherche une robe.  "})

    assert _transcriber(handler).transcribe_url("https://cdn.example/a") == "Bonjour, je cherche une robe."
    assert seen["auth"] == "Bearer key"
    assert b'name="model"' in seen["body"] and b"voice.m4a" in seen["body"] and b"AUDIO" in seen["body"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_transcriber_api_errors_raise(status):
    def handler(req):
        return httpx.Response(200, content=b"A") if req.url.host == "cdn.example" else httpx.Response(status, text="no")

    with pytest.raises(TranscriptionError):
        _transcriber(handler).transcribe_url("https://cdn.example/a")


def test_transcriber_refuses_non_https_and_oversized_audio():
    with pytest.raises(TranscriptionError):
        _transcriber(lambda r: httpx.Response(200)).transcribe_url("http://cdn.example/a")
    big = WhisperTranscriber("k", max_bytes=3, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"12345"))))
    with pytest.raises(TranscriptionError):
        big.transcribe_url("https://cdn.example/a")


class FakeTranscriber:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, []

    def transcribe_url(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.text


@pytest.fixture
def kora(make_tenant, owner_engine):
    tenant_id, _ = make_tenant("kora", "P1")
    set_tenant_config(owner_engine, tenant_id, KORA)
    seed_catalog(tenant_id, KORA)
    return tenant_id


def test_voice_message_is_transcribed_stored_and_answered(kora):
    from app.worker import run_once

    _in("U1", "", "m1", kind="audio")
    llm = FakeLLM(text_reply("Avec plaisir ! La robe Sika existe en S, M et L."))
    sender, tr = FakeSender(), FakeTranscriber("Je voudrais voir la robe Sika")
    while run_once(llm, sender.factory(), tr):
        pass
    assert tr.calls == ["https://cdn.example/x"]
    assert llm.calls[0]["messages"][0]["content"] == "[Message vocal transcrit] Je voudrais voir la robe Sika"
    [m] = _rows(kora, "SELECT text, type FROM messages WHERE direction = 'in'")
    assert (m.text, m.type) == ("Je voudrais voir la robe Sika", "audio")
    assert sender.sent[0][1].startswith("Avec plaisir")


def test_failed_transcription_falls_back_to_polite_request(kora):
    from app.worker import run_once

    _in("U1", "", "m1", kind="audio")
    llm = FakeLLM(text_reply("Pourriez-vous m'écrire votre demande ?"))
    sender = FakeSender()
    while run_once(llm, sender.factory(), FakeTranscriber(error=TranscriptionError("panne"))):
        pass
    assert "message vocal" in llm.calls[0]["messages"][0]["content"]
    assert len(sender.sent) == 1


def test_kora_full_catalog_flow_uses_tool_result(kora):
    _in("U1", "Vous avez une robe de soirée ?", "m1")
    llm = FakeLLM(
        tool_reply("get_catalog", {"query": "robe soirée"}),
        text_reply("La robe Fiogbé en pagne tissé émeraude, 45 000 FCFA. Il ne reste que peu de pièces."),
    )
    sender = FakeSender()
    _drain(llm, sender)
    tool_out = llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert "KOR-R03" in tool_out and "45000" in tool_out
    assert {t["name"] for t in llm.calls[0]["tools"]} == {"get_business_info", "get_catalog", "escalate_to_human"}


# --- modèles gratuits (Groq) : conversion de format et routage --------------------------------

from app.agent.llm import LLMError, OpenAICompatLLM, RoutingLLM, to_openai_messages, to_openai_tools  # noqa: E402


def test_anthropic_history_is_converted_to_openai_format():
    history = [
        {"role": "user", "content": "Une robe de soirée ?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Je regarde."},
            {"type": "tool_use", "id": "t1", "name": "get_catalog", "input": {"query": "soirée"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": '{"products": []}'}]},
    ]
    out = to_openai_messages("SYS", history)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "get_catalog", "arguments": '{"query": "soirée"}'}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": '{"products": []}'}


def test_tool_schemas_are_simplified_for_open_models():
    schema = CatalogInput.model_json_schema()
    [tool] = to_openai_tools([{"name": "get_catalog", "description": "d", "input_schema": schema}])
    params = tool["function"]["parameters"]
    assert "title" not in params and "anyOf" not in params["properties"]["query"]
    assert params["properties"]["query"]["type"] == ["string", "null"]


def _groq(handler):
    return OpenAICompatLLM("k", _http=httpx.Client(transport=httpx.MockTransport(handler)), _sleep=lambda s: None)


def test_openai_compat_reply_becomes_anthropic_style_blocks():
    def handler(req):
        body = json.loads(req.read())
        assert body["model"] == "llama-3.3-70b-versatile" and body["tools"][0]["type"] == "function"
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": "<think>hmm</think>",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_catalog", "arguments": '{"query":"robe"}'}}],
            }}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    tools = [{"name": "get_catalog", "description": "d", "input_schema": CatalogInput.model_json_schema()}]
    r = _groq(handler).create(model="llama-3.3-70b-versatile", system="S", tools=tools,
                              messages=[{"role": "user", "content": "x"}], max_tokens=100)
    assert r.text == "" and r.stop_reason == "tool_use" and (r.input_tokens, r.output_tokens) == (100, 20)
    assert r.tool_calls[0].input == {"query": "robe"}
    assert r.content_blocks == [{"type": "tool_use", "id": "c1", "name": "get_catalog", "input": {"query": "robe"}}]


def test_openai_compat_http_error_is_retryable_llm_error():
    with pytest.raises(LLMError):
        _groq(lambda r: httpx.Response(429, text="rate limit")).create(
            model="m", system="S", tools=[], messages=[{"role": "user", "content": "x"}], max_tokens=10)


def test_router_picks_provider_from_model_name_and_requires_matching_key():
    router = RoutingLLM(anthropic_key="", groq_key="g")
    assert isinstance(router._client_for("llama-3.3-70b-versatile"), OpenAICompatLLM)
    with pytest.raises(LLMError):
        router._client_for("claude-sonnet-5-5")        # pas de clé Anthropic : erreur claire
    with pytest.raises(LLMError):
        RoutingLLM(anthropic_key="a", groq_key="")._client_for("openai/gpt-oss-120b")


# --- défauts observés sur la vraie page de test -------------------------------------------------

from app.agent.text import clean_for_messenger  # noqa: E402


def test_optional_tool_params_still_accept_null_for_open_models():
    [tool] = to_openai_tools([{"name": "get_catalog", "description": "d", "input_schema": CatalogInput.model_json_schema()}])
    assert tool["function"]["parameters"]["properties"]["category"]["type"] == ["string", "null"]
    assert CatalogInput(query="robe", category=None, max_price=None).category is None   # côté serveur aussi


def test_markdown_is_stripped_for_messenger():
    raw = "Vous avez choisi la **Robe Sika**.\n\n### Détails\n* Taille **M**\n* Couleur *indigo*"
    assert clean_for_messenger(raw) == "Vous avez choisi la Robe Sika.\n\nDétails\n- Taille M\n- Couleur indigo"


def test_prompt_forbids_exact_stock_and_fake_payment_until_tools_exist():
    p = build_system_prompt("Maison Kora", "commerce", KORA)
    assert "nombre exact de pièces" in p
    assert "Commande en ligne : pas encore activée" in p and "Ne promets jamais un lien de paiement" in p
    full = {**KORA, "tools_enabled": KORA["tools_enabled"] + ["create_order", "create_payment_link"]}
    assert "pas encore activée" not in build_system_prompt("Maison Kora", "commerce", full)


# --- réponses tronquées et modèles à raisonnement ---------------------------------------------------

def _ok(content="Bonjour", finish="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}], "usage": {}})


def _call(llm, model="openai/gpt-oss-120b"):
    return llm.create(model=model, system="S", tools=[], messages=[{"role": "user", "content": "x"}], max_tokens=1000)


def test_truncated_reply_is_never_sent():
    with pytest.raises(LLMError, match="tronquée"):
        _call(_groq(lambda r: _ok("…livraison à Coton", finish="length")))


def test_reasoning_models_get_room_and_low_effort_but_others_do_not():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.read()))
        return _ok()

    _call(_groq(handler), "openai/gpt-oss-120b")
    _call(_groq(handler), "llama-3.3-70b-versatile")
    assert bodies[0]["max_tokens"] >= 3000 and bodies[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in bodies[1]


def test_reasoning_effort_rejected_by_provider_is_retried_without_it():
    seen = []

    def handler(req):
        body = json.loads(req.read())
        seen.append("reasoning_effort" in body)
        return httpx.Response(400, text="unknown parameter reasoning_effort") if "reasoning_effort" in body else _ok()

    assert _call(_groq(handler)).text == "Bonjour" and seen == [True, False]


# --- fournisseur configurable (NVIDIA NIM) et patience sur 429 -------------------------------------

def test_agent_provider_is_configurable_and_groq_stays_as_fallback():
    nim = RoutingLLM(anthropic_key="", groq_key="g", llm_key="nvapi-x", llm_base_url="https://integrate.api.nvidia.com/v1")
    c = nim._client_for("nvidia/nemotron-3-super-120b-a12b")
    assert isinstance(c, OpenAICompatLLM) and c.api_key == "nvapi-x" and c.base_url == "https://integrate.api.nvidia.com/v1"
    old = RoutingLLM(anthropic_key="", groq_key="g")._client_for("llama-3.3-70b-versatile")
    assert old.api_key == "g" and "groq" in old.base_url
    with pytest.raises(LLMError):
        RoutingLLM(anthropic_key="a")._client_for("meta/llama-3.3-70b-instruct")


def test_rate_limit_is_waited_out_using_retry_after_then_succeeds():
    calls, waits = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "7"}, text="slow down") if len(calls) < 3 else _ok()

    llm = OpenAICompatLLM("k", _http=httpx.Client(transport=httpx.MockTransport(handler)), _sleep=waits.append)
    assert _call(llm).text == "Bonjour" and waits == [7.0, 7.0] and len(calls) == 3


def test_rate_limit_wait_is_capped_and_persistent_429_still_raises():
    waits = []
    llm = OpenAICompatLLM("k", _http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"retry-after": "600"}, text="x"))), _sleep=waits.append)
    with pytest.raises(LLMError, match="429"):
        _call(llm)
    assert waits == [20.0, 20.0]
