"""Commandes et paiements : toute la logique métier qui touche à l'argent vit ici, jamais dans le modèle.

Principes (document de conception, chapitre 07) :
- les prix, frais et totaux sont RECALCULÉS par le serveur depuis le catalogue ; le modèle ne fournit que des choix ;
- le lien de paiement est créé par le backend ; le modèle le transmet sans le modifier ;
- un paiement n'est « réussi » qu'après revérification auprès de l'API FedaPay.
"""
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.text import fold
from app.config import get_settings
from app.db import routing_session, tenant_session
from app.services import catalog as catalog_service
from app.services import fedapay as fp

log = logging.getLogger(__name__)

LINK_REUSE_MINUTES = 30
PAYMENT_SUCCEEDED = "payment_succeeded"   # marque (messages.raw.kind) la notification d'un paiement confirmé


class OrderError(Exception):
    """Erreur métier lisible par le modèle (il la reformule pour le client)."""


class PaymentConfigError(Exception):
    """Configuration de paiement incomplète : problème d'administration, pas à expliquer au client."""


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    return (parts[0], " ".join(parts[1:]) or "Client") if parts else ("Client", "Client")


def invoice_email(config: dict | None) -> str:
    """E-mail de l'entreprise rattaché au client FedaPay (là où part la facture) : JSON du client, sinon .env."""
    email = ((config or {}).get("payment") or {}).get("invoice_email") or get_settings().fedapay_customer_email
    if not email:
        raise PaymentConfigError("e-mail de facturation manquant (FEDAPAY_CUSTOMER_EMAIL ou payment.invoice_email)")
    return email


def _find_product(products: list[dict], product_id: str) -> dict | None:
    for p in products:
        if fold(p["id"]) == fold(product_id):
            return p
    return None


def _delivery_fee(config: dict, zone: str) -> tuple[str, int]:
    zones = ((config.get("business_info") or {}).get("livraison") or {}).get("zones") or {}
    for name, info in zones.items():
        if fold(name) == fold(zone):
            return name, int(info["frais_fcfa"])
    raise OrderError(f"Zone de livraison non desservie ou inconnue. Zones disponibles : {sorted(zones)}. "
                     "Si le client est ailleurs, transfère à l'équipe.")


def build_order(config: dict, items: list[dict], zone: str, products: list[dict] | None = None) -> dict:
    """Valide les choix du client contre le catalogue et calcule les montants (pur, sans base de données).

    `products` : catalogue de la base (production) ; à défaut, celui de la configuration (tests, modèle d'exemple).
    """
    if products is None:
        products = (config.get("catalog") or {}).get("products", [])
    lines, subtotal = [], 0
    for it in items:
        product = _find_product(products, it["product_id"])
        if product is None:
            raise OrderError(f"Article inconnu : {it['product_id']}. Utilise get_catalog pour obtenir les identifiants.")
        variants = [v for v in product.get("variants", []) if int(v.get("stock", 0)) > 0]
        if it.get("size"):
            variants = [v for v in variants if fold(str(v.get("size"))) == fold(it["size"])]
        if it.get("color"):
            variants = [v for v in variants if fold(str(v.get("color"))) == fold(it["color"])]
        if not variants:
            raise OrderError(f"{product['name']} : combinaison taille/couleur indisponible. Propose une alternative.")
        if len(variants) > 1:
            options = [f"{v.get('size')} / {v.get('color')}" for v in variants]
            raise OrderError(f"{product['name']} : précise la taille et/ou la couleur. Options : {options}")
        v = variants[0]
        if it["quantity"] > int(v["stock"]):
            raise OrderError(f"{product['name']} ({v.get('size')}, {v.get('color')}) : stock insuffisant "
                             f"pour {it['quantity']} pièce(s) ; maximum {v['stock']}.")
        line_total = int(product["price"]) * it["quantity"]
        subtotal += line_total
        lines.append({
            "product_id": product["id"], "name": product["name"], "size": v.get("size"), "color": v.get("color"),
            "quantity": it["quantity"], "unit_price": int(product["price"]), "line_total": line_total,
        })
    zone_name, fee = _delivery_fee(config, zone)
    return {"lines": lines, "subtotal": subtotal, "delivery_fee": fee, "total": subtotal + fee, "zone": zone_name}


