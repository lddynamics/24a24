import os

# Les tests tournent TOUJOURS sur une base dédiée (jamais la base de dev).
TEST_APP_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://agent24_app:app_pw@localhost:5432/agent24_test")
TEST_OWNER_URL = os.environ.get(
    "TEST_MIGRATIONS_DATABASE_URL", "postgresql+psycopg://agent24_owner:owner_pw@localhost:5432/agent24_test"
)
assert TEST_APP_URL.endswith("_test") and TEST_OWNER_URL.endswith("_test"), "les tests exigent une base *_test"

os.environ["DATABASE_URL"] = TEST_APP_URL
os.environ["MIGRATIONS_DATABASE_URL"] = TEST_OWNER_URL
os.environ["META_APP_SECRET"] = "test_secret"
os.environ["FEDAPAY_WEBHOOK_SECRET"] = "whsec_test"
os.environ["MESSENGER_VERIFY_TOKEN"] = "test_verify"
os.environ["ENCRYPTION_KEY"] = "1McfU3gfbUe7T7rwiuHhbRcPFwx94bIAKgysg3jFuRk="  # clé de test

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from scripts.create_tenant import create_tenant  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated():
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def owner_engine():
    return create_engine(TEST_OWNER_URL)


@pytest.fixture(autouse=True)
def clean_db(owner_engine):
    yield
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE tenants CASCADE"))


@pytest.fixture
def make_tenant(owner_engine):
    def _make(slug: str, page_id: str):
        return create_tenant(
            owner_engine, name=slug, slug=slug, sector=None, page_id=page_id, page_token="tok_" + slug
        )

    return _make
