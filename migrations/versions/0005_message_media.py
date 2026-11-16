"""Médias de messages (vocaux) : archivés en base pour être écoutés depuis la console.

Les URL de médias fournies par Meta expirent vite : on copie l'audio à la réception. Les vocaux envoyés
par l'équipe y sont aussi conservés. Stockage `bytea` : suffisant pour le prototype (vocaux de quelques
dizaines de Ko) ; à remplacer par un stockage d'objets chiffré avant la production.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE message_media (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      message_id   uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
      content_type text NOT NULL,
      size         integer NOT NULL CHECK (size > 0),
      data         bytea NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      UNIQUE (message_id)
    );
    ALTER TABLE message_media ENABLE ROW LEVEL SECURITY;
    ALTER TABLE message_media FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON message_media USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
    GRANT SELECT, INSERT ON message_media TO {APP_ROLE};
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS message_media CASCADE;")
