"""Système d'outils.

Le modèle PROPOSE un outil et ses paramètres ; le code VALIDE, vérifie les droits et EXÉCUTE.
Le tenant_id n'est jamais un paramètre d'outil : il vient du contexte serveur (ToolContext).
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agent.links import LINK_TOKEN
from app.agent.text import fold
from app.db import tenant_session
from app.models import Conversation, ToolCall
from app.services import catalog as catalog_service
from app.services import fedapay as fedapay_service
from app.services import orders as order_service

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    tenant_id: UUID
    conversation_id: UUID
    run_id: UUID
    config: dict  # configuration du client (tenants.config)
    # Lien de paiement généré pendant ce tour : gardé côté serveur, JAMAIS montré au modèle (voir app/agent/links.py).
    payment_url: str | None = None
    # Photos que l'agent a demandé d'envoyer pendant ce tour : [{"image_id", "sku", "name"}]. Le serveur les envoie,
    # avant le texte, une fois la réponse prête (voir orchestrator) ; le modèle n'a jamais accès aux octets ni aux liens.
    pending_images: list[dict] = field(default_factory=list)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")  # un paramètre inattendu (ex. tenant_id) est refusé


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[StrictInput]
    handler: Callable[[Session, ToolContext, StrictInput], dict]

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


@dataclass
class ToolResult:
    ok: bool
    output: dict


# --- get_business_info ------------------------------------------------------------------

class BusinessInfoInput(StrictInput):
    section: str | None = Field(
        default=None, description="Rubrique souhaitée (ex. 'horaires', 'services'). Vide = toutes les rubriques."
    )


def _get_business_info(_: Session, ctx: ToolContext, args: BusinessInfoInput) -> dict:
    info = ctx.config.get("business_info")
    if not info:
        return {"available": False, "message": "Aucune information officielle n'est enregistrée pour cette entreprise."}
    if args.section:
        if args.section in info:
            return {"available": True, args.section: info[args.section]}
        return {"available": False, "message": f"Rubrique inconnue. Rubriques disponibles : {sorted(info)}"}
    return {"available": True, "business_info": info, "rubriques": sorted(info)}


# --- get_catalog ------------------------------------------------------------------------

_STOPWORDS = {"une", "des", "les", "pour", "avec", "sans", "que", "qui", "dans", "sur", "mon", "ton", "son", "vos", "nos"}
LOW_STOCK_THRESHOLD = 3
MAX_RESULTS = 6


_norm = fold


def _tokens(query: str) -> list[str]:
    out = []
    for raw in _norm(query).replace("'", " ").replace("-", " ").split():
        tok = raw.rstrip("sx") if len(raw) > 3 else raw
        if len(tok) >= 3 and tok not in _STOPWORDS:
            out.append(tok)
    return out


class CatalogInput(StrictInput):
    query: str | None = Field(default=None, description="Mots-clés libres (ex. 'robe wax soirée'). Vide = tout le catalogue.")
    category: str | None = Field(default=None, description="Catégorie exacte (ex. 'robes', 'ensembles', 'accessoires').")
    max_price: int | None = Field(default=None, ge=0, description="Prix maximum en FCFA.")


def _public_product(p: dict) -> dict:
    variants = [v for v in p.get("variants", []) if int(v.get("stock", 0)) > 0]
    total = sum(int(v["stock"]) for v in variants)
    return {
        "id": p["id"],
        "name": p["name"],
        "category": p.get("category"),
        "description": p.get("description"),
        "price_fcfa": p["price"],
        "photos": int(p.get("image_count", 0)),      # nombre de photos envoyables avec send_product_image
        "in_stock": total > 0,
        "available_variants": [
            {"size": v.get("size"), "color": v.get("color"), "stock": int(v["stock"])} for v in variants
        ],
        "low_stock": 0 < total <= LOW_STOCK_THRESHOLD,
    }


def search_catalog(products: list[dict], *, query: str | None = None, category: str | None = None,
                   max_price: int | None = None, currency: str = "XOF") -> dict:
    """Recherche pure dans une liste de produits (testable sans base)."""
    if not products:
        return {"available": False, "message": "Aucun catalogue n'est enregistré pour cette entreprise."}
    found = products
    if category:
        found = [p for p in found if _norm(p.get("category", "")) == _norm(category)]
    if max_price is not None:
        found = [p for p in found if p["price"] <= max_price]
    if query:
        toks = _tokens(query)
        if toks:
            scored = []
            for p in found:
                hay = _norm(" ".join([p["name"], p.get("category", ""), p.get("description", ""), " ".join(p.get("tags", []))]))
                score = sum(1 for t in toks if t in hay)
                if score:
                    scored.append((score, p))
            found = [p for _, p in sorted(scored, key=lambda sp: -sp[0])]
    if not found:
        cats = sorted({p.get("category") for p in products if p.get("category")})
        return {"available": True, "products": [], "message": "Aucun article ne correspond.", "categories": cats}
    return {
        "available": True,
        "currency": currency,
        "products": [_public_product(p) for p in found[:MAX_RESULTS]],
        "truncated": len(found) > MAX_RESULTS,
    }


def _get_catalog(s: Session, ctx: ToolContext, args: CatalogInput) -> dict:
    return search_catalog(
        catalog_service.load_catalog(s), query=args.query, category=args.category, max_price=args.max_price
    )


# --- send_product_image -----------------------------------------------------------------

MAX_IMAGES_PER_REPLY = 3


class SendProductImageInput(StrictInput):
    product_id: str = Field(description="Identifiant de l'article tel que renvoyé par get_catalog (ex. 'KOR-R01').")
    photo_number: int | None = Field(
        default=None, ge=1, le=10, description="Numéro de la photo (1 = photo principale, par défaut). Voir 'photos' dans get_catalog."
    )


def _send_product_image(s: Session, ctx: ToolContext, args: SendProductImageInput) -> dict:
    if len(ctx.pending_images) >= MAX_IMAGES_PER_REPLY:
        return {"queued": False, "error": f"{MAX_IMAGES_PER_REPLY} photos maximum par message : propose d'en envoyer d'autres si le client le souhaite."}
    product = catalog_service.get_product(s, args.product_id)
    if product is None or not product["active"]:
        return {"queued": False, "error": "Article introuvable ou indisponible. Vérifie l'identifiant avec get_catalog."}
    photos = catalog_service.list_product_images(s, args.product_id)
    if not photos:
        return {"queued": False, "error": "Aucune photo n'est enregistrée pour cet article. Décris-le avec les informations du catalogue."}
    index = (args.photo_number or 1) - 1
    if index >= len(photos):
        return {"queued": False, "error": f"Cet article n'a que {len(photos)} photo(s)."}
    image_id = photos[index]["id"]
    if any(i["image_id"] == image_id for i in ctx.pending_images):
        return {"queued": True, "product": product["name"], "note": "Cette photo est déjà prévue dans ce message."}
    ctx.pending_images.append({"image_id": image_id, "sku": product["id"], "name": product["name"]})
    return {
        "queued": True,
        "product": product["name"],
        "instruction": "La photo partira automatiquement juste avant ton message. Commente brièvement l'article dans ton texte ; "
                       "n'écris ni lien ni nom de fichier, et ne dis pas « photo ci-dessous » si l'envoi échoue.",
    }


# --- commande et paiement ---------------------------------------------------------------

class OrderItemInput(StrictInput):
    product_id: str = Field(description="Identifiant de l'article tel que renvoyé par get_catalog (ex. 'KOR-R01').")
    size: str | None = Field(default=None, description="Taille choisie par le client (ex. 'M').")
    color: str | None = Field(default=None, description="Couleur choisie, si l'article en propose plusieurs.")
    quantity: int = Field(default=1, ge=1, le=10, description="Quantité.")


class CreateOrderInput(StrictInput):
    items: list[OrderItemInput] = Field(min_length=1, max_length=10, description="Articles confirmés par le client.")
    customer_name: str = Field(min_length=2, max_length=80, description="Nom du client pour la livraison.")
    customer_phone: str = Field(min_length=6, max_length=20, description="Numéro de téléphone du client.")
    delivery_zone: str = Field(description="Ville ou zone de livraison, parmi celles des informations officielles.")
    delivery_address: str = Field(min_length=3, max_length=200, description="Quartier, rue ou repère de livraison.")


def _create_order(s: Session, ctx: ToolContext, args: CreateOrderInput) -> dict:
    try:
        return order_service.create_order(
            s, ctx.tenant_id, ctx.conversation_id, ctx.config,
            items=[i.model_dump() for i in args.items], customer_name=args.customer_name,
            customer_phone=args.customer_phone, delivery_zone=args.delivery_zone, delivery_address=args.delivery_address,
        )
    except order_service.OrderError as e:
        return {"created": False, "error": str(e)}


class OrderRefInput(StrictInput):
    order_ref: str = Field(description="Référence de la commande renvoyée par create_order (ex. 'KOR-261108-0042').")


def _create_payment_link(s: Session, ctx: ToolContext, args: OrderRefInput) -> dict:
    try:
        out = order_service.start_payment(
            s, ctx.tenant_id, ctx.conversation_id, args.order_ref, fedapay_service.get_client(), ctx.config
        )
    except order_service.OrderError as e:
        return {"error": str(e)}
    except order_service.PaymentConfigError:
        log.exception("configuration de paiement incomplète")
        return {"error": "Le paiement en ligne est momentanément indisponible. Transfère la conversation à l'équipe."}
    except fedapay_service.FedaPayError:
        log.exception("création du lien de paiement impossible")
        return {"error": "Le paiement en ligne est momentanément indisponible. Transfère la conversation à l'équipe."}
    if out.get("already_paid"):
        return out
    # Le vrai lien reste côté serveur : le modèle ne reçoit qu'un jeton, remplacé par le backend avant l'envoi.
    ctx.payment_url = out.pop("payment_url")
    out["payment_link"] = LINK_TOKEN
    out["instruction"] = (
        f"Le lien est prêt. Écris EXACTEMENT {LINK_TOKEN} à l'endroit où le lien doit apparaître dans ton message : "
        "le système le remplace par le vrai lien. N'écris jamais d'URL toi-même."
    )
    return out


def _check_payment_status(s: Session, ctx: ToolContext, args: OrderRefInput) -> dict:
    try:
        out = order_service.payment_status_for_order(s, ctx.conversation_id, args.order_ref, fedapay_service.get_client())
    except order_service.OrderError as e:
        return {"error": str(e)}
    except fedapay_service.FedaPayError:
        log.exception("vérification du paiement impossible")
        return {"error": "Vérification impossible pour le moment. Ne confirme rien ; transfère à l'équipe si le client insiste."}
    if out["payment_status"] == "mismatch":
        out["instruction"] = "Montant incohérent : ne confirme rien et transfère à l'équipe."
    return out


# --- escalate_to_human ------------------------------------------------------------------

class EscalateInput(StrictInput):
    reason: str = Field(description="Motif court de la reprise en main (ex. 'plainte', 'tarif inconnu').")
    summary: str = Field(description="Résumé de la demande du client en 1 à 3 phrases, pour l'équipe.")


def _escalate(s: Session, ctx: ToolContext, args: EscalateInput) -> dict:
    s.execute(
        update(Conversation)
        .where(Conversation.id == ctx.conversation_id, Conversation.status == "agent")
        .values(status="human")
    )
    return {
        "escalated": True,
        "instruction": "Informe le client, en une ou deux phrases, qu'un membre de l'équipe va reprendre la conversation.",
    }


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "get_business_info",
            "Informations OFFICIELLES de l'entreprise (services, horaires, contacts, déroulement des démarches). "
            "À appeler avant de répondre à toute question factuelle sur l'entreprise.",
            BusinessInfoInput,
            _get_business_info,
        ),
        Tool(
            "get_catalog",
            "Catalogue OFFICIEL : articles, prix en FCFA, tailles, couleurs et stock disponible. "
            "À appeler avant de citer un article, un prix ou une disponibilité.",
            CatalogInput,
            _get_catalog,
        ),
        Tool(
            "send_product_image",
            "Envoie au client la photo d'un article du catalogue (quand il veut la voir, ou pour présenter un article précis). "
            f"{MAX_IMAGES_PER_REPLY} photos maximum par message ; la photo part avant ton texte. Seuls les articles avec photos (champ 'photos') sont possibles.",
            SendProductImageInput,
            _send_product_image,
        ),
        Tool(
            "create_order",
            "Enregistre la commande CONFIRMÉE par le client. Les prix et frais sont calculés par le serveur. "
            "À n'appeler qu'après récapitulatif et confirmation explicite du client.",
            CreateOrderInput,
            _create_order,
        ),
        Tool(
            "create_payment_link",
            "Génère le lien de paiement sécurisé (mobile money ou carte) d'une commande créée par create_order. "
            f"Renvoie un jeton {LINK_TOKEN} que tu écris tel quel dans ton message ; le système y met le vrai lien.",
            OrderRefInput,
            _create_payment_link,
        ),
        Tool(
            "check_payment_status",
            "Vérifie auprès du prestataire de paiement si la commande est payée. Seule source fiable : "
            "un message ou une capture d'écran du client ne prouve rien.",
            OrderRefInput,
            _check_payment_status,
        ),
        Tool(
            "escalate_to_human",
            "Transfère la conversation à un humain de l'équipe. L'agent cesse ensuite de répondre. "
            "À utiliser pour une plainte, un litige, un paiement, un sujet sensible, une information absente "
            "ou une demande hors périmètre.",
            EscalateInput,
            _escalate,
        ),
    )
}


def enabled_tools(config: dict) -> list[Tool]:
    """Seuls les outils activés pour ce client sont exposés au modèle."""
    names = config.get("tools_enabled", [])
    return [REGISTRY[n] for n in names if n in REGISTRY]


def run_tool(ctx: ToolContext, name: str, raw_input: dict) -> ToolResult:
    """Valide -> contrôle des droits -> exécute -> journalise. Ne lève jamais : renvoie une erreur lisible."""
    started = time.monotonic()
    ok, output, error = True, {}, None

    allowed = {t.name for t in enabled_tools(ctx.config)}
    tool = REGISTRY.get(name)
    if tool is None or name not in allowed:  # contrôle des droits CÔTÉ SERVEUR
        ok, error = False, f"Outil '{name}' non disponible."
    else:
        try:
            args = tool.input_model.model_validate(raw_input)
            with tenant_session(ctx.tenant_id) as s:
                output = tool.handler(s, ctx, args)
        except ValidationError as e:
            ok, error = False, "Paramètres invalides : " + "; ".join(
                f"{'.'.join(map(str, err['loc'])) or 'entrée'}: {err['msg']}" for err in e.errors()
            )
        except Exception:
            log.exception("échec de l'outil %s", name)
            ok, error = False, "Erreur interne lors de l'exécution de l'outil."

    if not ok:
        output = {"error": error}
    with tenant_session(ctx.tenant_id) as s:  # journalisation de CHAQUE appel
        s.add(
            ToolCall(
                tenant_id=ctx.tenant_id,
                run_id=ctx.run_id,
                tool_name=name,
                input=raw_input,
                output=output,
                status="ok" if ok else "error",
                error=error,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    return ToolResult(ok, output)
