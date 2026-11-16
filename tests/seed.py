from app.db import tenant_session
from app.services import catalog as catalog_service


def seed_catalog(tenant_id, config) -> int:
    with tenant_session(tenant_id) as s:
        return catalog_service.import_from_config(s, tenant_id, config)
