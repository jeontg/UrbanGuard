"""추적기 어댑터 — ByteTrack 을 어디서 가져오든 같은 얼굴로 쓴다.

왜 필요한가
    ``supervision.ByteTrack`` 이 **0.28.0 에서 폐기**됐고 **0.31.0 에서
    제거**됩니다. 후속은 별도 패키지 ``trackers`` 의 ``ByteTrackTracker`` 이며,
    **메서드 이름도 ``update_with_detections()`` → ``update()`` 로 바뀌었습니다.**

    호출부가 두 군데(인파·교통)라 각자 고치면 한쪽만 고치는 일이 생깁니다.
    여기 한곳에서 고르고, 부르는 쪽은 항상 :meth:`Tracker.update` 만 씁니다.

무엇을 조심했나
    * **새 패키지가 없어도 서비스는 떠야 합니다.** 없으면 폐기된
      ``supervision.ByteTrack`` 으로 되돌아갑니다 — 관제가 멈추는 것보다
      경고를 내며 도는 편이 낫습니다
    * **어느 쪽을 쓰고 있는지 밖에서 볼 수 있어야 합니다**(:attr:`backend`).
      추적 결과가 달라지면 원인을 여기서부터 찾게 됩니다
"""
from __future__ import annotations

import logging

log = logging.getLogger("urbanguard.tracking")

TRACKERS = "trackers"          # 후속 패키지 (권장)
SUPERVISION = "supervision"    # 폐기 예정 — 0.31.0 에서 제거
NONE = "none"                  # 둘 다 없음

# 기본 문턱값. ``trackers`` 쪽 기본이 0.7 로 높아 저화질 CCTV 에서 트랙이
# 잘 안 잡힌다. supervision 기본(0.25)에 맞춰 **기존 동작을 유지**한다.
DEFAULT_ACTIVATION = 0.25


class Tracker:
    """박스를 받아 추적 id 를 붙여 준다.

    :param fps: 프레임률. 트랙 유지 시간 계산에 쓰인다.
    :param activation: 이 신뢰도 미만은 새 트랙으로 만들지 않는다.
    """

    def __init__(self, fps: float = 30.0, activation: float = DEFAULT_ACTIVATION):
        self.backend = NONE
        self._impl = None
        self._call = None
        fr = float(max(fps, 1.0))

        try:
            from trackers import ByteTrackTracker

            self._impl = ByteTrackTracker(frame_rate=fr,
                                          track_activation_threshold=activation)
            self._call = self._impl.update
            self.backend = TRACKERS
            return
        except Exception as e:  # noqa: BLE001
            log.debug("trackers 패키지를 쓸 수 없습니다: %s", str(e)[:120])

        try:
            import warnings

            import supervision as sv
            with warnings.catch_warnings():
                # 폐기 경고는 여기서 한 번만 남긴다 — 매 프레임 찍히면 로그가 묻힌다.
                warnings.simplefilter("ignore", FutureWarning)
                self._impl = sv.ByteTrack(frame_rate=int(fr),
                                          track_activation_threshold=activation)
            self._call = self._impl.update_with_detections
            self.backend = SUPERVISION
            log.warning("폐기 예정 supervision.ByteTrack 을 씁니다 "
                        "(0.31.0 에서 제거). `pip install trackers` 를 권합니다.")
        except Exception as e:  # noqa: BLE001
            log.warning("추적기를 쓸 수 없습니다 — 추적 없이 진행합니다: %s",
                        str(e)[:120])

    @property
    def available(self) -> bool:
        return self._call is not None

    def update(self, detections):
        """``supervision.Detections`` 를 받아 추적 id 가 붙은 것을 돌려준다.

        **검출을 버리지 않는다.** 사람을 세는 일과 속도를 재는 일은 다르다 —
        추적이 아직 확정되지 않았다고 **화면의 인원수가 0이 되면 안 된다.**
        확정 여부는 :func:`is_confirmed` 로 부르는 쪽이 가린다.

        추적기가 없으면 받은 것을 그대로 돌려준다. id 가 없어 속도는 못 구하지만
        **인원수·밀집도는 계속 나온다** — 전부 멈추는 것보다 낫다.
        """
        if self._call is None:
            return detections
        try:
            return self._call(detections)
        except Exception as e:  # noqa: BLE001
            log.warning("추적 실패(이번 프레임 건너뜀): %s", str(e)[:120])
            return detections


def is_confirmed(track_id) -> bool:
    """이 추적 id 를 **속도 계산에 써도 되는가.**

    ⚠️ **두 구현의 계약이 다르다.** 실측으로 확인했다.

    ==========================  ==========================================
    supervision.ByteTrack       확정된 것만 돌려준다
    trackers.ByteTrackTracker   **미확정에 ``tracker_id = -1``** 을 붙여
                                함께 돌려준다 (첫 프레임은 항상 −1)
    ==========================  ==========================================

    미확정을 트랙으로 취급하면 **여러 박스가 「id −1 인 한 트랙」으로 뭉치고**,
    다음 프레임에 진짜 id 가 붙는 순간 좌표가 급점프해 **속도가 터무니없이
    커진다**(실측 41,231 px/s).
    """
    try:
        return int(track_id) >= 0
    except (TypeError, ValueError):
        return False


def describe(backend: str) -> str:
    """화면·로그에 쓸 설명."""
    return {
        TRACKERS: "trackers.ByteTrackTracker",
        SUPERVISION: "supervision.ByteTrack (폐기 예정)",
        NONE: "추적기 없음",
    }.get(backend, backend)
