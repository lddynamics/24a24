"""Catalogue d'un client : lecture par l'agent, gestion par la console, stock décrémenté au paiement.

Source de vérité = table `products` (isolée par RLS). `tenants.config.catalog` ne sert plus que de modèle d'import.
"""
import json
import re
import secrets
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.text import fold
from app.services import images as image_service

MAX_PRICE = 10_000_000
MAX_STOCK = 100_000
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,39}$")
MAX_PRODUCT_IMAGES = 6
_COLUMNS = (
    "sku, name, category, description, price, tags, variants, active, "
    "(SELECT count(*) FROM product_images i WHERE i.product_id = products.id) AS image_count, "
    "(SELECT i.id FROM product_images i WHERE i.product_id = products.id ORDER BY i.position, i.created_at, i.id LIMIT 1) AS cover_image_id"
)


class ProductError(Exception):
    pass


def _to_dict(r) -> dict:
    return {
        "id": r.sku, "name": r.name, "category": r.category or "", "description": r.description or "",
        "price": r.price, "tags": list(r.tags or []), "variants": list(r.variants or []), "active": r.active,
        "image_count": int(r.image_count or 0), "cover_image_id": str(r.cover_image_id) if r.cover_image_id else None,
    }


def list_products(s: Session, *, only_active: bool = False) -> list[dict]:
    where = " WHERE active" if only_active else ""
    rows = s.execute(text(f"SELECT {_COLUMNS} FROM products{where} ORDER BY position, name")).all()
    return [_to_dict(r) for r in rows]


def load_catalog(s: Session) -> list[dict]:
    """Produits visibles par l'agent (actifs)."""
    return list_products(s, only_active=True)


def get_product(s: Session, sku: str) -> dict | None:
    r = s.execute(text(f"SELECT {_COLUMNS} FROM products WHERE sku = :sku"), {"sku": sku}).first()
    return _to_dict(r) if r else None


def stock_total(product: dict) -> int:
    return sum(int(v.get("stock", 0)) for v in product.get("variants", []))


def new_sku() -> str:
    return "P-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


def create_product(s: Session, tenant_id: UUID, data: dict) -> str:
    sku = data.get("sku") or new_sku()
    if s.execute(text("SELECT 1 FROM products WHERE sku = :sku"), {"sku": sku}).first():
        raise ProductError(f"La référence « {sku} » existe déjà.")
    pos = s.execute(text("SELECT COALESCE(MAX(position), -1) + 1 FROM products")).scalar_one()
    s.execute(
        text("""INSERT INTO products (tenant_id, sku, name, category, description, price, tags, variants, position)
                VALUES (:t, :sku, :name, :cat, :desc, :price, CAST(:tags AS jsonb), CAST(:variants AS jsonb), :pos)"""),
        {"t": tenant_id, "sku": sku, "name": data["name"], "cat": data["category"], "desc": data["description"],
         "price": data["price"], "tags": json.dumps(data["tags"], ensure_ascii=False),
         "variants": json.dumps(data["variants"], ensure_ascii=False), "pos": pos},
    )
    return sku


def update_product(s: Session, sku: str, data: dict) -> bool:
    res = s.execute(
        text("""UPDATE products SET name = :name, category = :cat, description = :desc, price = :price,
                  tags = CAST(:tags AS jsonb), variants = CAST(:variants AS jsonb), updated_at = now()
                WHERE sku = :sku"""),
        {"sku": sku, "name": data["name"], "cat": data["category"], "desc": data["description"],
         "price": data["price"], "tags": json.dumps(data["tags"], ensure_ascii=False),
         "variants": json.dumps(data["variants"], ensure_ascii=False)},
    )
    return res.rowcount > 0


def delete_product(s: Session, sku: str) -> bool:
    """Suppression définitive. Les commandes déjà passées gardent leurs lignes figées (nom, prix, taille)."""
    return s.execute(text("DELETE FROM products WHERE sku = :sku"), {"sku": sku}).rowcount > 0


