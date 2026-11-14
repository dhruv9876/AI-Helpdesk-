from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Helpdesk"
    database_url: str = "sqlite:///./helpdesk.db"
    secret_key: str = "dev-secret-key-change-this-before-deploying"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    bcrypt_rounds: int = 12  # tests lower this to run fast

    # --- knowledge base / RAG ---
    embedding_backend: str = "hashing"  # "hashing" (offline, default) or "sentence-transformers"
    embedding_model: str = "all-MiniLM-L6-v2"  # used by the sentence-transformers backend
    kb_top_k: int = 4
    kb_min_score: float = 0.12  # pre-filter: below this the bot escalates without calling the LLM
    chunk_max_chars: int = 700

    # --- AI ticket workflow (phase 3) ---
    ai_auto_triage: bool = True  # run triage + duplicate check in the background on new tickets
    dup_min_score: float = 0.15  # calibrated in scripts/eval_duplicates.py

    # --- dashboard (phase 5) ---
    show_demo_logins: bool = False  # list the seeded demo accounts on the login page (demo only!)

    # --- live chat (phase 4) ---
    chat_max_message_chars: int = 2000
    chat_messages_per_minute: int = 20  # per user per chat; protects the bot/LLM from floods
    chat_auth_timeout_seconds: float = 10.0  # a websocket must authenticate within this time

    # --- LLM (leave the key empty to use the offline mock) ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
