"""Statut et date dans payment_routes : le worker retrouve les paiements en attente sans webhook.

`payment_routes` (sans RLS, alimentée par trigger) sert déjà à retrouver le client d'une transaction. On y copie
aussi `status` et `created_at` pour que le worker puisse lister, tous clients confondus, les paiements encore
« pending » et les revérifier auprès de FedaPay (filet de sécurité si un webhook n'arrive pas).

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE payment_routes
      ADD COLUMN status     text        NOT NULL DEFAULT 'pending',
      ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
    UPDATE payment_routes r SET status = p.status, created_at = p.created_at
      FROM payments p WHERE p.id = r.payment_id;
    CREATE INDEX payment_routes_pending ON payment_routes (created_at) WHERE status = 'pending';

    CREATE OR REPLACE FUNCTION sync_payment_route() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $fn$
    BEGIN
      DELETE FROM payment_routes WHERE payment_id = NEW.id;
      INSERT INTO payment_routes (provider, provider_transaction_id, payment_id, tenant_id, status, created_at)
      VALUES (NEW.provider, NEW.provider_transaction_id, NEW.id, NEW.tenant_id, NEW.status, NEW.created_at);
      RETURN NEW;
    END
    $fn$;
    DROP TRIGGER payments_route ON payments;
    CREATE TRIGGER payments_route AFTER INSERT OR UPDATE OF provider_transaction_id, status ON payments
      FOR EACH ROW EXECUTE FUNCTION sync_payment_route();
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER payments_route ON payments;
    CREATE OR REPLACE FUNCTION sync_payment_route() RETURNS trigger
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
    DROP INDEX payment_routes_pending;
    ALTER TABLE payment_routes DROP COLUMN status, DROP COLUMN created_at;
    """)
