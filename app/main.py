import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.config import get_settings
from app.db import assert_safe_role, get_engine
from app.routers import console, webhooks

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    assert_safe_role()

    # Hébergement gratuit (Render) : pas de « background worker » -> le worker tourne dans un thread du processus web.
    stop = threading.Event()
    thread = None
    if settings.embedded_worker:
        from app import worker

        runtime = worker.build_runtime()
        thread = threading.Thread(target=worker.run_loop, args=(*runtime, stop.is_set), name="embedded-worker", daemon=True)
        thread.start()
        log.info("worker intégré démarré dans le processus web")
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=10)


app = FastAPI(title="Agent24", lifespan=lifespan)
app.include_router(webhooks.router)
console.install(app)


@app.get("/health")
def health():
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}
