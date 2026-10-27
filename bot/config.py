from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    openrouter_api_key: str
    admin_id: int

    stt_model: str = "openai/whisper-large-v3-turbo"
    summary_model: str = "anthropic/claude-haiku-5.5"
    # default for users who haven't picked one in /settings; None = auto-detect
    stt_language: str | None = "ru"

    max_duration_sec: int = 3600
    max_concurrent_jobs: int = 3
    chunk_sec: int = 600

    db_path: str = "/app/data/bot.db"

    @field_validator("stt_language", mode="before")
    @classmethod
    def auto_to_none(cls, v):
        v = (v or "").strip().lower()
        return None if v in ("", "auto") else v


settings = Settings()
