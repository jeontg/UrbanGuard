# -*- coding: utf-8 -*-
"""제외 목록(카메라)에 WHEP 주소를 내려주면 안 된다 — 2026-08-29 실사용
중 발견.

## 왜 이 시험이 있나

화면(침수 실시간 관제)에서 「올림픽교차로」(``restream.excluded_ids``에
있는 카메라, Wowza 계열 원본이 재배포와 근본적으로 안 맞아
``add_path()``가 애초에 MediaMTX에 등록하지 않는다 — `core/restream.py::
is_excluded()` 참고) 실시간 뷰를 열었더니 "WHEP 재생 오류: WHEP 서버
응답 오류: 400"만 뜨고 영상이 안 보였다.

원인은 ``service/runner.py::_restream_whep_url()``·``service/main.py::
_restream_whep_url_for()`` 둘 다 ``restream_enabled()``·
``mediamtx_healthy()``만 확인하고 **``is_excluded()``를 확인하지
않았던 것**이다 — 제외 목록 카메라는 MediaMTX에 경로가 없으니 WHEP
협상이 항상 400으로 실패하는데, 화면은 whepUrl이 내려온 이상 hls.js로
자연히 못 떨어지고(``app.js::openLive()`` 참고) WHEP 오류만 보여준다.

이 시험은 두 함수 모두 제외 목록 카메라에는 ``None``을 돌려주는지
고정한다(``None``이면 ``openLive()``가 기존 hls.js 경로로 떨어진다).
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import restream as RS
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session

CID = "TEST-WHEP-EXCLUDED"


@pytest.fixture(autouse=True)
def _clean(db_schema):
    RS.clear_health_cache()
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_excluded_ids(db, [CID])
        # ⚠️ public_host를 다른 시험 파일(예: test_restream.py의 우선순위
        # 시험)이 DB에 남겨 뒀을 수 있다 — request_host 반영을 확인하는
        # 이 파일의 시험이 그 잔재 때문에 거짓 실패하지 않도록 기본값으로
        # 되돌린다.
        S.set_restream_hosts_ports(db, host="127.0.0.1", public_host="",
                                   rtsp_port="8554", whep_port="8889",
                                   api_port="9997")
        db.commit()
    finally:
        db.close()
    # ⚠️ 여기서 S.invalidate()를 부르면 안 된다 — `set_*()`가 이미 캐시에
    # 값을 넣어 뒀는데(``settings.py::set_value``), invalidate로 비우면
    # `runner.py::_restream_whep_url()`처럼 **db 없이** 캐시만 보는
    # 호출부(백그라운드 스레드용, request 세션이 없다)가 DEFAULTS(꺼짐)로
    # 떨어져 이 시험이 실패한다(실측 발견) — db를 항상 넘기는 호출부만
    # 쓰는 ``test_restream.py``의 기존 관례를 그대로 베꼈다가 걸린 함정.
    yield
    RS.clear_health_cache()
    S.invalidate()


def test_runner_모듈은_제외_카메라에_whep_url을_내려주지_않는다(monkeypatch):
    from tot_dashboard.service import runner as R

    # mediamtx_healthy를 True로 고정 — "재배포 서버 자체는 멀쩡한데 이
    # 카메라만 제외 목록"인 실제 신고 상황을 재현한다.
    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    assert R._restream_whep_url(CID) is None


def test_runner_모듈은_제외_아닌_카메라에는_평소처럼_whep_url을_내려준다(monkeypatch):
    """회귀 방지 — 제외 목록 확인을 넣으며 정상 카메라의 동작이 깨지면
    안 된다."""
    from tot_dashboard.service import runner as R

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    url = R._restream_whep_url("TEST-WHEP-NOT-EXCLUDED")
    assert url is not None
    assert "TEST-WHEP-NOT-EXCLUDED" in url


def test_main_모듈은_제외_카메라에_whep_url을_내려주지_않는다(monkeypatch):
    from tot_dashboard.service import main as M

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    db = get_session()
    try:
        assert M._restream_whep_url_for(CID, db) is None
    finally:
        db.close()


def test_main_모듈은_제외_아닌_카메라에는_평소처럼_whep_url을_내려준다(monkeypatch):
    from tot_dashboard.service import main as M

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    db = get_session()
    try:
        url = M._restream_whep_url_for("TEST-WHEP-NOT-EXCLUDED", db)
        assert url is not None
        assert "TEST-WHEP-NOT-EXCLUDED" in url
    finally:
        db.close()


# --- request_host 전달 (2026-08-29, R-01) ------------------------------
#
# whep_url이 접속자와 무관하게 항상 127.0.0.1로 조립돼 관제요원 PC에서
# 열면 반드시 실패하던 문제. `_restream_whep_url_for()`가 `request_host`
# 를 받아 `core/restream.py::resolved_whep_url()`까지 그대로 관통시키는지
# 고정한다.

def test_main_모듈은_request_host를_whep_url에_반영한다(monkeypatch):
    from tot_dashboard.service import main as M

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    db = get_session()
    try:
        url = M._restream_whep_url_for("TEST-WHEP-NOT-EXCLUDED", db,
                                       request_host="ug.example.internal")
        assert url == "http://ug.example.internal:8889/TEST-WHEP-NOT-EXCLUDED/whep"
    finally:
        db.close()


def test_main_모듈은_request_host가_없으면_기존처럼_동작한다(monkeypatch):
    """회귀 방지 — request_host 배선을 넣으며 기존(생략) 호출이 깨지면
    안 된다."""
    from tot_dashboard.service import main as M

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    db = get_session()
    try:
        url = M._restream_whep_url_for("TEST-WHEP-NOT-EXCLUDED", db)
        assert url == "http://127.0.0.1:8889/TEST-WHEP-NOT-EXCLUDED/whep"
    finally:
        db.close()
