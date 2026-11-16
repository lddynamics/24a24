"""Publications sur la page Facebook : brouillons, programmation, publication, statistiques.

`social_schedule` (sans RLS, alimentée par trigger) permet au worker de trouver les publications dues
sans connaître le client à l'avance, comme `payment_routes` pour les paiements.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE social_posts (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      status          text NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'scheduled', 'publishing', 'published', 'failed', 'deleted')),
      message         text NOT NULL DEFAULT '',
      source          text NOT NULL DEFAULT 'human' CHECK (source IN ('human', 'ai')),
      product_sku     text,
      scheduled_for   timestamptz,
      attempts        integer NOT NULL DEFAULT 0,
      next_attempt_at timestamptz,
      fb_post_id      text,
      permalink       text,
      error           text,
      published_at    timestamptz,
      reactions       integer,
      comments        integer,
      shares          integer,
      stats_at        timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now(),
      updated_at      timestamptz NOT NULL DEFAULT now(),
      CHECK (status <> 'scheduled' OR scheduled_for IS NOT NULL)
    );
    CREATE INDEX social_posts_tenant_status ON social_posts (tenant_id, status, scheduled_for);

    CREATE TABLE social_post_media (
      post_id      uuid PRIMARY KEY REFERENCES social_posts(id) ON DELETE CASCADE,
      tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      content_type text NOT NULL,
      size         integer NOT NULL CHECK (size > 0),
      data         bytea NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE social_schedule (
      post_id   uuid PRIMARY KEY REFERENCES social_posts(id) ON DELETE CASCADE,
      tenant_id uuid NOT NULL,
      run_at    timestamptz NOT NULL
    );
    CREATE INDEX social_schedule_run ON social_schedule (run_at);

    CREATE FUNCTION sync_social_schedule() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $fn$
    BEGIN
      IF NEW.status = 'scheduled' THEN
        INSERT INTO social_schedule (post_id, tenant_id, run_at)
        VALUES (NEW.id, NEW.tenant_id, GREATEST(NEW.scheduled_for, COALESCE(NEW.next_attempt_at, NEW.scheduled_for)))
        ON CONFLICT (post_id) DO UPDATE SET run_at = EXCLUDED.run_at;
      ELSIF NEW.status = 'publishing' THEN      -- reprise si le worker s'est arrêté en pleine publication
        INSERT INTO social_schedule (post_id, tenant_id, run_at)
        VALUES (NEW.id, NEW.tenant_id, NEW.updated_at + interval '10 minutes')
        ON CONFLICT (post_id) DO UPDATE SET run_at = EXCLUDED.run_at;
      ELSE
        DELETE FROM social_schedule WHERE post_id = NEW.id;
      END IF;
      RETURN NEW;
    END
    $fn$;
    CREATE TRIGGER social_posts_schedule AFTER INSERT OR UPDATE OF status, scheduled_for, next_attempt_at, updated_at
      ON social_posts FOR EACH ROW EXECUTE FUNCTION sync_social_schedule();
    """)
    for table in ("social_posts", "social_post_media"):
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table} USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
        GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE};
        """)
    op.execute(f"GRANT SELECT ON social_schedule TO {APP_ROLE};")


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS social_schedule, social_post_media, social_posts CASCADE;
    DROP FUNCTION IF EXISTS sync_social_schedule();
    """)
