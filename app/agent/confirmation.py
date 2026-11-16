"""Message de confirmation de paiement, écrit par le BACKEND (pas par le modèle).

Dès que FedaPay confirme un paiement, le client reçoit ce message tout de suite, sans qu'il ait à le signaler et sans
appel au modèle : même texte à chaque fois, montants exacts lus en base, aucune invention possible.
"""
from app.agent.text import fold
from app.services.orders import split_name


def fcfa(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " FCFA"


def _line(it: dict) -> str:
    options = ", ".join(str(x) for x in (f"taille {it['size']}" if it.get("size") else None, it.get("color")) if x)
    label = f"{it['name']} ({options})" if options else it["name"]
    return f"- {it['quantity']} × {label} — {fcfa(it['line_total'])}"


def delivery_delay(config: dict, zone: str) -> str | None:
    """Délai officiel de la zone, lu dans les informations du client (jamais deviné)."""
    zones = (((config or {}).get("business_info") or {}).get("livraison") or {}).get("zones") or {}
    for name, info in zones.items():
        if fold(name) == fold(zone):
            return (info or {}).get("delai") or None
    return None


def payment_confirmation_text(order: dict, config: dict, tenant_name: str = "") -> str:
    """`order` : ligne de la table orders (reference, customer_name, items, delivery_*, total...)."""
    first, _ = split_name(order["customer_name"])
    lines = [_line(it) for it in order["items"]]
    if order["delivery_fee"]:
        lines.append(f"- Frais de livraison ({order['delivery_zone']}) : {fcfa(order['delivery_fee'])}")
    parts = [
        f"Merci {first}, votre paiement est confirmé ✅",
        f"Commande {order['reference']} :\n" + "\n".join(lines) + f"\nTotal payé : {fcfa(order['total'])}",
        f"Livraison : {order['delivery_address']}, {order['delivery_zone']}.",
    ]
    delay = delivery_delay(config, order["delivery_zone"])
    if delay:
        parts[-1] += f" Délai estimé : {delay}."
    parts.append(f"À bientôt chez {tenant_name} !" if tenant_name else "Merci pour votre confiance !")
    return "\n\n".join(parts)
