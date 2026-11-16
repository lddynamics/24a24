"""Supabase : retire l'accès des rôles de l'API Data (anon / authenticated) aux tables de l'application.

Supabase expose le schéma public via PostgREST avec la clé « anon ». Les tables sans RLS (tenants, channels,
channel_routes...) seraient lisibles par n'importe qui connaissant l'URL du projet. L'application n'utilise
QUE la connexion Postgres directe (rôle agent24_app) : on ferme donc cette porte.
No-op sur un PostgreSQL ordinaire (les rôles n'existent pas).

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    DO $$
    DECLARE r text;
    BEGIN
      FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
          EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
          EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
          EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', r);
          EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
          EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', r);
          EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I', r);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade() -> None:
    pass    # on ne rouvre jamais l'accès public
