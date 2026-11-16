"""Enregistre (chiffré) un nouveau jeton de PAGE Facebook pour un client. À lancer avec le rôle PROPRIÉTAIRE.

  python -m scripts.set_page_token --tenant-id <UUID>          (le jeton est demandé, jamais affiché)
  PAGE_ACCESS_TOKEN=EAAB... python -m scripts.set_page_token --tenant-id <UUID>

Le jeton sert à la fois à Messenger et à la publication sur la page : il doit avoir pages_messaging,
pages_manage_posts, pages_read_engagement et pages_show_list.
"""
import argparse
import getpass
import os
from uuid import UUID

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings
from app.crypto import encrypt


def set_page_token(engine: Engine, tenant_id: UUID, token: str) -> int:
    token = token.strip()
    if len(token) < 20:
        raise ValueError("Jeton invalide (trop court).")
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        res = conn.execute(
            text("UPDATE channels SET credentials_encrypted = :c WHERE type = 'messenger' AND tenant_id = :t"),
            {"c": encrypt(token), "t": tenant_id},
        )
        return res.rowcount


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant-id", required=True)
    args = p.parse_args()
    token = os.environ.get("PAGE_ACCESS_TOKEN") or getpass.getpass("Nouveau jeton de la page : ")
    n = set_page_token(create_engine(get_settings().migrations_database_url), UUID(args.tenant_id), token)
    print("jeton mis à jour" if n else "aucun canal Messenger trouvé pour ce client")


if __name__ == "__main__":
    main()