def _new_reference(config: dict) -> str:
    prefix = (config.get("order_prefix") or "CMD").upper()
    return f"{prefix}-{datetime.now(UTC):%y%m%d}-{secrets.randbelow(10000):04d}"


def create_order(s: Session, tenant_id: UUID, conversation_id: UUID, config: dict, *, items: list[dict],
                 customer_name: str, customer_phone: str, delivery_zone: str, delivery_address: str) -> dict:
    built = build_order(config, items, delivery_zone, products=catalog_service.load_catalog(s))
    for _ in range(5):  # collision de référence très improbable ; on réessaie
        ref = _new_reference(config)
        if not s.execute(text("SELECT 1 FROM orders WHERE reference = :r"), {"r": ref}).first():
            break
    s.execute(
        text("""INSERT INTO orders (tenant_id, conversation_id, reference, customer_name, customer_phone,
                  delivery_zone, delivery_address, items, subtotal, delivery_fee, total)
                VALUES (:t, :c, :ref, :n, :p, :z, :a, CAST(:items AS jsonb), :sub, :fee, :total)"""),
        {"t": tenant_id, "c": conversation_id, "ref": ref, "n": customer_name.strip(), "p": customer_phone.strip(),
         "z": built["zone"], "a": delivery_address.strip(), "items": json.dumps(built["lines"], ensure_ascii=False),
         "sub": built["subtotal"], "fee": built["delivery_fee"], "total": built["total"]},
    )
    return {"order_ref": ref, "items": built["lines"], "subtotal_fcfa": built["subtotal"],
            "delivery_fee_fcfa": built["delivery_fee"], "total_fcfa": built["total"], "delivery_zone": built["zone"]}


def _order(s: Session, conversation_id: UUID, ref: str):
    # Un client ne peut agir que sur les commandes de SA conversation (en plus de l'isolation par tenant).
    row = s.execute(
        text("SELECT id, reference, status, total, currency FROM orders WHERE reference = :r AND conversation_id = :c"),
        {"r": ref.strip().upper(), "c": conversation_id},
    ).first()
    if row is None:
        raise OrderError("Commande introuvable pour cette conversation.")
    return row


def start_payment(s: Session, tenant_id: UUID, conversation_id: UUID, ref: str, client: fp.FedaPayClient,
                  config: dict | None = None) -> dict:
    order = _order(s, conversation_id, ref)
    if order.status in ("paid", "delivered"):
        return {"already_paid": True, "order_ref": order.reference}
    if order.status == "cancelled":
        raise OrderError("Cette commande a été annulée par l'équipe : transfère à l'équipe si le client la réclame.")
    pending = s.execute(
        text("""SELECT payment_url FROM payments WHERE order_id = :o AND status = 'pending'
                  AND created_at > now() - make_interval(mins => :m) ORDER BY created_at DESC LIMIT 1"""),
        {"o": order.id, "m": LINK_REUSE_MINUTES},
    ).first()
    if pending and pending.payment_url:  # évite de créer plusieurs transactions pour la même commande
        return {"order_ref": order.reference, "payment_url": pending.payment_url, "amount_fcfa": order.total, "reused": True}

    payment_id = uuid4()
    callback = get_settings().fedapay_callback_url or None
    email = invoice_email(config)       # avant tout appel à FedaPay : pas de transaction orpheline si c'est absent
    customer_name = s.execute(text("SELECT customer_name FROM orders WHERE id = :o"), {"o": order.id}).scalar_one()
    first, last = split_name(customer_name)
    # FedaPay rattache un « client » à la transaction (la page de paiement en a besoin). Même e-mail pour tous :
    # FedaPay met alors simplement à jour ce client (comportement documenté).
    tx = client.create_transaction(
        amount=order.total, currency=order.currency, description=f"Commande {order.reference}",
        callback_url=callback, metadata={"order_reference": order.reference, "payment_id": str(payment_id)},
        customer={"firstname": first, "lastname": last, "email": email},
    )
    _token, url = client.generate_token(tx.id)
    s.execute(
        text("""INSERT INTO payments (id, tenant_id, order_id, provider_transaction_id, payment_url, amount, currency)
                VALUES (:id, :t, :o, :tx, :url, :amt, :cur)"""),
        {"id": payment_id, "t": tenant_id, "o": order.id, "tx": tx.id, "url": url, "amt": order.total, "cur": order.currency},
    )
    s.execute(text("UPDATE orders SET status = 'awaiting_payment', updated_at = now() WHERE id = :o"), {"o": order.id})
    return {"order_ref": order.reference, "payment_url": url, "amount_fcfa": order.total}


