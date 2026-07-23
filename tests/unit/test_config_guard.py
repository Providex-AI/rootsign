"""audit #7c: the store DATABASE_URL defaults are dev-credentialed localhost
URLs. A production deployment that forgets to set DATABASE_URL must fail
closed rather than silently connect to a throwaway local DB.

Explicit DATABASE_URL/DATABASE_URL_SYNC kwargs are passed so the test is
deterministic regardless of any DATABASE_URL exported in the environment.
"""

from __future__ import annotations

import pytest

from rootsign.config import Settings

_DEV_ASYNC = "postgresql+asyncpg://rootsign:rootsign@localhost:5432/rootsign_dev"
_DEV_SYNC = "postgresql+psycopg2://rootsign:rootsign@localhost:5432/rootsign_dev"
_PROD_ASYNC = "postgresql+asyncpg://produser:secret@db.internal:5432/rootsign"
_PROD_SYNC = "postgresql+psycopg2://produser:secret@db.internal:5432/rootsign"


class TestProductionDBGuard:
    def test_development_with_dev_url_is_fine(self):
        s = Settings(
            ENVIRONMENT="development",
            DATABASE_URL=_DEV_ASYNC,
            DATABASE_URL_SYNC=_DEV_SYNC,
        )
        assert s.ENVIRONMENT == "development"

    def test_production_with_dev_url_raises(self):
        with pytest.raises(ValueError, match="production"):
            Settings(
                ENVIRONMENT="production",
                DATABASE_URL=_DEV_ASYNC,
                DATABASE_URL_SYNC=_DEV_SYNC,
            )

    def test_production_is_case_insensitive(self):
        with pytest.raises(ValueError):
            Settings(
                ENVIRONMENT="Production",
                DATABASE_URL=_DEV_ASYNC,
                DATABASE_URL_SYNC=_DEV_SYNC,
            )

    def test_production_with_real_url_is_fine(self):
        s = Settings(
            ENVIRONMENT="production",
            DATABASE_URL=_PROD_ASYNC,
            DATABASE_URL_SYNC=_PROD_SYNC,
        )
        assert s.ENVIRONMENT == "production"
