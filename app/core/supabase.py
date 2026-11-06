
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import Client, create_client


class Settings(BaseSettings):
    supabase_url: str
    supabase_secret_key: str

    max_daily_otp_requests: int = 10

    # Comma-separated trusted proxy IPs/CIDRs.
    # Keep empty until authoritative proxy IP ranges are verified.
    trusted_proxies: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_supabase_client() -> Client:
    settings = get_settings()

    return create_client(
        settings.supabase_url,
        settings.supabase_secret_key,
    )
