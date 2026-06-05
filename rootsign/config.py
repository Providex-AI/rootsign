"""Store-side infrastructure settings (database, pool sizing).

These are intentionally NOT prefixed with ROOTSIGN_ — they describe the
PostgreSQL/TimescaleDB instance the storage layer connects to, which is
operator/infra config (CI, docker-compose, prod secrets) rather than
SDK-user-facing configuration. SDK-user config lives in
`rootsign/sdk/config.py` with env_prefix="ROOTSIGN_".
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql+asyncpg://rootsign:rootsign@localhost:5432/rootsign_dev"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://rootsign:rootsign@localhost:5432/rootsign_dev"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://rootsign:rootsign@localhost:5432/rootsign_test"
    TEST_DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://rootsign:rootsign@localhost:5432/rootsign_test"
    )

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30


settings = Settings()
