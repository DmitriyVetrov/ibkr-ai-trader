"""Configuration safety: what the collector reads, and what it refuses to assume."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.readiness.collectors import collect_configuration, collect_secrets

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_the_collector_reads_the_loaded_settings(
    readiness_settings: Settings, system_config: SystemConfig
) -> None:
    """Loaded settings, not the YAML file.

    A ``.env`` that overrides a committed value is the value the process would
    actually run under; inspecting the file would miss it.
    """
    record = collect_configuration(
        settings=readiness_settings, config=system_config, config_error=None, observed_at=NOW
    )
    assert record.collected
    assert record.detail["trading_mode"] == "PAPER"
    assert record.detail["config_loaded"] is True


def test_the_shipped_execution_switch_is_reported_off(
    readiness_settings: Settings, system_config: SystemConfig
) -> None:
    """``config/execution.yaml`` ships ``enabled: false`` and must stay that way."""
    record = collect_configuration(
        settings=readiness_settings, config=system_config, config_error=None, observed_at=NOW
    )
    assert record.detail["execution_enabled"] is False
    assert record.detail["execution_allow_live"] is False


def test_a_configuration_failure_is_recorded_rather_than_raised(
    readiness_settings: Settings,
) -> None:
    """An assessor that crashed could not report that the config is broken."""
    record = collect_configuration(
        settings=readiness_settings,
        config=None,
        config_error="config/risk.yaml: unknown key 'maxx_loss'",
        observed_at=NOW,
    )
    assert record.collected is False
    assert record.detail["config_loaded"] is False
    assert "maxx_loss" in str(record.detail["config_error"])


def test_no_secret_reaches_the_evidence_record(
    readiness_settings: Settings, system_config: SystemConfig
) -> None:
    """Brief section 20. Only *whether* an account is configured, never which."""
    record = collect_configuration(
        settings=readiness_settings, config=system_config, config_error=None, observed_at=NOW
    )
    rendered = str(record.model_dump(mode="json"))
    for forbidden in ("password", "api_key", "secret", "token"):
        assert forbidden not in rendered.lower(), f"{forbidden} reached the evidence record"
    assert "account_configured" in record.detail
    assert "ibkr_account" not in record.detail


def test_the_live_guards_are_recorded(
    readiness_settings: Settings, system_config: SystemConfig
) -> None:
    record = collect_configuration(
        settings=readiness_settings, config=system_config, config_error=None, observed_at=NOW
    )
    assert record.detail["live_trading_confirmed"] is False
    assert record.detail["live_readiness_checklist_signed_off"] is False


def test_the_secret_scan_reports_what_it_checked(tmp_path) -> None:
    """A scan that names nothing is indistinguishable from one that ran nothing."""
    record = collect_secrets(repo_root=tmp_path, observed_at=NOW)
    assert "checked" in record.detail
    assert ".env" in record.detail["checked"]


def test_the_real_repository_tracks_no_secret() -> None:
    """The repository's own state, asserted rather than assumed."""
    repo = pathlib_root()
    record = collect_secrets(repo_root=repo, observed_at=NOW)
    assert record.detail["tracked_secret_files"] == []
    assert record.detail["dotenv_ignored"] is True


def pathlib_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]
