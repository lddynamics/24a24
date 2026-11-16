"""Abstraction du modèle de langage : permet de tester sans appel réseau et de changer de fournisseur."""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall]
    content_blocks: list[dict]      # tel quel, pour rejouer le tour de l'assistant dans l'historique
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def create(self, *, model: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int) -> LLMReply: ...


@dataclass
class AnthropicLLM:
    api_key: str
    _client: object = field(default=None, repr=False)

    def __post_init__(self):
        import anthropic

        self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=2, timeout=60)

    def create(self, *, model, system, tools, messages, max_tokens) -> LLMReply:
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            # La partie stable du prompt est marquée pour le cache (réduit coût et latence).
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)

        text_parts, calls, blocks = [], [], []
        for b in resp.content:
            if b.type == "text":
                text_parts.append(b.text)
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                calls.append(ToolCall(id=b.id, name=b.name, input=dict(b.input)))
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        return LLMReply(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            content_blocks=blocks,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


# --- Modèles gratuits / OpenAI-compatibles (Groq) pour les tests --------------------------------

class LLMError(Exception):
    """Erreur du fournisseur de modèle : le job sera réessayé avec reprise espacée."""


def _simplify_schema(node):
    """Rend un schéma JSON (Pydantic) plus digeste pour les modèles ouverts : sans « title »,
    et `anyOf [X, null]` ramené à `type: [X, "null"]`."""
    if isinstance(node, list):
        return [_simplify_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k != "title"}
    any_of = node.get("anyOf")
    if any_of:
        non_null = [a for a in any_of if a.get("type") != "null"]
        if len(non_null) == 1 and isinstance(non_null[0].get("type"), str):
            # Garde la possibilité de null : les modèles ouverts envoient souvent null pour un champ optionnel,
            # et l'API du fournisseur rejette la requête si le schéma ne l'autorise pas.
            merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **non_null[0]}
            merged["type"] = [non_null[0]["type"], "null"]
            return _simplify_schema(merged)
    return {k: _simplify_schema(v) for k, v in node.items()}