@dataclass
class Refresh:
    payment_status: str       # pending | succeeded | failed | mismatch
    transitioned: bool        # vrai si CET appel a fait passer le paiement à un état final


def refresh_payment(s: Session, payment_id: UUID, client: fp.FedaPayClient) -> Refresh:
    """Revérifie un paiement auprès de FedaPay et met la base à jour. Source de vérité : l'API."""
    pay = s.execute(
        text("SELECT id, order_id, provider_transaction_id, status, amount FROM payments WHERE id = :p"), {"p": payment_id}
    ).one()
    if pay.status != "pending":
        return Refresh(pay.status, False)
    tx = client.get_transaction(pay.provider_transaction_id)
    new = fp.map_status(tx.status)
    if new == "succeeded" and tx.amount is not None and tx.amount != pay.amount:
        log.error("paiement %s : montant FedaPay %s != attendu %s", payment_id, tx.amount, pay.amount)
        return Refresh("mismatch", False)
    if new == "pending":
        return Refresh("pending", False)
    updated = s.execute(
        text("""UPDATE payments SET status = :st, updated_at = now(),
                  paid_at = CASE WHEN :st = 'succeeded' THEN now() END
                WHERE id = :p AND status = 'pending' RETURNING id"""),
        {"st": new, "p": payment_id},
    ).first()
    if updated is None:  # une exécution concurrente a déjà conclu
        return Refresh(new, False)
    s.execute(
        text("UPDATE orders SET status = :st, updated_at = now() WHERE id = :o"),
        {"st": "paid" if new == "succeeded" else "failed", "o": pay.order_id},
    )
    if new == "succeeded":   # une seule fois (transition atomique ci-dessus) : le stock suit les ventes payées
        items = s.execute(text("SELECT items FROM orders WHERE id = :o"), {"o": pay.order_id}).scalar_one()
        catalog_service.decrement_stock(s, items)
    return Refresh(new, True)


def payment_status_for_order(s: Session, conversation_id: UUID, ref: str, client: fp.FedaPayClient) -> dict:
    order = _order(s, conversation_id, ref)
    pay = s.execute(
        text("SELECT id FROM payments WHERE order_id = :o ORDER BY created_at DESC LIMIT 1"), {"o": order.id}
    ).first()
    if pay is None:
        return {"order_ref": order.reference, "order_status": order.status, "payment_status": "no_payment", "paid": False}
    result = refresh_payment(s, pay.id, client)
    order_status = s.execute(text("SELECT status FROM orders WHERE id = :o"), {"o": order.id}).scalar_one()
    return {"order_ref": order.reference, "order_status": order_status, "payment_status": result.payment_status,
            "paid": result.payment_status == "succeeded"}


