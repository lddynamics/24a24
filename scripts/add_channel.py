"""Ajoute un canal (Messenger ou WhatsApp) à un client EXISTANT. À lancer avec le rôle PROPRIÉTAIRE.

WhatsApp : l'identifiant du canal est le « Phone number ID » (Meta for Developers > WhatsApp > API Setup), PAS le numéro
de téléphone. Le jeton doit être celui d'un utilisateur système (permanent) ayant accès au compte WhatsApp Business.

  ACCESS_TOKEN=EAAB... python -m scripts.add_channel --tenant-id <UUID> --channel whatsapp --external-id 1234567890
"""
import argparse
import getpass
import os
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text

from app.channels.credentials import encode
from app.config import get_settings
from app.crypto import encrypt

CHANNEL_TYPES = ("messenger", "whatsapp")


def add_channel(
    engine: Engine, *, tenant_id: UUID, channel_type: str, external_id: str, access_token: str, name: str | None = None
) -> UUID:
    if channel_type not in CHANNEL_TYPES:
        raise ValueError(f"canal non géré: {channel_type}")
    channel_id = uuid4()
    with engine.begin() as conn:
        # La RLS est forcée même pour le propriétaire : on se place dans le tenant concerné.
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO channels (id, tenant_id, type, external_id, name, credentials_encrypted) "
                "VALUES (:id, :tenant_id, :type, :ext, :name, :cred)"
            ),
            {
                "id": channel_id,
                "tenant_id": tenant_id,
                "type": channel_type,
                "ext": external_id,
                "name": name,
                "cred": encrypt(encode(channel_type, access_token, external_id)),
            },
        )
    return channel_id


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant-id", required=True, type=UUID)
    p.add_argument("--channel", required=True, choices=CHANNEL_TYPES)
    p.add_argument("--external-id", required=True, help="Messenger : identifiant de la page. WhatsApp : Phone number ID.")
    p.add_argument("--name", help="Libellé du canal")
    a = p.parse_args()
    token = os.environ.get("ACCESS_TOKEN") or getpass.getpass("Jeton d'accès : ")

    engine = create_engine(get_settings().migrations_database_url)
    channel_id = add_channel(
        engine, tenant_id=a.tenant_id, channel_type=a.channel, external_id=a.external_id, access_token=token, name=a.name
    )
    print(f"channel_id={channel_id}")


if __name__ == "__main__":
    main()
