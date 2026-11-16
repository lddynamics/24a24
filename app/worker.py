"""Worker : lit la file de jobs et fait répondre l'agent.   python -m app.worker"""
import logging
import signal
import time

from app.agent.llm import LLMClient, RoutingLLM
from app.agent.orchestrator import handle_job, on_terminal_failure
from app.channels.sender import SendError, get_sender
from app.config import get_settings
from app.db import assert_safe_role
from app.services.jobs import Job, claim_job, complete_job, fail_job, requeue_stale
from app.services import fedapay as fedapay_service
from app.services import orders as order_service
from app.services import social
from app.services.media import HttpMediaFetcher
from app.services.transcribe import Transcriber, build_transcriber
from app.services.vision import Describer, build_describer

log = logging.getLogger("worker")
_stop = False


def process(job: Job, llm: LLMClient, sender_factory=get_sender, transcriber: Transcriber | None = None, media_fetcher=None,
            describer: Describer | None = None) -> None:
    try:
        status = handle_job(job, llm=llm, sender_factory=sender_factory, transcriber=transcriber, media_fetcher=media_fetcher,
                            describer=describer)
        complete_job(job.id)
        log.info("job %s -> %s", job.id, status)
    except SendError as e:
        terminal = fail_job(job, str(e), permanent=e.permanent)
        log.warning("job %s: échec d'envoi (%s) terminal=%s", job.id, e, terminal)
        if terminal:
            on_terminal_failure(job, sender_factory=sender_factory)
    except Exception as e:
        log.exception("job %s: échec", job.id)
        terminal = fail_job(job, f"{type(e).__name__}: {e}")
        if terminal:
            on_terminal_failure(job, sender_factory=sender_factory)


def run_once(llm: LLMClient, sender_factory=get_sender, transcriber: Transcriber | None = None, media_fetcher=None,
             describer: Describer | None = None) -> bool:
    """Traite au plus un job. Retourne False si la file était vide."""
    requeue_stale()
    job = claim_job()
    if job is None:
        return False
    process(job, llm, sender_factory, transcriber, media_fetcher, describer)
    return True


def run_scheduler() -> int:
    """Publie les publications Facebook dont l'heure est venue. Ne doit jamais interrompre le worker."""
    try:
        return len(social.publish_due())
    except Exception:
        log.exception("planificateur de publications : erreur")
        return 0


def run_payment_reconciliation() -> int:
    """Revérifie les paiements en attente auprès de FedaPay : le client est prévenu même si le webhook n'arrive pas.
    Ne doit jamais interrompre le worker."""
    settings = get_settings()
    try:
        client = fedapay_service.get_client()
    except fedapay_service.FedaPayError:
        return 0        # FedaPay non configuré : pas de paiements à suivre
    try:
        changed = order_service.reconcile_pending_payments(client, window_minutes=settings.payment_poll_window_minutes)
    except Exception:
        log.exception("réconciliation des paiements : erreur")
        return 0
    if changed:
        log.info("réconciliation : %s paiement(s) mis à jour", changed)
    return changed


def build_runtime():
    """Vérifie la configuration et construit les clients (LLM, vocaux, images). Lève SystemExit si une clé manque."""
    settings = get_settings()
    assert_safe_role()
    default_is_claude = settings.agent_model.startswith("claude")
    if default_is_claude and not settings.anthropic_api_key:
        raise SystemExit(f"ANTHROPIC_API_KEY manquant pour le modèle par défaut {settings.agent_model}")
    if not default_is_claude and not (settings.llm_api_key or settings.groq_api_key):
        raise SystemExit(f"LLM_API_KEY (ou GROQ_API_KEY) manquant pour le modèle par défaut {settings.agent_model}")
    llm = RoutingLLM.from_settings(settings)
    transcriber = build_transcriber()
    describer = build_describer()
    media_fetcher = HttpMediaFetcher()
    log.info(
        "worker démarré (modèle par défaut: %s, envoi: %s, vocaux: %s, images: %s)",
        settings.agent_model, settings.send_mode, "transcrits" if transcriber else "non transcrits",
        f"lues ({settings.vision_model})" if describer else "non lues",
    )
    return llm, transcriber, describer, media_fetcher


def run_loop(llm, transcriber, describer, media_fetcher, should_stop) -> None:
    """Boucle principale. `should_stop()` est interrogée à chaque tour (signal en mode autonome, Event en mode intégré)."""
    settings = get_settings()
    next_scheduler = next_payment_check = 0.0
    while not should_stop():
        if settings.payment_poll_seconds > 0 and time.monotonic() >= next_payment_check:
            run_payment_reconciliation()
            next_payment_check = time.monotonic() + settings.payment_poll_seconds
        if time.monotonic() >= next_scheduler:      # publications programmées : vérification toutes les 10 s
            run_scheduler()
            next_scheduler = time.monotonic() + settings.social_scheduler_seconds
        try:
            busy = run_once(llm, transcriber=transcriber, media_fetcher=media_fetcher, describer=describer)
        except Exception:                            # coupure de base (pooler Supabase, réveil...) : on retente, on ne meurt pas
            log.exception("worker : erreur sur la file, nouvel essai")
            busy = False
        if not busy:
            time.sleep(settings.worker_poll_seconds)
    log.info("worker arrêté proprement")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    llm, transcriber, describer, media_fetcher = build_runtime()

    def _handle(*_):
        global _stop
        _stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    run_loop(llm, transcriber, describer, media_fetcher, lambda: _stop)


if __name__ == "__main__":
    main()
