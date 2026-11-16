"""Diagnostic du fournisseur LLM (NVIDIA NIM, Gemini, OpenRouter, Groq…).

    python -m scripts.llm_diag                       # liste les modèles de votre clé
    python -m scripts.llm_diag --test                # teste (appel d'outil) une sélection de modèles courants
    python -m scripts.llm_diag --test MODELE [...]   # teste les modèles donnés

Lit LLM_API_KEY / LLM_BASE_URL dans .env (à défaut GROQ_API_KEY / GROQ_BASE_URL). Un modèle « OK » répond ET sait
appeler un outil : c'est ce qu'exige l'agent. Un 404 « Function … Not found for account » = modèle non disponible
pour votre compte : choisissez-en un autre.
"""
import json
import sys

import httpx

from app.config import get_settings

CANDIDATES = [
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.1-405b-instruct",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-nano-30b-a3b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "mistralai/mistral-large-2-instruct",
    "qwen/qwen3-235b-a22b",
]

TOOL = {
    "type": "function",
    "function": {
        "name": "get_catalog",
        "description": "Catalogue des articles.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}


def _conn() -> tuple[str, str]:
    s = get_settings()
    key, base = s.llm_api_key or s.groq_api_key, (s.llm_base_url or s.groq_base_url).rstrip("/")
    if not key:
        raise SystemExit("LLM_API_KEY manquante dans .env")
    return key, base


def list_models() -> list[str]:
    key, base = _conn()
    r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"{r.status_code}: {r.text[:300]}")
    ids = sorted(m["id"] for m in r.json().get("data", []))
    print(f"{len(ids)} modèles listés pour votre clé ({base}) :")
    for i in ids:
        print("  ", i)
    print("\nAttention : « listé » ne veut pas dire « utilisable » (un modèle peut être listé et répondre 404). "
          "Lancez --test pour vérifier.")
    return ids


def test_model(model: str) -> str:
    key, base = _conn()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Avez-vous des robes en wax ? Utilise l'outil get_catalog."}],
        "tools": [TOOL],
        "tool_choice": "auto",
        "max_tokens": 600,
        "temperature": 0.2,
    }
    try:
        r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=90)
    except httpx.HTTPError as e:
        return f"ÉCHEC  réseau: {type(e).__name__}"
    if r.status_code != 200:
        return f"ÉCHEC  {r.status_code}: {r.text[:140]}"
    msg = r.json()["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        return f"PARTIEL répond sans appeler l'outil : {(msg.get('content') or '')[:80]!r}"
    try:
        args = json.loads(calls[0]["function"]["arguments"])
    except (json.JSONDecodeError, KeyError):
        return "ÉCHEC  arguments d'outil illisibles"
    return f"OK     appel d'outil {calls[0]['function']['name']}({args})"


def main(argv: list[str]) -> None:
    if not argv:
        list_models()
        return
    if argv[0] != "--test":
        raise SystemExit(__doc__)
    models = argv[1:] or CANDIDATES
    for m in models:
        print(f"{m:48s} {test_model(m)}")


if __name__ == "__main__":
    main(sys.argv[1:])