def set_active(s: Session, sku: str, active: bool) -> bool:
    return s.execute(
        text("UPDATE products SET active = :a, updated_at = now() WHERE sku = :sku"), {"a": active, "sku": sku}
    ).rowcount > 0


# --- photos des produits -------------------------------------------------------------------------------

def list_product_images(s: Session, sku: str) -> list[dict]:
    """Photos d'un produit, dans l'ordre d'affichage (la première est la photo principale)."""
    rows = s.execute(
        text("""SELECT i.id, i.size FROM product_images i JOIN products p ON p.id = i.product_id
                WHERE p.sku = :sku ORDER BY i.position, i.created_at, i.id"""), {"sku": sku},
    ).all()
    return [{"id": str(r.id), "size": r.size} for r in rows]


def add_product_image(s: Session, tenant_id: UUID, sku: str, data: bytes) -> str:
    """Ajoute une photo (validée par son contenu, ré-encodée en JPEG). Lève ProductError avec un message lisible."""
    row = s.execute(text("SELECT id FROM products WHERE sku = :sku FOR UPDATE"), {"sku": sku}).first()
    if row is None:
        raise ProductError("Produit introuvable.")
    count = s.execute(text("SELECT count(*) FROM product_images WHERE product_id = :p"), {"p": row.id}).scalar_one()
    if count >= MAX_PRODUCT_IMAGES:
        raise ProductError(f"{MAX_PRODUCT_IMAGES} photos maximum par produit : supprimez-en une d'abord.")
    try:
        jpeg, mime = image_service.normalize_image(data)
    except image_service.ImageError as e:
        raise ProductError(str(e)) from e
    pos = s.execute(text("SELECT COALESCE(MAX(position), -1) + 1 FROM product_images WHERE product_id = :p"), {"p": row.id}).scalar_one()
    return str(s.execute(
        text("""INSERT INTO product_images (tenant_id, product_id, content_type, size, data, position)
                VALUES (:t, :p, :ct, :n, :d, :pos) RETURNING id"""),
        {"t": tenant_id, "p": row.id, "ct": mime, "n": len(jpeg), "d": jpeg, "pos": pos},
    ).scalar_one())


def delete_product_image(s: Session, sku: str, image_id: UUID) -> bool:
    return s.execute(
        text("DELETE FROM product_images WHERE id = :i AND product_id = (SELECT id FROM products WHERE sku = :sku)"),
        {"i": image_id, "sku": sku},
    ).rowcount > 0


def set_cover_image(s: Session, sku: str, image_id: UUID) -> bool:
    """Place la photo en première position : c'est celle que l'agent envoie par défaut."""
    return s.execute(
        text("""UPDATE product_images SET position = (SELECT COALESCE(MIN(position), 0) - 1 FROM product_images
                                                      WHERE product_id = product_images.product_id)
                WHERE id = :i AND product_id = (SELECT id FROM products WHERE sku = :sku)"""),
        {"i": image_id, "sku": sku},
    ).rowcount > 0


def get_product_image(s: Session, image_id: UUID) -> tuple[bytes, str] | None:
    r = s.execute(text("SELECT data, content_type FROM product_images WHERE id = :i"), {"i": image_id}).first()
    return (bytes(r.data), r.content_type) if r else None


def import_from_config(s: Session, tenant_id: UUID, config: dict) -> int:
    """Copie le catalogue du JSON du client (modèle d'exemple) ; n'écrase rien. Retourne le nombre ajouté."""
    added = 0
    for p in (config.get("catalog") or {}).get("products", []):
        if s.execute(text("SELECT 1 FROM products WHERE sku = :sku"), {"sku": p["id"]}).first():
            continue
        create_product(s, tenant_id, {
            "sku": p["id"], "name": p["name"], "category": p.get("category", ""), "description": p.get("description", ""),
            "price": int(p["price"]), "tags": list(p.get("tags", [])), "variants": list(p.get("variants", [])),
        })
        added += 1
    return added