def _inline_refs(schema: dict) -> dict:
    """Remplace les références `$ref` vers `$defs` par leur définition : beaucoup de modèles ouverts
    ne savent pas les suivre (schémas imbriqués, ex. la liste d'articles d'une commande)."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return walk({**defs[ref.rsplit("/", 1)[1]], **{k: v for k, v in node.items() if k != "$ref"}})
        return {k: walk(v) for k, v in node.items() if k != "$defs"}

    return walk(schema)


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _simplify_schema(_inline_refs(t["input_schema"])),
            },
        }
        for t in tools
    ]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Convertit l'historique au format Anthropic (blocs text / tool_use / tool_result) vers le format OpenAI."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            texts = [b["text"] for b in content if b["type"] == "text"]
            calls = [
                {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                for b in content
                if b["type"] == "tool_use"
            ]
            msg: dict = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in content:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b["content"])})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


@dataclass
class OpenAICompatLLM:
    """Client pour toute API compatible OpenAI /chat/completions (Groq par défaut)."""

    api_key: str
    base_url: str = "https://api.groq.com/openai/v1"
    temperature: float = 0.3
    max_rate_limit_retries: int = 2   # 429 : on patiente sur place (Retry-After) au lieu d'échouer tout le job
    max_wait_seconds: float = 20.0
    _http: object = field(default=None, repr=False)
    _sleep: object = field(default=time.sleep, repr=False)

    def __post_init__(self):
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=60)

    def _post_once(self, body: dict):
        import httpx

        try:
            return self._http.post(
                f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=body
            )
        except httpx.HTTPError as e:
            raise LLMError(f"fournisseur injoignable: {type(e).__name__}") from e

    @staticmethod
    def _retry_after(r) -> float | None:
        raw = r.headers.get("retry-after")
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    def _post(self, body: dict):
        """POST avec patience sur 429 : on attend le délai indiqué par le fournisseur (plafonné) puis on réessaie.
        Au-delà, l'erreur remonte et le job est replanifié par la file (backoff)."""
        r = self._post_once(body)
        for attempt in range(self.max_rate_limit_retries):
            if r.status_code != 429:
                break
            wait = self._retry_after(r)
            wait = min(wait if wait is not None else 3.0 * (attempt + 1), self.max_wait_seconds)
            self._sleep(max(wait, 0.0))
            r = self._post_once(body)
        return r

    def create(self, *, model, system, tools, messages, max_tokens) -> LLMReply:
        body: dict = {
            "model": model,
            "messages": to_openai_messages(system, messages),
            # Les modèles « à raisonnement » (gpt-oss) comptent leur réflexion dans max_tokens : marge large,
            # sinon la réponse est coupée net (ex. « …livraison à Coton »).
            "max_tokens": max(max_tokens, 3000),
            "temperature": self.temperature,
        }
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"   # réponses plus rapides pour de la conversation de vente
        if tools:
            body["tools"] = to_openai_tools(tools)
            body["tool_choice"] = "auto"
        r = self._post(body)
        if r.status_code == 400 and "reasoning_effort" in r.text and "reasoning_effort" in body:
            body.pop("reasoning_effort")           # paramètre non accepté par ce modèle : on réessaie sans
            r = self._post(body)
        if r.status_code != 200:
            raise LLMError(f"{r.status_code}: {r.text[:200]}")
        data = r.json()
        if data["choices"][0].get("finish_reason") == "length":
            # Jamais d'envoi d'une réponse tronquée : le job sera réessayé.
            raise LLMError("réponse tronquée (limite de tokens atteinte)")
        msg = data["choices"][0]["message"]
        text = _THINK.sub("", msg.get("content") or "").strip()
        calls, blocks = [], []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], input=args))
            blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
        usage = data.get("usage") or {}
        return LLMReply(
            text=text,
            tool_calls=calls,
            content_blocks=blocks,
            stop_reason=_STOP.get(data["choices"][0].get("finish_reason"), data["choices"][0].get("finish_reason")),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


@dataclass
class RoutingLLM:
    """Choisit le fournisseur d'après le nom du modèle : « claude-… » -> Anthropic, sinon Groq.

    Permet de faire tourner en parallèle un client de test sur un modèle gratuit et un autre
    sur Claude (config.agent.model par client), puis de basculer par simple changement de modèle.
    """

    anthropic_key: str = ""
    groq_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Fournisseur compatible OpenAI choisi pour l'agent (NVIDIA NIM…). S'il est vide, on utilise Groq.
    llm_key: str = ""
    llm_base_url: str = ""
    _anthropic: object = field(default=None, repr=False)
    _groq: object = field(default=None, repr=False)

    def _client_for(self, model: str):
        if model.startswith("claude"):
            if not self.anthropic_key:
                raise LLMError(f"ANTHROPIC_API_KEY manquante pour le modèle {model}")
            if self._anthropic is None:
                self._anthropic = AnthropicLLM(self.anthropic_key)
            return self._anthropic
        key = self.llm_key or self.groq_key
        if not key:
            raise LLMError(f"LLM_API_KEY (ou GROQ_API_KEY) manquante pour le modèle {model}")
        if self._groq is None:
            self._groq = OpenAICompatLLM(key, base_url=self.llm_base_url or self.groq_base_url)
        return self._groq

    @classmethod
    def from_settings(cls, s) -> "RoutingLLM":
        return cls(s.anthropic_api_key, s.groq_api_key, s.groq_base_url, llm_key=s.llm_api_key, llm_base_url=s.llm_base_url)

    def create(self, *, model, system, tools, messages, max_tokens) -> LLMReply:
        return self._client_for(model).create(
            model=model, system=system, tools=tools, messages=messages, max_tokens=max_tokens
        )
