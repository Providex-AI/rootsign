"""Unit tests for the SDK-side settings — env_prefix=ROOTSIGN_."""

from __future__ import annotations

import pytest

from rootsign.sdk.config import SDKSettings


class TestSDKSettingsDefaults:
    def test_backend_defaults_to_jsonl(self, monkeypatch):
        # ADR-011: default flipped to jsonl in v0.2.0. delenv the conftest pin
        # AND bypass any developer-local .env (_env_file=None) to observe the
        # true default.
        monkeypatch.delenv("ROOTSIGN_BACKEND", raising=False)
        s = SDKSettings(_env_file=None)
        assert s.BACKEND == "jsonl"

    def test_jsonl_settings_defaults(self, monkeypatch):
        monkeypatch.delenv("ROOTSIGN_BACKEND", raising=False)
        # The conftest data-dir isolation must be lifted to see the real default.
        monkeypatch.delenv("ROOTSIGN_DATA_DIR", raising=False)
        s = SDKSettings(_env_file=None)
        assert s.DATA_DIR == "~/.rootsign"
        assert s.JSONL_FSYNC == "chain"

    def test_spool_dir_derives_from_data_dir(self, monkeypatch):
        """ADR-013 Decision 4: moving DATA_DIR must move the spool with it."""
        monkeypatch.delenv("ROOTSIGN_SPOOL_DIR", raising=False)
        monkeypatch.setenv("ROOTSIGN_DATA_DIR", "/var/lib/agent")
        assert SDKSettings(_env_file=None).SPOOL_DIR == "/var/lib/agent/spool"

    def test_spool_dir_default_sits_under_the_default_data_dir(self, monkeypatch):
        monkeypatch.delenv("ROOTSIGN_SPOOL_DIR", raising=False)
        monkeypatch.delenv("ROOTSIGN_DATA_DIR", raising=False)
        assert SDKSettings(_env_file=None).SPOOL_DIR == "~/.rootsign/spool"

    def test_spool_dir_can_be_split_from_data_dir(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_DATA_DIR", "/var/lib/agent")
        monkeypatch.setenv("ROOTSIGN_SPOOL_DIR", "/mnt/durable/spool")
        assert SDKSettings(_env_file=None).SPOOL_DIR == "/mnt/durable/spool"

    def test_local_backend_alias_deprecated_maps_to_postgres(self, monkeypatch):
        # ADR-011: 'local' is the deprecated alias for 'postgres'.
        monkeypatch.setenv("ROOTSIGN_BACKEND", "local")
        with pytest.warns(DeprecationWarning, match="deprecated"):
            s = SDKSettings()
        assert s.BACKEND == "postgres"

    def test_cloud_url_default(self):
        s = SDKSettings()
        assert s.CLOUD_URL == "https://ingest.getprovidex.com/v1"

    def test_capture_decisions_defaults_false(self):
        s = SDKSettings()
        assert s.CAPTURE_DECISIONS is False

    def test_retry_policy_defaults(self):
        s = SDKSettings()
        assert s.MAX_RETRIES == 3
        assert s.RETRY_BASE_DELAY == pytest.approx(0.1)
        assert s.RETRY_MAX_DELAY == pytest.approx(5.0)


class TestSDKSettingsEnvOverride:
    def test_env_prefix_reads_rootsign_vars(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "cloud")
        monkeypatch.setenv("ROOTSIGN_API_KEY", "sk-test-123")
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")

        s = SDKSettings()
        assert s.BACKEND == "cloud"
        assert s.API_KEY == "sk-test-123"
        assert s.CAPTURE_DECISIONS is True

    def test_bare_var_without_prefix_ignored(self, monkeypatch):
        """A bare BACKEND env var (no ROOTSIGN_ prefix) must not leak in."""
        monkeypatch.delenv("ROOTSIGN_BACKEND", raising=False)
        monkeypatch.setenv("BACKEND", "cloud")  # should be ignored
        s = SDKSettings(_env_file=None)
        assert s.BACKEND == "jsonl"

    def test_invalid_backend_rejected(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "satellite")
        with pytest.raises(Exception):  # pydantic ValidationError
            SDKSettings()