def decrement_stock(s: Session, lines: list[dict]) -> None:
    """Retire du stock les pièces d'une commande payée (jamais sous zéro). Verrouille la ligne produit."""
    for line in lines:
        row = s.execute(
            text("SELECT id, variants FROM products WHERE sku = :sku FOR UPDATE"), {"sku": line.get("product_id")}
        ).first()
        if row is None:
            continue
        variants = list(row.variants or [])
        for v in variants:
            if str(v.get("size")) == str(line.get("size")) and str(v.get("color")) == str(line.get("color")):
                v["stock"] = max(0, int(v.get("stock", 0)) - int(line["quantity"]))
        s.execute(
            text("UPDATE products SET variants = CAST(:v AS jsonb), updated_at = now() WHERE id = :id"),
            {"v": json.dumps(variants, ensure_ascii=False), "id": row.id},
        )


def _int(value: str) -> int | None:
    digits = re.sub(r"[\s\u202f\u00a0]", "", value or "")
    return int(digits) if re.fullmatch(r"\d{1,12}", digits) else None


def parse_product_form(fields: dict[str, list[str]], *, creating: bool) -> tuple[dict, list[str]]:
    """Valide le formulaire produit. Retourne (données nettoyées, erreurs en français)."""
    def one(key: str) -> str:
        return (fields.get(key) or [""])[0].strip()

    errors: list[str] = []
    name = one("name")
    if not 2 <= len(name) <= 120:
        errors.append("Le nom doit comporter entre 2 et 120 caractères.")
    category, description = one("category"), one("description")
    if len(category) > 60:
        errors.append("La catégorie est trop longue (60 caractères maximum).")
    if len(description) > 1000:
        errors.append("La description est trop longue (1 000 caractères maximum).")
    price = _int(one("price"))
    if price is None or not 1 <= price <= MAX_PRICE:
        errors.append(f"Le prix doit être un nombre entier de FCFA entre 1 et {MAX_PRICE:,}.".replace(",", " "))
    tags = [t.strip() for t in one("tags").split(",") if t.strip()]
    if len(tags) > 12 or any(len(t) > 30 for t in tags):
        errors.append("12 mots-clés maximum, 30 caractères chacun.")

    variants, seen = [], set()
    sizes, colors, stocks = fields.get("variant_size", []), fields.get("variant_color", []), fields.get("variant_stock", [])
    for i in range(max(len(sizes), len(colors), len(stocks))):
        size = (sizes[i] if i < len(sizes) else "").strip()
        color = (colors[i] if i < len(colors) else "").strip()
        raw_stock = (stocks[i] if i < len(stocks) else "").strip()
        if not (size or color or raw_stock):
            continue                                   # ligne vide ignorée
        stock = _int(raw_stock or "0")
        if stock is None or stock > MAX_STOCK:
            errors.append(f"Variante {i + 1} : le stock doit être un entier entre 0 et {MAX_STOCK}.")
            continue
        if len(size) > 30 or len(color) > 30:
            errors.append(f"Variante {i + 1} : taille et couleur limitées à 30 caractères.")
            continue
        key = (fold(size), fold(color))
        if key in seen:
            errors.append(f"Variante en double : {size or '—'} / {color or '—'}.")
            continue
        seen.add(key)
        variants.append({"size": size or "Unique", "color": color or "Unique", "stock": stock})
    if not variants:
        errors.append("Ajoutez au moins une variante (taille, couleur et stock).")

    sku = ""
    if creating:
        sku = one("sku")
        if sku and not SKU_RE.match(sku):
            errors.append("La référence ne peut contenir que lettres, chiffres, « - » et « _ » (2 à 40 caractères).")
    data = {"sku": sku, "name": name, "category": category, "description": description,
            "price": price or 0, "tags": tags, "variants": variants}
    return data, errors
