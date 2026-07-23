"""Store-side infrastructure settings (database, pool sizing).

These are intentionally NOT prefixed with ROOTSIGN_ — they describe the
PostgreSQL/TimescaleDB instance the storage layer connects to, which is
operator/infra config (CI, docker-compose, prod secrets) rather than
SDK-user-facing configuration. SDK-user config lives in
`rootsign/sdk/config.py` with env_prefix="ROOTSIGN_".
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Substring that identifies the dev-credentialed local default DB URLs. Used
# by the production guard below (audit #7c).
_DEV_DB_MARKER = "rootsign:rootsign@localhost"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment environment. Defaults to development so dev/test/CI need no
    # extra config; set ENVIRONMENT=production in real deployments to arm the
    # DATABASE_URL guard below.
    ENVIRONMENT: str = "development"

    DATABASE_URL: str = "postgresql+asyncpg://rootsign:rootsign@localhost:5432/rootsign_dev"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://rootsign:rootsign@localhost:5432/rootsign_dev"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://rootsign:rootsign@localhost:5432/rootsign_test"
    TEST_DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://rootsign:rootsign@localhost:5432/rootsign_test"
    )

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30

    @model_validator(mode="after")
    def _guard_production_db(self) -> "Settings":
        """Fail closed instead of silently connecting to the dev DB in prod.

        audit #7c: the DATABASE_URL defaults are dev-credentialed localhost
        URLs, so a production deployment that forgets to set DATABASE_URL would
        otherwise connect to a throwaway local database rather than erroring.
        """
        if self.ENVIRONMENT.strip().lower() == "production" and (
            _DEV_DB_MARKER in self.DATABASE_URL or _DEV_DB_MARKER in self.DATABASE_URL_SYNC
        ):
            raise ValueError(
                "ENVIRONMENT=production but DATABASE_URL still points at the "
                "dev-credentialed local database. Set DATABASE_URL (and "
                "DATABASE_URL_SYNC) explicitly for production deployments."
            )
        return self


settings = Settings()
