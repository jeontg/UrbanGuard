"""Verify common/notifier.py (promoted from SAM's real app/notifier.py) works
in dry-run mode and reads the standardized SOLAPI_SENDER env var (not the
retired flood3 SMS_FROM name) — see docs/integration_plan.md sections 3-3
and 4 decision #5.
"""
import pytest

from tot_dashboard.common.notifier import (
    AlertNotifier,
    DuplicateNotificationError,
    NotificationConfigurationError,
    NotificationSettings,
)


def test_dry_run_requires_recipients_but_not_solapi_credentials(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_DRY_RUN", "true")
    monkeypatch.setenv("ALERT_RECIPIENTS", "01011112222,01033334444")
    monkeypatch.delenv("SOLAPI_API_KEY", raising=False)
    monkeypatch.delenv("SOLAPI_SENDER", raising=False)

    notifier = AlertNotifier()
    status = notifier.status()
    assert status["dry_run"] is True
    assert status["recipient_count"] == 2

    result = notifier.send(
        event_key="block-A:WIR_FLOOD_RISK",
        message={"body": "test alert", "severity_label": "경계"},
        channels=["sms"],
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["results"][0]["status"] == "dry-run"


def test_dry_run_without_recipients_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_DRY_RUN", "true")
    monkeypatch.delenv("ALERT_RECIPIENTS", raising=False)

    notifier = AlertNotifier()
    with pytest.raises(NotificationConfigurationError):
        notifier.send(event_key="k", message={"body": "x"}, channels=["sms"])


def test_cooldown_blocks_duplicate_events(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_DRY_RUN", "true")
    monkeypatch.setenv("ALERT_RECIPIENTS", "01011112222")
    monkeypatch.setenv("NOTIFICATION_COOLDOWN_SECONDS", "300")

    notifier = AlertNotifier()
    notifier.send(event_key="dup", message={"body": "x"}, channels=["sms"])
    with pytest.raises(DuplicateNotificationError):
        notifier.send(event_key="dup", message={"body": "x"}, channels=["sms"])


def test_sender_env_var_is_solapi_sender_not_sms_from(monkeypatch):
    """flood3's original .env.example used SMS_FROM, which no code ever read
    (docs/integration_plan.md section 3-3). Confirm this module reads the
    standardized SOLAPI_SENDER instead."""
    monkeypatch.setenv("SOLAPI_SENDER", "01099998888")
    monkeypatch.setenv("SMS_FROM", "01000000000")  # must be ignored
    settings = NotificationSettings.from_env()
    assert settings.sender == "01099998888"
