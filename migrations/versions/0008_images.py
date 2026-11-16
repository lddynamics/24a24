"""Images : photos des produits (catalogue) et description automatique des images reçues.

- `product_images` : plusieurs photos par produit, stockées en base (JPEG normalisé, comme les médias de messages).
  Isolation par RLS ; supprimées avec le produit.
- `message_media.description` : ce que le modèle de vision a compris d'une image reçue. Calculée une seule fois,
  puis rejouée dans l'historique de l'agent (pas de nouvel appel au modèle de vision à chaque tour).

Stockage `bytea` : suffisant pour le prototype ; à remplacer par un stockage d'objets avant la production.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE product_images (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      product_id   uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      content_type text NOT NULL,
      size         integer NOT NULL CHECK (size > 0),
      data         bytea NOT NULL,
      position     integer NOT NULL DEFAULT 0,
      created_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX product_images_product ON product_images (product_id, position, created_at);
    ALTER TABLE product_images ENABLE ROW LEVEL SECURITY;
    ALTER TABLE product_images FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON product_images USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
    GRANT SELECT, INSERT, UPDATE, DELETE ON product_images TO {APP_ROLE};

    ALTER TABLE message_media ADD COLUMN description text;
    GRANT UPDATE (description) ON message_media TO {APP_ROLE};
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE message_media DROP COLUMN IF EXISTS description;
    DROP TABLE IF EXISTS product_images CASCADE;
    """)