def handle_fedapay_event(payload: dict, client: fp.FedaPayClient) -> str:
    """Traite un webhook FedaPay déjà authentifié. Retourne un statut lisible pour les logs."""
    name = str(payload.get("name") or payload.get("type") or "")
    if not name.startswith("transaction."):
        return "ignored:event"
    tx_id = fp.extract_transaction_id(payload)
    if tx_id is None:
        log.warning("webhook FedaPay sans identifiant de transaction (événement %s)", name)
        return "ignored:no_transaction_id"
    with routing_session() as s:
        route = s.execute(
            text("SELECT tenant_id, payment_id FROM payment_routes WHERE provider = 'fedapay' AND provider_transaction_id = :t"),
            {"t": tx_id},
        ).first()
    if route is None:
        return "ignored:unknown_transaction"

    with tenant_session(route.tenant_id) as s:
        result = refresh_payment(s, route.payment_id, client)   # revérification auprès de l'API
        if result.transitioned and result.payment_status == "succeeded":
            _notify_agent(s, route.tenant_id, route.payment_id)
    return f"processed:{result.payment_status}"


def _notify_agent(s: Session, tenant_id: UUID, payment_id: UUID) -> None:
    """Paiement confirmé : crée un message système + un job. Le worker envoie alors LUI-MÊME la confirmation au client
    (texte du backend, sans modèle) ; le client n'a rien à signaler. Envoyé même si un humain a la main : c'est
    une information transactionnelle."""
    row = s.execute(
        text("""SELECT o.reference, o.conversation_id, c.channel_id
                FROM payments p JOIN orders o ON o.id = p.order_id AND o.tenant_id = p.tenant_id
                JOIN conversations c ON c.id = o.conversation_id AND c.tenant_id = o.tenant_id
                WHERE p.id = :p"""),
        {"p": payment_id},
    ).one()
    body = (f"[Notification système] Paiement de la commande {row.reference} confirmé par FedaPay. "
            "La confirmation a été envoyée automatiquement au client.")
    msg_id = s.execute(
        text("""INSERT INTO messages (tenant_id, conversation_id, channel_id, direction, type, text, raw,
                                      idempotency_key, received_at)
                VALUES (:t, :c, :ch, 'in', 'other', :body, CAST(:raw AS jsonb), :key, now())
                ON CONFLICT (tenant_id, channel_id, idempotency_key) DO NOTHING RETURNING id"""),
        {"t": tenant_id, "c": row.conversation_id, "ch": row.channel_id, "body": body,
         "raw": json.dumps({"system": True, "kind": PAYMENT_SUCCEEDED, "payment_id": str(payment_id)}),
         "key": f"fedapay:{payment_id}:succeeded"},
    ).scalar()
    if msg_id is not None:
        s.execute(
            text("INSERT INTO jobs (tenant_id, conversation_id, message_id) VALUES (:t, :c, :m)"),
            {"t": tenant_id, "c": row.conversation_id, "m": msg_id},
        )


def reconcile_pending_payments(client: fp.FedaPayClient, *, min_age_seconds: int = 15, window_minutes: int = 360,
                               limit: int = 50) -> int:
    """Filet de sécurité sans webhook : revérifie auprès de FedaPay les paiements encore « pending » récents.

    Si le webhook n'arrive pas (tunnel coupé, mauvaise URL, secret erroné), le client est quand même prévenu.
    Retourne le nombre de paiements dont l'état a changé. Une erreur FedaPay sur un paiement n'arrête pas les autres.
    """
    with routing_session() as s:
        routes = s.execute(
            text("""SELECT tenant_id, payment_id FROM payment_routes
                    WHERE provider = 'fedapay' AND status = 'pending'
                      AND created_at > now() - make_interval(mins => :w)
                      AND created_at < now() - make_interval(secs => :a)
                    ORDER BY created_at LIMIT :n"""),
            {"w": window_minutes, "a": min_age_seconds, "n": limit},
        ).all()
    changed = 0
    for route in routes:
        try:
            with tenant_session(route.tenant_id) as s:
                result = refresh_payment(s, route.payment_id, client)
                if result.transitioned and result.payment_status == "succeeded":
                    _notify_agent(s, route.tenant_id, route.payment_id)
        except fp.FedaPayError as e:
            log.warning("réconciliation du paiement %s impossible : %s", route.payment_id, e)
            continue
        changed += int(result.transitioned)
    return changed
