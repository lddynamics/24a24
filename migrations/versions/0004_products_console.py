"""Catalogue en base (gérable depuis la console) + statut de commande « livrée ».

Le catalogue d'un client vivait dans tenants.config (JSON). Il devient une table `products`,
source de vérité unique : la console le modifie, l'agent le lit, le paiement décrémente le stock.
Les catalogues déjà présents dans la configuration sont recopiés ici (une seule fois).

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE products (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      sku         text NOT NULL,
      name        text NOT NULL,
      category    text,
      description text,
      price       integer NOT NULL CHECK (price >= 0),
      tags        jsonb NOT NULL DEFAULT '[]',
      variants    jsonb NOT NULL DEFAULT '[]',      -- [{"size": "M", "color": "Indigo", "stock": 4}]
      active      boolean NOT NULL DEFAULT true,
      position    integer NOT NULL DEFAULT 0,
      created_at  timestamptz NOT NULL DEFAULT now(),
      updated_at  timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, sku)
    );
    CREATE INDEX products_tenant_position ON products (tenant_id, position, name);

    -- Recopie des catalogues déjà présents dans la configuration (avant l'activation de la RLS sur la table).
    -- Si le rôle de migration ne peut pas lire `tenants` (RLS), rien n'est copié : utiliser « Importer » dans la console.
    INSERT INTO products (tenant_id, sku, name, category, description, price, tags, variants, position)
    SELECT t.id, e.p->>'id', e.p->>'name', e.p->>'category', e.p->>'description', (e.p->>'price')::int,
           COALESCE(e.p->'tags', '[]'::jsonb), COALESCE(e.p->'variants', '[]'::jsonb), (e.ord - 1)::int
    FROM tenants t,
         jsonb_array_elements(t.config->'catalog'->'products') WITH ORDINALITY AS e(p, ord)
    WHERE jsonb_typeof(t.config->'catalog'->'products') = 'array'
    ON CONFLICT (tenant_id, sku) DO NOTHING;
    """)
    op.execute(f"""
    ALTER TABLE products ENABLE ROW LEVEL SECURITY;
    ALTER TABLE products FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON products USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
    GRANT SELECT, INSERT, UPDATE, DELETE ON products TO {APP_ROLE};

    ALTER TABLE orders DROP CONSTRAINT orders_status_check;
    ALTER TABLE orders ADD CONSTRAINT orders_status_check
      CHECK (status IN ('draft', 'awaiting_payment', 'paid', 'delivered', 'failed', 'cancelled'));
    """)


def downgrade() -> None:
    op.execute("""
    UPDATE orders SET status = 'paid' WHERE status = 'delivered';
    ALTER TABLE orders DROP CONSTRAINT orders_status_check;
    ALTER TABLE orders ADD CONSTRAINT orders_status_check
      CHECK (status IN ('draft', 'awaiting_payment', 'paid', 'failed', 'cancelled'));
    DROP TABLE IF EXISTS products CASCADE;
    """)
