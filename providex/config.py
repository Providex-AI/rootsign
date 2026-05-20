from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://providex:providex@localhost:5432/providex_dev"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://providex:providex@localhost:5432/providex_dev"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://providex:providex@localhost:5432/providex_test"
    TEST_DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://providex:providex@localhost:5432/providex_test"
    )

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30


settings = Settings()
