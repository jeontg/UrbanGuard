"""운영 모델 선택 — 상시 탐지가 실제로 쓰는 모델을 화면에서 정한다 (S-61).

시험대(:mod:`.model_probe`)와 무엇이 다른가
    시험대는 「대 봤다」이고, 여기는 「이걸로 간다」입니다. 시험대에서 더 나은
    모델을 찾아도 그걸 운영에 적용하려면 코드를 고쳐야 했습니다 — 그 마지막
    한 칸을 메웁니다.

즉시 반영과 재기동 필요를 **구분해서 말합니다**
    도메인마다 모델을 붙드는 방식이 달라 반영 시점이 다릅니다.

    * **노면** — 분석기 객체 하나를 상시 순회가 계속 쓰고 있어, 그 객체의
      모델만 갈아 끼우면 **다음 순회부터 바로** 새 모델이 돕니다.
    * **침수** — 파이프라인이 기동 시 설정을 읽어 모델을 물고 있습니다.
      바꾸려면 재기동이 필요합니다.
    * **인파** — 분석기가 추적 상태(ByteTrack)를 들고 있어, 도중에 모델을
      바꾸면 추적 id 가 뒤엉킵니다. 재기동이 필요합니다.

    이 차이를 숨기고 「저장했습니다」라고만 하면 **바뀐 줄 알고 관제하게**
    됩니다. 그건 지금까지 걷어내 온 종류의 결함이라, 화면에 그대로 적습니다.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import model_registry as registry
from . import settings as S

log = logging.getLogger("urbanguard.model_ops")

# 저장 즉시 반영되는 도메인. 나머지는 재기동해야 한다.
LIVE_APPLY = {"road"}

APPLY_NOTE = {
    "road": "저장 즉시 다음 순회부터 반영됩니다. 분석기 객체 하나를 상시 "
            "순회가 계속 쓰고 있어, 그 객체의 모델만 갈아 끼우면 됩니다.",
    "flood": "⚠ 저장은 됐지만 **서비스를 재기동해야** 실제로 바뀝니다. "
             "침수 파이프라인이 기동 시 모델을 물고 있습니다.",
    # ⚠️ traffic 키가 빠져 있었다(2026-08-22 전수점검) — routes_analytics.py
    #   가 이 dict 에서 못 찾으면 빈 문자열(note="")을 내려보내, 교통 모델을
    #   저장해도 반영 시점 안내가 한 줄도 안 나갔다. flood 와 같은 사정
    #   이다 — runner.py 의 PipelineRunner 가 기동 시 소스를 만들며 모델을
    #   물기 때문에 재기동이 필요하다(2026-08-22, runner.py._build_source
    #   에서 model_ops.selected_key("traffic") 을 실제로 읽도록 고친 뒤에도
    #   "다음 접속부터"가 아니라 "재기동 후"인 것은 그대로다).
    "traffic": "⚠ 저장은 됐지만 **서비스를 재기동해야** 실제로 바뀝니다. "
               "교통위험 판정이 기동 시 검출 모델을 물고 있습니다.",
    "crowd": "개인정보 마스킹에는 **다음 마스킹부터 바로** 반영됩니다. "
             "⚠ 다만 **인파 계수(밀집도)는 바뀌지 않습니다** — 계수는 "
             "torchvision 검출기를 아키텍처 이름으로 쓰고 있어 파일 경로로 "
             "고르는 구조가 아닙니다.",
}


def selected_key(domain: str, db: Session | None = None) -> str:
    """이 도메인의 운영 모델 키. 지정하지 않았으면 코드 기본값."""
    key = S.MODEL_KEYS.get(domain)
    if key is None:
        return ""
    chosen = (S.get(key, db) or "").strip()
    if chosen:
        return chosen
    default = registry.default_for(domain)
    return default.key if default else ""


def selected(domain: str, db: Session | None = None) -> registry.ModelInfo | None:
    key = selected_key(domain, db)
    return registry.get(key) if key else None


def note(domain: str, db: Session | None = None) -> str:
    key = S.MODEL_NOTE_KEYS.get(domain, "")
    return S.get(key, db) if key else ""


def choose(db: Session, domain: str, model_key: str) -> dict:
    """운영 모델을 정한다. 무엇이 어떻게 반영됐는지 돌려준다.

    반환값의 ``applied`` 는 **지금 실제로 바뀌었는가**다. ``False`` 면 저장만
    된 것이고 재기동해야 한다 — 호출부는 이 값을 그대로 화면에 옮겨야 한다.
    """
    if domain not in S.MODEL_KEYS:
        raise ValueError(f"알 수 없는 도메인입니다: {domain}")
    info = registry.get(model_key)
    if info is None:
        raise LookupError(f"모델을 찾을 수 없습니다: {model_key}")
    if not info.exists:
        raise FileNotFoundError(f"모델 파일이 없습니다: {info.key}")
    # ⚠️ 화면 드롭다운은 도메인별로 걸러서 보여주지만, 그건 정상 조작을
    #   편하게 하는 것일 뿐 방어선이 아니다 — 폼 값을 조작하면(또는 API를
    #   직접 부르면) 노면 모델을 domain="flood" 로 보내 저장할 수 있었다
    #   (2026-08-22 전수점검, 서버측 검증 부재). 분류 미상("")은 어느
    #   도메인에도 쓸 수 있으므로 통과시킨다.
    if info.domain not in (domain, ""):
        raise ValueError(f"이 모델은 「{info.domain}」 전용입니다 — "
                         f"「{domain}」 도메인에는 쓸 수 없습니다: {info.key}")

    before = S.set_value(db, S.MODEL_KEYS[domain], info.key)
    db.commit()

    applied = False
    if domain in LIVE_APPLY:
        applied = _apply_now(domain, info.key)
    if domain == "crowd":
        # 마스킹 모델을 다시 고르게 한다. 캐시를 비우지 않으면 예전 모델이
        # 프로세스가 살아 있는 동안 계속 쓰인다.
        try:
            from . import image_mask
            image_mask.reset_model_cache()
        except Exception:  # noqa: BLE001
            log.exception("마스킹 모델 캐시 비우기 실패")
    # 설정이 바뀌었음을 상시 워처에 알린다 — 다음 바퀴에서 다시 읽는다.
    try:
        from . import config_rev
        from . import config_rev_bridge
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
    except Exception:  # noqa: BLE001
        log.exception("설정 변경 신호 발신 실패")

    return {"domain": domain, "model": info.to_dict(), "before": before or "",
            "applied": applied, "note": APPLY_NOTE.get(domain, "")}


def set_note(db: Session, domain: str, text: str) -> str:
    """모델 비고를 고친다. 사람이 내린 판단을 남기는 자리다."""
    key = S.MODEL_NOTE_KEYS.get(domain)
    if key is None:
        raise ValueError(f"알 수 없는 도메인입니다: {domain}")
    before = S.set_value(db, key, (text or "").strip()[:500])
    db.commit()
    return before or ""


def _apply_now(domain: str, model_key: str) -> bool:
    """돌고 있는 분석기에 새 모델을 꽂는다. 노면만 가능하다."""
    if domain != "road":
        return False
    try:
        from ..service import main as service_main
        analyzer = getattr(service_main, "_road_analyzer", None)
        if analyzer is None or not hasattr(analyzer, "set_model"):
            return False
        analyzer.set_model(model_key)
        return True
    except Exception:  # noqa: BLE001
        # 반영에 실패하면 **성공했다고 말하지 않는다.** 저장은 남으므로
        # 재기동하면 적용된다.
        log.exception("노면 모델 즉시 반영 실패 model=%s", model_key)
        return False


def prime_from_settings() -> None:
    """기동 시 저장된 운영 모델을 분석기에 적용한다.

    이게 없으면 화면에서 고른 모델이 **재기동 후 원래대로 돌아갑니다** —
    설정은 남아 있는데 코드 기본값으로 돌아가는, 가장 헷갈리는 상태입니다.
    """
    try:
        from .db import get_session
        db = get_session()
        try:
            S.load_all(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        log.warning("운영 모델 설정 로드 실패: %s", str(e)[:120])
        return

    chosen = (S.get(S.KEY_MODEL_ROAD) or "").strip()
    if not chosen:
        return
    info = registry.get(chosen)
    if info is None or not info.exists:
        log.warning("지정된 노면 모델을 쓸 수 없습니다(파일 없음): %s", chosen)
        return
    if _apply_now("road", chosen):
        log.info("노면 운영 모델 적용: %s", chosen)
