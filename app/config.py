from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str                 # rôle applicatif (soumis à la RLS)
    migrations_database_url: str      # rôle propriétaire (migrations, scripts d'admin)
    meta_app_secret: str              # secret de l'app Meta : vérifie la signature des webhooks
    messenger_verify_token: str       # jeton de vérification de l'abonnement webhook
    encryption_key: str               # clé Fernet
    log_level: str = "INFO"

    anthropic_api_key: str = ""       # requis pour le worker (sauf tests avec faux LLM)
    agent_model: str = "claude-sonnet-5-5"  # modèle par défaut, surchargeable par client (config.agent.model)
    # Fournisseur du modèle de l'agent quand le nom ne commence pas par « claude » (API compatible OpenAI) :
    # NVIDIA NIM, Gemini, OpenRouter, Groq… Vide = on retombe sur GROQ_API_KEY / GROQ_BASE_URL (ancien comportement).
    llm_api_key: str = ""
    llm_base_url: str = ""            # ex. https://integrate.api.nvidia.com/v1
    graph_api_version: str = "v23.0"  # à vérifier dans le changelog Meta
    send_mode: Literal["messenger", "console"] = "messenger"  # "console" : affiche au lieu d'envoyer
    worker_poll_seconds: float = 1.0
    # True : le worker tourne dans le processus web (hébergement gratuit sans worker séparé). False : `python -m app.worker`.
    embedded_worker: bool = False

    # WhatsApp NON officiel via Evolution API (prototype). Voir docker-compose.evolution.yml.
    evolution_api_url: str = "http://localhost:8080"
    evolution_api_key: str = ""             # AUTHENTICATION_API_KEY d'Evolution
    evolution_webhook_secret: str = ""      # secret que l'on fait envoyer par Evolution dans X-Webhook-Secret (vide = webhook refusé)

    social_scheduler_seconds: float = 10.0   # fréquence de vérification des publications programmées

    # Console d'entreprise (pages servies par le backend). Désactivée tant que le mot de passe est vide.
    console_password: str = ""
    console_tenant_id: str = ""         # UUID du client géré par la console (voir « SELECT id FROM tenants »)
    console_session_hours: int = 12
    console_utc_offset_hours: int = 1   # affichage des heures (Bénin : UTC+1)

    # Paiement FedaPay (un seul agrégateur). Clés du bac à sable en développement.
    fedapay_secret_key: str = ""
    fedapay_environment: str = "sandbox"      # "sandbox" ou "live"
    fedapay_webhook_secret: str = ""          # secret du endpoint webhook (tableau de bord FedaPay)
    payment_poll_seconds: int = 20            # le worker revérifie les paiements en attente (0 = désactivé) : filet si le webhook manque
    payment_poll_window_minutes: int = 360    # ... pour les paiements créés depuis moins de 6 h
    fedapay_callback_url: str = ""            # page vers laquelle FedaPay renvoie le client (optionnel)
    fedapay_customer_email: str = ""          # e-mail de l'entreprise : reçoit la facture FedaPay (surchargeable par client)

    # Transcription des messages vocaux (API compatible OpenAI ; Groq par défaut). Vide = désactivée.
    groq_api_key: str = ""              # sert aussi aux modèles gratuits de test (voir app/agent/llm.py)
    groq_base_url: str = "https://api.groq.com/openai/v1"
    transcription_model: str = "whisper-large-v3-turbo"
    transcription_language: str = "fr"   # code ISO 639-1 ; vide = détection automatique
    transcription_max_bytes: int = 10_000_000

    # Lecture des images envoyées par les clients (modèle de vision). Vide = images non lues (l'agent demande d'écrire).
    # « claude-… » -> Anthropic (ANTHROPIC_API_KEY) ; sinon API compatible OpenAI (NVIDIA NIM, Groq…), mêmes clés que l'agent.
    vision_model: str = ""
    vision_api_key: str = ""            # vide = LLM_API_KEY, puis GROQ_API_KEY
    vision_base_url: str = ""           # vide = LLM_BASE_URL, puis GROQ_BASE_URL
    vision_timeout_seconds: float = 45.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
