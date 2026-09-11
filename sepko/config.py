from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET_KEYS = frozenset(
    {
        "",
        "change-me-in-production",
        "changeme",
        "secret",
        "dev",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SEPKO_", extra="ignore")

    env: str = "development"
    # asyncpg za FastAPI; sync psycopg se izvodi automatski za seed/init_db
    database_url: str = "postgresql+asyncpg://sepko:sepko@localhost:5433/sepko"
    secret_key: str = "change-me-in-production"
    allowed_hosts: str = "127.0.0.1,localhost"
    partner_mode: str = "mock"  # mock | navira | http
    partner_base_url: str = ""
    partner_api_key: str = ""
    navira_base_url: str = ""
    navira_api_key: str = ""
    cron_secret: str = ""
    superadmin_password: str = ""
    prostudio_admin_password: str = ""
    hotel_admin_password: str = ""
    block_probes: bool = True
    global_rate_limit: bool = True
    cert_dir: str = ""
    cert_password: str = ""
    pkcs11_module: str = ""
    pkcs11_pin: str = ""
    database_admin_url: str = ""
    vault_addr: str = ""

    @field_validator("secret_key")
    @classmethod
    def secret_key_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("SEPKO_SECRET_KEY must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.env != "production":
            return self
        if self.secret_key.lower() in _INSECURE_SECRET_KEYS:
            raise ValueError("SEPKO_SECRET_KEY must be a strong random value in production")
        if self.partner_mode in ("navira", "http") and not (self.navira_api_key or self.partner_api_key):
            raise ValueError("Partner API key is required in production partner mode")
        return self

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
