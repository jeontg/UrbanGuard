"""Real SOLAPI SMS / Kakao Alimtalk alert notifier — canonical for all domains.

Promoted from SAM's ``app/notifier.py`` (docs/integration_plan.md sections 2
and 4, decision #5). This is the only one of the three original projects with
a genuinely working notification implementation — flood3's own
``orchestrator/notifier.py`` (``DryRunNotifier``, ported to
``traffic_weather/dry_run_notifier.py`` in Phase 3) was always a
placeholder; its own docstring said as much ("Phase 2에서 SAM AlertNotifier를
이식한다"). This module is that promotion, made available to every domain
(flood/traffic_weather/crowd) rather than just SAM's crowd dashboard.

★ Env var fix (docs/integration_plan.md section 3-3): flood3's ``.env.example``
used ``SMS_FROM`` for the sender number, but no flood3 code ever actually read
it (``DryRunNotifier`` doesn't touch env at all) — only this module's
``SOLAPI_SENDER`` was ever live code. ``SOLAPI_SENDER`` is standardized on;
``SMS_FROM`` is retired.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from solapi import SolapiMessageService
from solapi.model import RequestMessage
from solapi.model.kakao.kakao_option import KakaoOption

Channel = Literal["sms", "kakao"]
PHONE_PATTERN = re.compile(r"^\d{9,12}$")


class NotificationConfigurationError(RuntimeError):
    """Raised when live message delivery is not configured."""


class DuplicateNotificationError(RuntimeError):
    """Raised when the same alert is submitted repeatedly within the cooldown."""


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _phone(value: str) -> str:
    normalized = re.sub(r"\D", "", value)
    if not PHONE_PATTERN.fullmatch(normalized):
        raise NotificationConfigurationError(f"잘못된 전화번호 형식: {value!r}")
    return normalized


def _masked_phone(value: str) -> str:
    if len(value) < 7:
        return "***"
    return f"{value[:3]}****{value[-4:]}"


@dataclass(frozen=True)
class NotificationSettings:
    api_key: str
    api_secret: str
    sender: str
    recipients: tuple[str, ...]
    kakao_pf_id: str
    kakao_template_id: str
    dry_run: bool
    cooldown_seconds: int

    @classmethod
    def from_env(cls) -> "NotificationSettings":
        recipients = tuple(
            _phone(item)
            for item in os.getenv("ALERT_RECIPIENTS", "").split(",")
            if item.strip()
        )
        sender_raw = os.getenv("SOLAPI_SENDER", "").strip()
        sender = _phone(sender_raw) if sender_raw else ""
        return cls(
            api_key=os.getenv("SOLAPI_API_KEY", "").strip(),
            api_secret=os.getenv("SOLAPI_API_SECRET", "").strip(),
            sender=sender,
            recipients=recipients,
            kakao_pf_id=(
                os.getenv("SOLAPI_KAKAO_PF_ID")
                or os.getenv("SOLAPI_KAKAO_CHANNEL_ID")
                or ""
            ).strip(),
            kakao_template_id=os.getenv("SOLAPI_KAKAO_TEMPLATE_ID", "").strip(),
            dry_run=_env_flag("NOTIFICATION_DRY_RUN", default=True),
            cooldown_seconds=max(
                0, int(os.getenv("NOTIFICATION_COOLDOWN_SECONDS", "30"))
            ),
        )

    def channel_ready(self, channel: Channel) -> bool:
        common = bool(
            self.api_key
            and self.api_secret
            and self.sender
            and self.recipients
        )
        if channel == "sms":
            return common
        return common and bool(self.kakao_pf_id and self.kakao_template_id)

    def public_status(self) -> dict[str, Any]:
        dry_run_available = self.dry_run and bool(self.recipients)
        return {
            "provider": "SOLAPI",
            "dry_run": self.dry_run,
            "sms_ready": self.channel_ready("sms"),
            "kakao_ready": self.channel_ready("kakao"),
            "sms_available": self.channel_ready("sms") or dry_run_available,
            "kakao_available": self.channel_ready("kakao") or dry_run_available,
            "recipients": [_masked_phone(item) for item in self.recipients],
            "recipient_count": len(self.recipients),
            "cooldown_seconds": self.cooldown_seconds,
        }


class AlertNotifier:
    """Send server-generated alert messages to configured phones only.

    Domain-agnostic: ``message`` is a plain dict — any domain (flood/
    traffic_weather/crowd) supplies its own ``severity_label``/``location``/
    ``context``/``recommendation``/``video_time``/``risk_score`` values (the
    Kakao template variable names are generic Korean labels, not
    crowd-specific).
    """

    def __init__(self, settings: NotificationSettings | None = None) -> None:
        self.settings = settings or NotificationSettings.from_env()
        self._client: SolapiMessageService | None = None
        self._recent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        return self.settings.public_status()

    def send(
        self,
        *,
        event_key: str,
        message: dict[str, Any],
        channels: list[Channel],
    ) -> dict[str, Any]:
        unique_channels = list(dict.fromkeys(channels))
        if not unique_channels:
            raise NotificationConfigurationError("전송 채널을 선택해 주세요.")
        if any(channel not in {"sms", "kakao"} for channel in unique_channels):
            raise NotificationConfigurationError("지원하지 않는 전송 채널입니다.")

        for channel in unique_channels:
            self._ensure_ready(channel)

        claimed: list[Channel] = []
        try:
            for channel in unique_channels:
                self._claim(event_key, channel)
                claimed.append(channel)
        except Exception:
            for channel in claimed:
                self._release(event_key, channel)
            raise

        results = []
        for channel in unique_channels:
            try:
                result = self._send_channel(channel, message)
            except Exception as exc:
                self._release(event_key, channel)
                result = {
                    "channel": channel,
                    "status": "failed",
                    "error": str(exc),
                }
            results.append(result)

        return {
            "ok": all(item["status"] != "failed" for item in results),
            "dry_run": self.settings.dry_run,
            "event_key": event_key,
            "recipients": [
                _masked_phone(item) for item in self.settings.recipients
            ],
            "results": results,
        }

    def _ensure_ready(self, channel: Channel) -> None:
        if self.settings.dry_run:
            if not self.settings.recipients:
                raise NotificationConfigurationError(
                    "ALERT_RECIPIENTS가 설정되지 않았습니다."
                )
            return
        if not self.settings.channel_ready(channel):
            if channel == "kakao":
                raise NotificationConfigurationError(
                    "카카오 알림톡 설정(API 키, 발신번호, 수신번호, PF ID, 템플릿 ID)이 필요합니다."
                )
            raise NotificationConfigurationError(
                "문자 설정(API 키, 발신번호, 수신번호)이 필요합니다."
            )

    def _claim(self, event_key: str, channel: Channel) -> None:
        key = (event_key, channel)
        now = time.monotonic()
        with self._lock:
            previous = self._recent.get(key)
            if (
                previous is not None
                and now - previous < self.settings.cooldown_seconds
            ):
                remaining = self.settings.cooldown_seconds - int(now - previous)
                raise DuplicateNotificationError(
                    f"중복 발송 방지 중입니다. 약 {remaining}초 후 다시 시도해 주세요."
                )
            self._recent[key] = now

    def _release(self, event_key: str, channel: Channel) -> None:
        with self._lock:
            self._recent.pop((event_key, channel), None)

    def _send_channel(
        self, channel: Channel, message: dict[str, Any]
    ) -> dict[str, Any]:
        body = str(message["body"])
        if self.settings.dry_run:
            return {
                "channel": channel,
                "status": "dry-run",
                "recipient_count": len(self.settings.recipients),
            }

        requests = [
            self._request(channel, recipient, body, message)
            for recipient in self.settings.recipients
        ]
        response = self._service().send(requests)
        group_info = response.group_info
        count = group_info.count
        return {
            "channel": channel,
            "status": "submitted",
            "group_id": group_info.group_id,
            "requested": getattr(count, "total", len(requests)),
            "registered_success": getattr(
                count, "registered_success", getattr(count, "registered", None)
            ),
            "registered_failed": getattr(count, "registered_failed", None),
        }

    def _request(
        self,
        channel: Channel,
        recipient: str,
        body: str,
        message: dict[str, Any],
    ) -> RequestMessage:
        if channel == "sms":
            return RequestMessage(
                from_=self.settings.sender,
                to=recipient,
                text=body,
            )

        variables = {
            "#{등급}": str(message.get("severity_label", "경보")),
            "#{장소}": str(message.get("location", "해당 구역")),
            "#{상황}": str(message.get("context", "위험이 감지되었습니다.")),
            "#{조치}": str(message.get("recommendation", "현장을 확인해 주세요.")),
            "#{영상시각}": str(message.get("video_time", "-")),
            "#{위험점수}": f"{float(message.get('risk_score', 0)):.3f}",
        }
        return RequestMessage(
            from_=self.settings.sender,
            to=recipient,
            text=body,
            kakaoOptions=KakaoOption(
                pfId=self.settings.kakao_pf_id,
                templateId=self.settings.kakao_template_id,
                variables=variables,
                disableSms=True,
            ),
        )

    def _service(self) -> SolapiMessageService:
        if self._client is None:
            self._client = SolapiMessageService(
                api_key=self.settings.api_key,
                api_secret=self.settings.api_secret,
            )
        return self._client
