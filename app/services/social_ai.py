"""Rédaction d'une publication Facebook par l'IA, à partir d'une consigne et (option) d'un produit du catalogue.

Garde-fous : le modèle ne reçoit que des FAITS vérifiés (fiche produit, informations officielles de l'entreprise) ;
le texte n'est jamais publié automatiquement (la publication reste un acte humain) ; les montants cités sont comparés
au catalogue et signalés s'ils diffèrent.
"""
import re
from functools import lru_cache

from app.agent.llm import LLMClient, LLMError, RoutingLLM
from app.agent.text import clean_for_messenger
from app.config import get_settings

MAX_BRIEF = 1000
_AMOUNT = re.compile(r"(\d[\d\s\u202f\u00a0.]*\d|\d)\s*(?:FCFA|F\s?CFA|XOF|francs?)", re.IGNORECASE)

SYSTEM = """\
Tu es le community manager de l'entreprise décrite ci-dessous. Tu rédiges UNE publication pour sa page Facebook.

Règles
- Utilise UNIQUEMENT les faits fournis (consigne, fiche produit, informations officielles). N'invente aucun prix, \
stock, délai, promotion, remise, date ni caractéristique.
- Si un prix figure dans la fiche produit, cite-le tel quel, en FCFA. Sinon, ne cite aucun prix.
- Texte simple, sans Markdown, en français, de 2 à 5 phrases, avec un accroche en première ligne.
- Au plus 2 emojis et 3 mots-clics (#). Ton conforme à la marque.
- Termine par une invitation à écrire à la page en message privé pour commander ou se renseigner.
- Réponds par le texte de la publication uniquement : pas de titre, pas de guillemets, pas d'explication."""


@lru_cache
def get_llm() -> LLMClient:
    s = get_settings()
    return RoutingLLM.from_settings(s)


def _facts(tenant_name: str, config: dict, product: dict | None) -> str:
    brand, tone, info = config.get("brand") or {}, config.get("tone") or {}, config.get("business_info") or {}
    lines = [f"Entreprise : {tenant_name}."]
    if brand.get("pitch"):
        lines.append(f"Présentation : {brand['pitch']}")
    if tone.get("style"):
        lines.append(f"Ton : {tone['style']}.")
    for key in ("livraison", "paiement", "horaires"):
        if info.get(key) and isinstance(info[key], str):
            lines.append(f"{key.capitalize()} : {info[key]}")
    if isinstance(info.get("livraison"), dict):
        zones = ", ".join(f"{n} ({z['frais_fcfa']} FCFA, {z['delai']})" for n, z in (info["livraison"].get("zones") or {}).items())
        lines.append(f"Livraison : {zones}.")
    if product:
        variants = [v for v in product.get("variants", []) if int(v.get("stock", 0)) > 0]
        sizes = ", ".join(sorted({str(v.get("size")) for v in variants})) or "indisponible"
        colors = ", ".join(sorted({str(v.get("color")) for v in variants}))
        lines.append(f"Produit : {product['name']} — {product['price']} FCFA — {product.get('description', '')} "
                     f"Tailles disponibles : {sizes}. Couleurs : {colors}.")
    return "\n".join(lines)


def amounts_in(text_: str) -> list[int]:
    out = []
    for m in _AMOUNT.finditer(text_):
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            out.append(int(digits))
    return out


def price_warnings(text_: str, allowed: set[int]) -> list[str]:
    """Montants cités dans le texte qui ne correspondent à aucun prix connu du catalogue."""
    return [f"Vérifiez le montant « {a:,} FCFA » : il ne correspond pas au catalogue.".replace(",", " ")
            for a in amounts_in(text_) if a not in allowed]


def generate_post(*, tenant_name: str, config: dict, brief: str, product: dict | None, catalog_prices: set[int],
                  llm: LLMClient | None = None, model: str | None = None) -> dict:
    brief = (brief or "").strip()
    if not brief and product is None:
        raise ValueError("Décrivez ce que vous voulez annoncer ou choisissez un produit.")
    if len(brief) > MAX_BRIEF:
        raise ValueError(f"La consigne est trop longue ({MAX_BRIEF} caractères maximum).")
    model = model or (config.get("agent") or {}).get("model") or get_settings().agent_model
    user = f"Informations officielles :\n{_facts(tenant_name, config, product)}\n\nConsigne : {brief or 'Présente ce produit.'}"
    reply = (llm or get_llm()).create(model=model, system=SYSTEM, tools=[], messages=[{"role": "user", "content": user}], max_tokens=700)
    body = clean_for_messenger(reply.text or "").strip().strip('"«» ')
    if not body:
        raise LLMError("réponse vide du modèle")
    return {"text": body[:2000], "warnings": price_warnings(body, catalog_prices)}
