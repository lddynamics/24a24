"""Commandes et paiements (FedaPay) + table de routage des webhooks de paiement.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE orders (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      conversation_id  uuid NOT NULL,
      reference        text NOT NULL,
      status           text NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'awaiting_payment', 'paid', 'failed', 'cancelled')),
      customer_name    text NOT NULL,
      customer_phone   text NOT NULL,
      delivery_zone    text NOT NULL,
      delivery_address text NOT NULL,
      items            jsonb NOT NULL,          -- lignes figées : prix recalculés par le serveur
      subtotal         integer NOT NULL CHECK (subtotal >= 0),
      delivery_fee     integer NOT NULL CHECK (delivery_fee >= 0),
      total            integer NOT NULL CHECK (total > 0),
      currency         text NOT NULL DEFAULT 'XOF',
      created_at       timestamptz NOT NULL DEFAULT now(),
      updated_at       timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      UNIQUE (tenant_id, reference),
      FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id) ON DELETE CASCADE
    );
    CREATE INDEX orders_conversation ON orders (tenant_id, conversation_id, created_at);

    CREATE TABLE payments (
      id                      uuid PRIMARY KEY,
      tenant_id               uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      order_id                uuid NOT NULL,
      provider                text NOT NULL DEFAULT 'fedapay',
      provider_transaction_id text NOT NULL,
      payment_url             text,
      status                  text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed')),
      amount                  integer NOT NULL,
      currency                text NOT NULL DEFAULT 'XOF',
      created_at              timestamptz NOT NULL DEFAULT now(),
      updated_at              timestamptz NOT NULL DEFAULT now(),
      paid_at                 timestamptz,
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id) ON DELETE CASCADE
    );
    CREATE INDEX payments_order ON payments (tenant_id, order_id, created_at);

    -- Routage des webhooks de paiement : retrouver le tenant à partir de l'identifiant de transaction du
    -- fournisseur, AVANT de connaître le tenant. Pas de RLS (identifiants seulement), alimentée par trigger.
    CREATE TABLE payment_routes (
      provider                text NOT NULL,
      provider_transaction_id text NOT NULL,
      payment_id              uuid NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
      tenant_id               uuid NOT NULL,
      PRIMARY KEY (provider, provider_transaction_id)
    );

    CREATE FUNCTION sync_payment_route() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $fn$
    BEGIN
      DELETE FROM payment_routes WHERE payment_id = NEW.id;
      INSERT INTO payment_routes (provider, provider_transaction_id, payment_id, tenant_id)
      VALUES (NEW.provider, NEW.provider_transaction_id, NEW.id, NEW.tenant_id);
      RETURN NEW;
    END
    $fn$;
    CREATE TRIGGER payments_route AFTER INSERT OR UPDATE OF provider_transaction_id ON payments
      FOR EACH ROW EXECUTE FUNCTION sync_payment_route();
    """)

    for table in ("orders", "payments"):
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
        """)

    op.execute(f"""
    GRANT SELECT, INSERT, UPDATE ON orders, payments TO {APP_ROLE};
    GRANT SELECT ON payment_routes TO {APP_ROLE};
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS payment_routes, payments, orders CASCADE;
    DROP FUNCTION IF EXISTS sync_payment_route();
    """)
