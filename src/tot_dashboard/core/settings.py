"""운영 중 바뀌는 설정값 — DB에 저장하고 화면에서 고친다.

`configs/*.yaml` 은 배포 시점에 정해지는 값이고, 여기 있는 것은 **운영자가
화면에서 바꾸는 값**이다(기관명, 화면 테마 등). 설계서 5절 S-85.

환경변수는 초기값으로만 쓴다 — DB에 값이 들어오면 그쪽이 이긴다. 지자체마다
기관명이 다른데 코드를 고칠 수 없기 때문이다(P0-5).
"""
from __future__ import annotations

import json
import os
import re
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.config import PROJECT_ROOT
from .models import AppSetting

# 화면 테마 — 관제실은 24시간 근무라 어두운 배경이 기본이다.
KEY_ORG_NAME = "org_name"
KEY_BOARD_BG = "board_bg"
# 상단 가운데 문구. 기관마다 사업명이 달라 고정할 수 없다.
KEY_SOLUTION_NAME = "solution_name"
# 좌측 상단 로고. 빈 값이면 기본 UrbanGuard 로고를 쓴다.
KEY_LOGO_FILE = "logo_file"
# 노면 학습 데이터 자동 수집(Phase 3). 도로 영상을 디스크에 계속 쌓으므로
# **기본은 꺼짐**이다 — 켜는 것은 개인정보 검토를 거친 운영 판단이어야 한다.
KEY_ROAD_COLLECT = "road_collect"
# 도메인별 **운영 모델**. 빈 값이면 코드의 기본 경로를 쓴다(S-61).
# 값은 모델 레지스트리 키 = 모델 파일 경로다.
KEY_MODEL_FLOOD = "model.flood"
KEY_MODEL_TRAFFIC = "model.traffic"   # 2026-08-21 flood/traffic 도메인 분리
KEY_MODEL_CROWD = "model.crowd"
KEY_MODEL_ROAD = "model.road"
MODEL_KEYS = {"flood": KEY_MODEL_FLOOD, "traffic": KEY_MODEL_TRAFFIC,
              "crowd": KEY_MODEL_CROWD, "road": KEY_MODEL_ROAD}
# 모델 비고 — 「부산 CCTV 실사용 불가」처럼 **사람이 내린 판단**을 적는 자리다.
# 이런 판단은 파일이나 통계에서 자동으로 도출되지 않으므로 분리해 둔다.
MODEL_NOTE_KEYS = {d: f"model_note.{d}" for d in MODEL_KEYS}

# S-01 감시 지점 배경지도. **기본은 꺼짐(빈 값)** 이다.
#
# 켜면 화면이 타일 이미지를 **외부 서버에서 직접 내려받는다.** 관제망에서
# 외부 접속이 막혀 있으면 지도가 뜨지 않고(그때는 기존 배치 도식으로 되돌아간다),
# 허용돼 있다면 **보고 있는 지역이 타일 제공자에게 드러난다.** 어느 쪽이든
# 기관 정책 확인이 먼저다(발주처 질문 6번).
#
# 값은 XYZ 타일 주소 틀이다. 예)
#   OSM        https://tile.openstreetmap.org/{z}/{x}/{y}.png
#   VWorld     https://api.vworld.kr/req/wmts/1.0.0/<인증키>/Base/{z}/{y}/{x}.png
#   지자체 GIS  기관이 제공하는 WMTS 주소
# S-87 서버 운영. 운영체제와 감시(재기동) 방식은 **따로** 받는다 —
# OS 만으로는 재기동 방식이 정해지지 않는다(server_profile.py 머리말).
KEY_SERVER_OS = "server.os"
KEY_RESTART_STRATEGY = "server.restart_strategy"
KEY_SERVICE_NAME = "server.service_name"
# 감시 여부를 프로그램이 확인할 수 없는 경우(주로 윈도우 서비스), 관리자가
# 책임지고 확인했다는 표시. 기본은 꺼짐 — 확인 못 한 것을 확인한 척하지 않는다.
KEY_SUPERVISION_ACK = "server.supervision_ack"

# 화면 정렬. **자리가 계속 바뀌면 읽던 카드를 놓친다** — 위험도순은 위험한
# 지점을 위로 올려 주지만, 값이 흔들릴 때마다 카드가 뛰어다닌다. 관제 중에
# 읽고 있던 지점이 화면 밖으로 밀리는 것이 더 나쁘다고 보는 현장도 있어
# 고를 수 있게 한다.
# 침수 위험도(RiskEngine) 독립 알림 발동 등급 (2026-08-21 flood/traffic
# 도메인 분리로 신설). 예전에는 SOLAPI 알림이 교통·기상 판정에서만 나가서,
# 물이 차오르는데 정체가 심하지 않으면 알림이 안 가는 공백이 있었다.
#
# ⚠️ 기본값 4(「높음」)는 **개발사 판단**이며 부산 실환경에서 검증되지
# 않았다 — 재난 담당부서 협의로 조정해야 한다. 그래서 코드에 박아 두지
# 않고 화면(S-95, /settings/levels)에서 관리자가 바꿀 수 있게 뺐다.
KEY_FLOOD_NOTIFY_MIN_GRADE = "flood.notify_min_grade"

# ★ 2026-08-26 — 교통·인파·노면도 같은 방식으로 관리자가 조정할 수 있게
# 뺐다. 세 도메인의 「경보 발동」 방식이 서로 다르다는 점에 주의:
#   - 교통: 이미 SOLAPI **자동 알림**이 있다(RiskDecisionAgent). 발동
#     심각도(0~3)를 조정한다.
#   - 인파·노면: 자동 SOLAPI 알림이 **없다**(승인 큐를 통한 수동 발송뿐).
#     대신 「관제 화면에 이벤트로 올릴지」의 문턱이 실질적인 경보 자리다.
#     그래서 이벤트 생성 최소 심각도/등급을 조정한다.
# 기본값은 전부 **지금까지 코드에 박혀 있던 값 그대로**다 — 화면을 만들었을
# 뿐 아무것도 바꾸지 않았다.
KEY_TRAFFIC_NOTIFY_MIN_SEVERITY = "traffic.notify_min_severity"
KEY_CROWD_DENSITY_MIN_SEVERITY = "crowd.density_event_min_severity"
KEY_ROAD_EVENT_MIN_GRADE = "road.event_min_grade"

# 강수(rainfall) 데이터 출처 (2026-08-27 신설, 강서구청 실측 요청).
#
# ⚠️ 카메라별 `rainfall` 설정(`blocks.json`/카메라 config JSON의 "type" 키,
# `rainfall_provider.build_rainfall()` 참고)이 있으면 **그 값이 항상 이긴다**
# — 이 설정은 카메라가 아무것도 지정하지 않았을 때만 쓰는 **전역 기본값**이다.
# 지금까지 카메라 39개소 중 명시적으로 "kma"를 지정한 곳이 없어, 이 설정
# 하나로 전체를 한 번에 바꿀 수 있다.
#
# 기본값은 지금까지의 동작(sine 합성값) 그대로다 — 관리자가 켜야만 바뀐다.
# `kma`로 바꿔도 `.env`의 `KMA_SERVICE_KEY`가 없으면 `build_rainfall()`이
# 조용히 sine으로 되돌아간다(rainfall_provider.py:156-160) — 서비스가
# 멈추는 것보다 합성값으로라도 도는 편이 안전하다는 이 프로젝트의 반복된
# 설계(fail-open)를 그대로 따른다.
KEY_RAINFALL_BACKEND = "rainfall.backend"

# 인파 상시 카메라별 모니터링 — 사람 검출 소스 (2026-08-27 신설).
#
# ⚠️ **카메라별 `crowd.source.type` 설정이 있으면 항상 그 값이 이긴다** —
# 이 설정은 카메라가 아무것도 지정하지 않았을 때만 쓰는 전역 기본값이다
# (`service/continuous.py::CrowdContinuousWatcher._build_analyzer()` 참고).
#
# ⚠️ 기본값을 **`detector`(실측)** 로 둔 이유 — 이 기능 자체가 "교통위험처럼
# 카메라별 실제 CCTV 분석"을 하려고 만든 것이라, 기본이 `mock`(합성값)이면
# 카메라를 켜도 가짜 데이터만 나와 기능의 목적이 성립하지 않는다.
#
# ⚠️ **CPU 부담이 실측되지 않았다** — 사람 검출은 화면을 2×2로 나눠 각각
# 추론하는 방식이라(FasterRCNN 타일 추론) 프레임당 약 1.6초가 걸린다
# (`crowd/live_analyzer.py::DetectorCrowdSource` 문서 참고). 그래서 이
# 기능은 **카메라 전부를 기본 꺼짐으로 배포**하고(S-80에서 관리자가 하나씩
# 켜며 CPU를 관찰하는 식), 만약 실측 결과 감당이 안 되면 이 스위치 하나로
# 전체를 합성값(mock)으로 즉시 되돌릴 수 있게 뒀다 — 코드를 고치거나
# 카메라 39개소를 하나씩 되돌릴 필요가 없다.
KEY_CROWD_CONTINUOUS_SOURCE = "crowd.continuous_source"

# 서비스별 PyTorch/NumPy/OpenCV 스레드 상한 (2026-09-02 신설, 속도 개선
# 2단계 — 처음엔 5개 서비스에 똑같이 적용되는 값 하나였으나, 사용자
# 요청으로 **서비스마다 다른 값**을 지정할 수 있게 다시 설계했다). 값은
# `{서비스 키: 정수}` 딕셔너리를 JSON 문자열로 인코딩한 것 — 키가 없는
# 서비스는 "자동"(코어 수 ÷ 4, `scripts/serve.py::_DEFAULT_TORCH_THREADS`
# 와 같은 공식)을 쓴다. 이 서버는 5개 서비스가 전부 별도 프로세스로
# 뜨는데, 아무 제한도 없으면 각자 "코어 전부 쓰겠다"고 나서 실제
# 연산량과 무관한 스케줄링 경합만 늘어난다(2026-09-02 실측, CPU 100%
# 포화). 배포 현장마다 서버 사양(코어 수)과 도메인별 무게(침수·교통위험은
# 무겁고 인파관리는 mock이라 가벼운 식)가 다르므로, 자동 계산값을
# 서비스별로 따로 조정할 수 있게 뺐다.
#
# ⚠️ 이 값을 실제로 쓰는 `scripts/serve.py`는 자기 머리말에 "의존성
# 없이 단독 실행돼야 한다(패키지가 깨져도 서비스를 띄워야 한다)"고
# 명시돼 있어 이 모듈(SQLAlchemy·Postgres 연결)을 임포트할 수 없다.
# 그래서 `set_service_thread_overrides()`가 DB뿐 아니라
# `_SERVICE_THREADS_FILE`에도 순수 JSON으로 값을 함께 적어 둔다 —
# `configs/blocks.json`이 이미 DB 비의존 스크립트들의 설정 소스로
# 쓰이는 것과 같은 원칙. **이 파일 경로는 `scripts/serve.py`의 경로
# 계산과 반드시 같아야 한다** — serve.py는 이 모듈을 못 불러오므로
# 경로를 그쪽에도 복제해 뒀다(`RESTART_EXIT_CODE`가 이미 이런 식으로
# 두 파일에 복제돼 있는 것과 같은 전례 — 한쪽을 고치면 다른 쪽도
# 고쳐야 한다). serve.py는 `--label`(예: `serve-crowd`)에서 서비스
# 키를 뽑아 자기 몫만 찾아 쓴다.
KEY_SERVICE_THREADS = "perf.service_threads"
_SERVICE_THREADS_FILE = PROJECT_ROOT / "data" / "config" / "perf.json"

# 관리자가 서비스 관리(`/admin/services`) 화면에서 "정지"시킨 도메인
# 서비스 키의 콤마 구분 목록(예: "traffic,flood") — 2026-09-02 신설.
#
# ⚠️ 왜 필요한가(실사용 중 자체 발견) — 정지/시작은 그 순간의 OS
# 프로세스 상태만 바꿀 뿐, "관리자가 원하는 최종 상태"를 어디에도
# 남기지 않았다. 그래서 `urbanguard-service.ps1 -Action start/restart`의
# "전체 서비스 확인"(`ensure-*.ps1`) 단계가 관리자의 의도와 무관하게
# 정지시킨 서비스를 도로 켜버리는 사고가 실제로 있었다(2026-09-02,
# 교통위험 서비스). 위 KEY_TORCH_THREADS와 정확히 같은 문제(DB 설정을
# DB에 의존할 수 없는 PowerShell 스크립트에 전달)라, 같은 해법을 쓴다 —
# `set_admin_stopped_services()`가 DB뿐 아니라 `_SERVICE_STATE_FILE`에도
# 순수 JSON으로 함께 적어 두고, `scripts/ensure-{key}-service.ps1`
# 4개가 시작 전에 그 파일을 확인한다.
KEY_SERVICE_ADMIN_STOPPED = "service.admin_stopped"
_SERVICE_STATE_FILE = PROJECT_ROOT / "data" / "config" / "service_state.json"

# 이벤트 자동 보류 정책(2026-09-02 신설) — 재탐지(임계등급 이상)가
# `KEY_EVENT_AUTO_HOLD_HOURS` 시간 넘게 없는 미해결 이벤트를 "자동
# 보류"(종결이 아님, `core/events.py::AUTO_HELD` 참고)로 옮긴다.
# 사용자 결정(2026-09-02): 기본 켜짐·24시간, 이벤트 관리 화면(S-02)에서
# 관리자가 직접 값을 바꿀 수 있어야 한다.
KEY_EVENT_AUTO_HOLD_ENABLED = "event.auto_hold_enabled"
KEY_EVENT_AUTO_HOLD_HOURS = "event.auto_hold_hours"

# 인파 검출 타일 격자 — 2026-08-29 신설(「4대탐지기능 성능개선 로드맵」
# 1단계). 화면을 몇×몇으로 나눠 각각 추론할지. 격자를 세분화할수록
# 원경의 작은 인물을 더 잡아내지만(실측: 2×2로 데모 검출률이 21%→100%),
# 타일 수만큼 추론 횟수가 늘어 CPU 부담도 커진다 — 3×3은 2×2 대비 약
# 2.25배, 4×4는 약 4배. 그래서 기본값은 기존 그대로 2×2로 두고(말없이
# 부담을 늘리지 않는다), 관리자가 CPU 여유를 보며 올릴 수 있게 전역
# 설정 하나로 뺐다(crowd_continuous_source와 같은 원칙 — 카메라 39개소
# JSON을 하나씩 고치지 않아도 된다).
KEY_CROWD_TILE_GRID = "crowd.tile_grid"

# CCTV 재배포 허브(MediaMTX) — 2026-08-28 신설.
#
# 4개 탐지 서비스가 각자 원본 CCTV(부산 ITS·서울 TOPIS/spatic)에 개별
# 연결하면 같은 카메라에 세 번째 연결이 시도될 때 CCTV 서버가 거절한다
# (실측, `road/live_analyzer.py` 주석). 재배포 서버를 두면 원본에는 카메라당
# 연결 1개만 열고, 그 뒤에서 RTSP(탐지 서비스용)·WebRTC(관제요원 브라우저용)로
# 나눠 준다.
#
# ⚠️ **기본값은 꺼짐(0)이다** — 재배포 서버는 새로운 단일 장애점(SPOF)이다.
# 재배포 서버가 죽으면 그 카메라는 원본으로 자동 전환하지 않고 "관측 없음"
# 으로 정직하게 실패 표시한다(폴백 없음, 확정 정책) — 검증 전에 기본으로
# 켜 두면 재배포 서버 안정성 문제가 곧바로 전체 탐지 중단으로 번진다.
# 관리자가 §「실행 순서」의 단계적 검증을 거쳐 명시적으로 켠다.
KEY_RESTREAM_ENABLED = "restream.enabled"
# RTSP는 같은 서버 프로세스(탐지 파이프라인)가 소비하므로 127.0.0.1이 맞다.
KEY_RESTREAM_HOST = "restream.host"
# WHEP은 **관제요원의 브라우저**가 직접 접속한다 — 서버가 사내망 IP나
# 도메인으로 서비스되고 있으면 127.0.0.1은 브라우저 기준으로 무효하다.
# 빈 값이면 호출부가 요청의 접속 주소(request.url.hostname)를 그대로 쓴다.
KEY_RESTREAM_PUBLIC_HOST = "restream.public_host"
KEY_RESTREAM_RTSP_PORT = "restream.rtsp_port"
KEY_RESTREAM_WHEP_PORT = "restream.whep_port"
# Control API. 인증이 없는 API라 반드시 127.0.0.1에만 바인딩한다(운영 문서).
KEY_RESTREAM_API_PORT = "restream.api_port"

# ★ 2026-08-28 실기 검증 중 발견 — 카메라별 재배포 제외 목록.
#
# 부산시 ITS 원본 서버 2곳(BLOCK-CENTUMSTN·OLYMPIC, Wowza 계열)이 재생목록
# 세션을 1회용으로만 허용해, MediaMTX의 표준 HLS 폴링(같은 재생목록을
# 반복 조회)과 근본적으로 맞지 않는다 — 3분 이상 단독 관찰해도 단 한 번도
# 성공하지 못함을 디버그 로그로 확인했다. **MediaMTX 자체의 한계**이지
# 우리 코드 결함이 아니다. 이 값이 비어 있으면 (재배포가 켜져 있는 한)
# 전체 카메라가 재배포 대상이다 — 즉 지금까지의 "전역 켜짐/꺼짐" 하나뿐인
# 설계 그대로다. 이 목록에 오른 카메라만 예외로 원본 직결을 유지한다.
KEY_RESTREAM_EXCLUDED_IDS = "restream.excluded_ids"

# ★ 2026-08-29 위험 점검(R-03) — 등록된 33개 경로 중 실제로 어느
# 도메인이든 쓰는 것은 9개뿐인데, 나머지 24개(73%)도 24시간 상시로
# 원본 CCTV를 끌어와 하루 239GB가 유입됐다(실측). "상시 지정된 카메라
# 만 등록"하는 대안은 기각했다 — 노면 선택 분석 카메라의 whep_url이
# 여전히 내려가는데(도메인·상시 구분 없이) `app.js::openLive()`가
# WHEP 실패 시 hls.js로 폴백하지 않아, 등록을 좁히면 그 카메라들의
# 실시간 뷰가 깨진다(제외 목록 미확인 버그와 같은 실패 패턴이 다른
# 경로로 재발). 대신 등록 범위는 그대로 두고 `add_path()`가 MediaMTX에
# `sourceOnDemand`를 심어 **리더(RTSP 소비자·WHEP 시청자)가 없으면
# 원본 연결 자체를 끊는다** — 등록은 유지되니 선택 카메라의 실시간
# 뷰도 그대로 동작한다.
#
# 기본값은 꺼짐(0) — 이 저장소의 "새 SPOF·새 동작은 검증 전엔 안전측"
# 관례를 그대로 따른다(KEY_RESTREAM_ENABLED와 같은 이유).
KEY_RESTREAM_ON_DEMAND = "restream.on_demand"
# 원본에 새로 붙는 데 걸리는 시간의 상한. readTimeout(30s, strm4.
# spatic.go.kr류 원본이 세그먼트 하나에 4초 이상 걸린다는 실측 근거로
# 이미 30s로 올려 둔 전례)과 같은 여유를 준다 — MediaMTX 기본값(10s)은
# 이 원본들에겐 빠듯하다.
KEY_RESTREAM_ON_DEMAND_START_TIMEOUT_SEC = "restream.on_demand_start_timeout_sec"
# 리더가 사라진 뒤 원본 연결을 끊기까지 기다리는 시간. MediaMTX
# 기본값(10s)보다 넉넉히 둬, 리더가 짧게 끊겼다 다시 붙는 흔한 경우
# (예: YoloDetectionSource의 재연결 루프)마다 콜드스타트를 또 겪는
# 진동을 피한다.
KEY_RESTREAM_ON_DEMAND_CLOSE_AFTER_SEC = "restream.on_demand_close_after_sec"

# ★ 2026-08-29(같은 날 후속) — MediaMTX 자신의 HLS 디먹서가 일부
# 카메라의 영상을 간헐적으로 손상시키는 것을 실측으로 확인했다(같은
# 순간 원본 직결은 깨끗한데 MediaMTX 재배포만 손상됨, mediamtx.log의
# "initial delimiter not found" 경고 — MediaMTX 공식 GitHub 이슈
# #3088과 동일 증상, 최신 버전까지 수정 없음). ffmpeg(-c copy, 재인코딩
# 없음)가 원본을 대신 읽어 MediaMTX에 RTSP로 발행하면 이 손상이 사라짐을
# 실측 확인했다(core/ffmpeg_relay.py). 이 목록에 오른 카메라만 그렇게
# 처리한다 — 33개 전부에 상시 적용하면 R-03이 없앤 "24시간 상시 연결"
# 문제가 재발할 위험이 있어, 상시 AI 판정 대상처럼 어차피 24시간
# 연결이 유지되는 카메라부터 우선 적용을 권고한다(관리자가 직접 채움).
KEY_RESTREAM_RELAY_IDS = "restream.relay_ids"

# ★ 2026-08-30 — 상시 화질 감시(Video Quality Monitoring) 신설.
#
# 손상 카메라를 지금까지 발견한 경로는 전부 "사용자가 화면을 보다가
# 우연히 제보"였다(도봉지하차도 출구·SEOUL-363 둘 다). mediamtx.log의
# decode error 누적치는 물어봐야 나오는 로그일 뿐 상시 경보가 아니고,
# 기존 손상판정 휴리스틱(flood/water_segmentation.py::
# is_likely_corrupted_frame)은 깨끗한 프레임도 오탐하는 것이 실측으로
# 확인돼 신뢰할 수 없다. 재배포 경로(RTSP)에 짧게 붙어 ffmpeg 자신이
# 보고하는 실제 디코더 오류 문자열을 세는 별도의 저빈도 스캐너
# (core/video_quality.py)를 둔다 — 사람이 화면을 안 보고 있어도 손상을
# 스스로 재고 표시하게 하기 위해서다.
#
# 기본값은 꺼짐(0) — 이 저장소의 "새 동작은 검증 전엔 안전측" 관례
# (KEY_RESTREAM_ENABLED와 같은 이유). 뷰어 전용(on-demand) 카메라는
# 스캔이 주기적으로 원본 연결을 짧게 다시 열게 되므로(R-03이 줄인
# 자원과 트레이드오프), 관리자가 검증 후 명시적으로 켠다.
KEY_VIDEO_QUALITY_ENABLED = "video_quality.enabled"
# 전체 카메라를 한 바퀴 도는 주기(초). 너무 짧으면 뷰어 전용 카메라의
# 원본 재연결이 잦아지고, 너무 길면 손상을 늦게 알아챈다 — 배포 후
# 실측하며 조정 권고(계획 문서 §확인이 필요합니다).
KEY_VIDEO_QUALITY_SCAN_INTERVAL_SEC = "video_quality.scan_interval_sec"
# 표본(SAMPLE_SEC초) 안에서 디코더 오류가 이 값을 넘으면 "주의".
KEY_VIDEO_QUALITY_WARN_THRESHOLD = "video_quality.warn_threshold"
# 이 값을 넘으면 "손상 심각". SEOUL-363 재현 캡처에서 3초 표본 중에도
# 다수의 오류 줄이 한꺼번에 찍힌 것을 참고해 임계치를 낮게 잡았다.
KEY_VIDEO_QUALITY_CRIT_THRESHOLD = "video_quality.crit_threshold"

# S-88 증거 영상. **어느 등급부터 남길지 관리자가 정한다** — 영상은
# 개인정보라 「일단 다 남기고 보자」로 두면 안 된다.
KEY_EVIDENCE_LEVELS = "evidence.levels"
# 증거 영상 보존기간(개월). 0 이면 자동 파기하지 않는다.
#
# **기본값을 0으로 둔 이유** — 사람이 정하지 않았는데 시스템이 자료를 지우기
# 시작하면 안 된다. 대신 화면이 「자동 파기하지 않는 중」이라고 계속 알린다.
# 개인정보 보유기간은 기관 규정 사항이라 우리가 정할 수 없다.
KEY_EVIDENCE_RETENTION_MONTHS = "evidence.retention_months"

KEY_CARD_ORDER = "board.card_order"
KEY_OBJECT_ORDER = "board.object_order"

KEY_MAP_TILE_URL = "map.tile_url"
KEY_MAP_ATTRIBUTION = "map.attribution"
KEY_MAP_MAX_ZOOM = "map.max_zoom"

DEFAULTS = {
    KEY_ORG_NAME: os.environ.get("URBANGUARD_ORG_NAME", "부산광역시"),
    KEY_BOARD_BG: "#0F1420",
    KEY_SOLUTION_NAME: os.environ.get("URBANGUARD_SOLUTION_NAME",
                                      "통합 도시안전 관제"),
    KEY_LOGO_FILE: "",
    KEY_ROAD_COLLECT: os.environ.get("URBANGUARD_ROAD_COLLECT", "off"),
    KEY_MODEL_FLOOD: "", KEY_MODEL_TRAFFIC: "", KEY_MODEL_CROWD: "",
    KEY_MODEL_ROAD: "",
    # 서버 운영. 기본은 「자동 감지 + 감시 스크립트」 — 지금 실제로 쓰는
    # 방식이다. 납품 시 systemd·윈도우 서비스로 바꾼다.
    KEY_SERVER_OS: os.environ.get("URBANGUARD_SERVER_OS", "auto"),
    KEY_RESTART_STRATEGY: os.environ.get("URBANGUARD_RESTART_STRATEGY",
                                         "supervisor"),
    KEY_SERVICE_NAME: os.environ.get("URBANGUARD_SERVICE_NAME", ""),
    KEY_SUPERVISION_ACK: "0",
    # 기본은 지금까지의 동작(위험도순·감속순)이다. 바꾸고 싶은 기관만 바꾼다.
    # 기본 4(「높음」) — 개발사 판단, 재난 담당부서 확인 전까지의 임시값.
    KEY_FLOOD_NOTIFY_MIN_GRADE: os.environ.get(
        "URBANGUARD_FLOOD_NOTIFY_MIN_GRADE", "4"),
    # 기본값은 전부 예전에 코드에 박혀 있던 값 그대로다(§ 위 주석 참고).
    KEY_TRAFFIC_NOTIFY_MIN_SEVERITY: os.environ.get(
        "URBANGUARD_TRAFFIC_NOTIFY_MIN_SEVERITY", "2"),
    KEY_CROWD_DENSITY_MIN_SEVERITY: os.environ.get(
        "URBANGUARD_CROWD_DENSITY_MIN_SEVERITY", "3"),
    KEY_ROAD_EVENT_MIN_GRADE: os.environ.get(
        "URBANGUARD_ROAD_EVENT_MIN_GRADE", "1"),
    # 기본은 sine(합성) — 실측(kma)은 관리자가 명시적으로 켠다.
    KEY_RAINFALL_BACKEND: os.environ.get("URBANGUARD_RAINFALL_BACKEND", "sine"),
    # 기본은 detector(실측) — 이 기능의 목적 자체가 실측이라, mock은
    # CPU 부담이 감당 안 될 때 관리자가 되돌리는 비상 전환용이다.
    KEY_CROWD_CONTINUOUS_SOURCE: os.environ.get(
        "URBANGUARD_CROWD_CONTINUOUS_SOURCE", "detector"),
    # 기본은 빈 JSON 객체(모든 서비스 "자동") — 위 KEY_SERVICE_THREADS 주석 참고.
    KEY_SERVICE_THREADS: os.environ.get("URBANGUARD_SERVICE_THREADS", "{}"),
    # 기본은 빈 목록(아무 서비스도 관리자가 정지시키지 않은 상태) — 위
    # KEY_SERVICE_ADMIN_STOPPED 주석 참고.
    KEY_SERVICE_ADMIN_STOPPED: "",
    # 기본 켜짐·24시간(2026-09-02 사용자 결정) — 위 KEY_EVENT_AUTO_HOLD_*
    # 주석 참고.
    KEY_EVENT_AUTO_HOLD_ENABLED: os.environ.get(
        "URBANGUARD_EVENT_AUTO_HOLD_ENABLED", "1"),
    KEY_EVENT_AUTO_HOLD_HOURS: os.environ.get(
        "URBANGUARD_EVENT_AUTO_HOLD_HOURS", "24"),
    # 기본 2×2 — 위 KEY_CROWD_TILE_GRID 주석 참고(CPU 부담을 말없이
    # 늘리지 않기 위해 기존 그대로).
    KEY_CROWD_TILE_GRID: os.environ.get("URBANGUARD_CROWD_TILE_GRID", "2x2"),
    # 기본 꺼짐(0) — 위 KEY_RESTREAM_ENABLED 주석 참고.
    KEY_RESTREAM_ENABLED: os.environ.get("URBANGUARD_RESTREAM_ENABLED", "0"),
    KEY_RESTREAM_HOST: os.environ.get("URBANGUARD_RESTREAM_HOST", "127.0.0.1"),
    KEY_RESTREAM_PUBLIC_HOST: os.environ.get("URBANGUARD_RESTREAM_PUBLIC_HOST", ""),
    KEY_RESTREAM_RTSP_PORT: os.environ.get("URBANGUARD_RESTREAM_RTSP_PORT", "8554"),
    KEY_RESTREAM_WHEP_PORT: os.environ.get("URBANGUARD_RESTREAM_WHEP_PORT", "8889"),
    KEY_RESTREAM_API_PORT: os.environ.get("URBANGUARD_RESTREAM_API_PORT", "9997"),
    # 기본은 빈 목록 — 전역 켜짐/꺼짐 하나뿐이던 기존 동작 그대로다.
    KEY_RESTREAM_EXCLUDED_IDS: os.environ.get("URBANGUARD_RESTREAM_EXCLUDED_IDS", ""),
    # 기본 꺼짐 — 위 KEY_RESTREAM_ON_DEMAND 주석 참고.
    KEY_RESTREAM_ON_DEMAND: os.environ.get("URBANGUARD_RESTREAM_ON_DEMAND", "0"),
    KEY_RESTREAM_ON_DEMAND_START_TIMEOUT_SEC: os.environ.get(
        "URBANGUARD_RESTREAM_ON_DEMAND_START_TIMEOUT_SEC", "30"),
    KEY_RESTREAM_ON_DEMAND_CLOSE_AFTER_SEC: os.environ.get(
        "URBANGUARD_RESTREAM_ON_DEMAND_CLOSE_AFTER_SEC", "60"),
    # 기본은 빈 목록 — 관리자가 손상 확인된 카메라를 직접 채운다.
    KEY_RESTREAM_RELAY_IDS: os.environ.get("URBANGUARD_RESTREAM_RELAY_IDS", ""),
    # 기본 꺼짐 — 위 KEY_VIDEO_QUALITY_ENABLED 주석 참고.
    KEY_VIDEO_QUALITY_ENABLED: os.environ.get("URBANGUARD_VIDEO_QUALITY_ENABLED", "0"),
    KEY_VIDEO_QUALITY_SCAN_INTERVAL_SEC: os.environ.get(
        "URBANGUARD_VIDEO_QUALITY_SCAN_INTERVAL_SEC", "900"),
    KEY_VIDEO_QUALITY_WARN_THRESHOLD: os.environ.get(
        "URBANGUARD_VIDEO_QUALITY_WARN_THRESHOLD", "3"),
    KEY_VIDEO_QUALITY_CRIT_THRESHOLD: os.environ.get(
        "URBANGUARD_VIDEO_QUALITY_CRIT_THRESHOLD", "15"),
    # 기본은 「경계·심각」. 조치·보고가 실제로 필요한 등급만 남긴다 —
    # 「주의」는 평상시에도 자주 떠서 쓰이지 않는 영상이 쌓인다.
    KEY_EVIDENCE_LEVELS: os.environ.get("URBANGUARD_EVIDENCE_LEVELS",
                                        "경계,심각"),
    KEY_EVIDENCE_RETENTION_MONTHS: os.environ.get(
        "URBANGUARD_EVIDENCE_RETENTION_MONTHS", "0"),
    KEY_CARD_ORDER: "severity",
    KEY_OBJECT_ORDER: "drop",
    # 배경지도는 기본 꺼짐 — 망분리 환경에서 켜 두면 매번 실패한다.
    KEY_MAP_TILE_URL: os.environ.get("URBANGUARD_MAP_TILE_URL", ""),
    KEY_MAP_ATTRIBUTION: os.environ.get("URBANGUARD_MAP_ATTRIBUTION", ""),
    KEY_MAP_MAX_ZOOM: os.environ.get("URBANGUARD_MAP_MAX_ZOOM", "18"),
    # 초기 비고는 지금까지 확인된 사실이다. 화면에서 고칠 수 있다.
    MODEL_NOTE_KEYS["flood"]: "val F1 0.9392 · 부산 실환경 미검증",
    # 교통은 2026-08-21 분리 신설 — 아직 전용 모델을 검증한 적이 없다.
    # 근거 없는 성능 수치를 미리 적지 않는다.
    MODEL_NOTE_KEYS["traffic"]: "미검증 — 전용 모델 선정 전",
    MODEL_NOTE_KEYS["crowd"]: "SAM3와 동일 인원수 확인 · 기본값은 mock",
    MODEL_NOTE_KEYS["road"]: "⚠ 부산 CCTV(240p)에서 차선 도색을 균열로 읽는 "
                             "오탐 확인 (2026-08-13). 점검 지점을 좁히는 참고 "
                             "자료로만 쓸 것",
}

# 미리 준비한 배경. 전부 어두운 색인 이유는 9-3절 명도 대비 기준을 만족시키면서
# 야간 관제 시 눈부심을 피하기 위해서다. 자유 입력도 허용하되 대비를 검사한다.
PRESETS = [
    ("#0F1420", "다크 네이비", "기본값 — 관제 표준"),
    ("#12161C", "차콜", "중립적인 무채색"),
    ("#0B1A2A", "딥 블루", "푸른 기운이 강함"),
    ("#141018", "딥 플럼", "붉은 기운이 약간"),
    ("#000000", "블랙", "대비 최대 · 번인 주의"),
]

def flood_notify_min_grade(db: Session | None = None) -> int:
    """침수 위험도 알림이 몇 등급(1~5)부터 나갈지. 관리자가 S-95 화면에서
    바꾼다 — 코드에 박아 두면 기관마다 다른 판단을 반영할 수 없다."""
    try:
        n = int(get(KEY_FLOOD_NOTIFY_MIN_GRADE, db))
    except (TypeError, ValueError):
        n = 4
    return min(max(n, 1), 5)


def set_flood_notify_min_grade(db: Session, grade) -> int:
    """저장. 범위 밖 값은 **가장 가까운 유효값으로 눕힌다** — 잘못된 값이
    들어와 알림이 아예 안 나가거나(6 이상) 항상 나가는(0 이하) 것보다는,
    화면이 보여 준 범위 안에서 가장 가까운 쪽으로 저장하는 편이 안전하다."""
    try:
        n = int(grade)
    except (TypeError, ValueError):
        n = 4
    n = min(max(n, 1), 5)
    set_value(db, KEY_FLOOD_NOTIFY_MIN_GRADE, str(n))
    return n


def _clamped_int(db: Session | None, key: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(get(key, db))
    except (TypeError, ValueError):
        n = default
    return min(max(n, lo), hi)


def traffic_notify_min_severity(db: Session | None = None) -> int:
    """교통위험 SOLAPI 알림이 몇 심각도(0~3, 관심·주의·경계·심각)부터
    나갈지. 예전에는 `RiskDecisionAgent(alert_min_severity=2)`로 코드에
    박혀 있었다."""
    return _clamped_int(db, KEY_TRAFFIC_NOTIFY_MIN_SEVERITY, 2, 0, 3)


def set_traffic_notify_min_severity(db: Session, severity) -> int:
    try:
        n = int(severity)
    except (TypeError, ValueError):
        n = 2
    n = min(max(n, 0), 3)
    set_value(db, KEY_TRAFFIC_NOTIFY_MIN_SEVERITY, str(n))
    return n


def crowd_density_min_severity(db: Session | None = None) -> int:
    """인파 밀집도 이벤트가 몇 심각도(0~4, crowd 고유 분류체계)부터 관제
    화면에 이벤트로 올라갈지. 인파는 자동 SOLAPI 알림이 없어(승인 큐를
    통한 수동 발송뿐), 이벤트 생성 문턱이 실질적인 경보 자리다.

    ⚠️ 예전에는 `_CROWD_SEVERITY_LEVEL = {3: "경계", 4: "심각"}`로 3 미만은
    전부 버렸다 — 실측(670회 중 93.6%가 심각도 2)에서 정한 값이다.

    ⚠️ **0·1(정상·군중밀집)을 골라도 실제로는 아무것도 안 바뀐다** —
    ``events.record_detection()``의 도메인 공통 문턱(``EVENT_THRESHOLD=
    "주의"``) 때문에, 이 심각도들이 옮겨 붙는 「관심」 수준은 원래도
    이벤트가 되지 않는다(실측 확인, 2026-08-26). 이 설정이 실제로
    여닫는 범위는 **2(이동흐름혼란)부터**다 — 다만 2는 실측상 매우
    자주 관측돼(93.6%) 이벤트가 폭주할 수 있다는 점을 화면에서 함께
    경고한다."""
    return _clamped_int(db, KEY_CROWD_DENSITY_MIN_SEVERITY, 3, 0, 4)


def set_crowd_density_min_severity(db: Session, severity) -> int:
    try:
        n = int(severity)
    except (TypeError, ValueError):
        n = 3
    n = min(max(n, 0), 4)
    set_value(db, KEY_CROWD_DENSITY_MIN_SEVERITY, str(n))
    return n


def road_event_min_grade(db: Session | None = None) -> int:
    """노면 손상이 몇 등급(1~4, 양호·관찰·보수 필요·긴급)부터 관제 화면에
    이벤트로 올라갈지. 노면도 자동 SOLAPI 알림이 없다.

    ⚠️ **1·2(양호·관찰)를 골라도 실제로는 아무것도 안 바뀐다.**
    ``events.record_detection()``이 도메인 공통 문턱(``EVENT_THRESHOLD=
    "주의"``)을 이미 갖고 있어, 등급 1·2가 옮겨 붙는 「관심」 수준은
    이 값과 무관하게 원래도 이벤트가 되지 않았다(실측 확인,
    2026-08-26). 이 설정이 실제로 여닫는 범위는 **3(보수 필요)·4(긴급)
    뿐**이다. 기본값 1은 「지금까지의 실제 동작」과 같다."""
    return _clamped_int(db, KEY_ROAD_EVENT_MIN_GRADE, 1, 1, 4)


def set_road_event_min_grade(db: Session, grade) -> int:
    try:
        n = int(grade)
    except (TypeError, ValueError):
        n = 1
    n = min(max(n, 1), 4)
    set_value(db, KEY_ROAD_EVENT_MIN_GRADE, str(n))
    return n


# 화면에서 고를 수 있는 강수 출처. 값(키)이 실제 저장값이고, 나머지는
# 설명문이다 — `set_rainfall_backend()`가 목록 밖 값을 걸러낼 때도 이 키를
# 그대로 쓴다.
RAINFALL_BACKENDS = {
    "sine": ("합성값 (기본)", "실제 기상 관측이 아닙니다 — 개발·시연용 사인파입니다. "
                              "카메라별로 「kma」를 지정하지 않은 모든 지점에 적용됩니다."),
    "kma": ("기상청 실측(KMA)", "공공데이터포털 「기상청_단기예보 조회서비스」(초단기실황)로 "
                                  "지점 좌표 기준 실제 강수량(mm/h)을 가져옵니다. "
                                  ".env 의 KMA_SERVICE_KEY 가 없으면 자동으로 합성값으로 "
                                  "되돌아갑니다."),
}


def rainfall_backend(db: Session | None = None) -> str:
    """강수 데이터 전역 기본 출처. 카메라별 명시 설정이 없을 때만 쓰인다
    (`service/runner.py::_build_block_ctx` 참고)."""
    v = get(KEY_RAINFALL_BACKEND, db)
    return v if v in RAINFALL_BACKENDS else "sine"


def set_rainfall_backend(db: Session, value) -> str:
    """저장. 목록 밖 값은 **sine(합성)으로 눕힌다** — 잘못된 값이 들어와
    조용히 KMA로 오인되는 것보다, 안전한 기본값으로 떨어지는 편이 낫다."""
    v = value if value in RAINFALL_BACKENDS else "sine"
    set_value(db, KEY_RAINFALL_BACKEND, v)
    return v


# 화면에서 고를 수 있는 인파 상시 카메라 검출 소스.
CROWD_CONTINUOUS_SOURCES = {
    "detector": ("실제 검출(기본)", "화면을 2×2로 나눠 각각 사람을 검출합니다(FasterRCNN "
                                    "타일 추론) — 프레임당 약 1.6초. 카메라별로 명시 "
                                    "설정하지 않은 모든 지점에 적용됩니다."),
    "mock": ("합성값", "실제 검출이 아닙니다 — CPU 부담이 감당 안 될 때 전체를 즉시 "
                        "되돌리는 비상 전환용입니다."),
}


def crowd_continuous_source(db: Session | None = None) -> str:
    """인파 상시 카메라별 모니터링의 전역 기본 검출 소스. 카메라별 명시
    설정이 없을 때만 쓰인다(`service/continuous.py::
    CrowdContinuousWatcher._build_analyzer` 참고)."""
    v = get(KEY_CROWD_CONTINUOUS_SOURCE, db)
    return v if v in CROWD_CONTINUOUS_SOURCES else "detector"


def set_crowd_continuous_source(db: Session, value) -> str:
    """저장. 목록 밖 값은 **detector(기본)로 눕힌다** — 잘못된 값이 조용히
    mock으로 떨어져 "켰는데 합성값만 나온다"고 오해하는 쪽보다 안전하다."""
    v = value if value in CROWD_CONTINUOUS_SOURCES else "detector"
    set_value(db, KEY_CROWD_CONTINUOUS_SOURCE, v)
    return v


def service_thread_overrides(db: Session | None = None) -> dict[str, int]:
    """서비스별 PyTorch/NumPy/OpenCV 스레드 상한 관리자 재정의값 —
    `{서비스 키: 정수}`. 키가 없는 서비스는 `scripts/serve.py`가 코어 수
    기반 자동값(코어 수 ÷ 4)을 쓴다. JSON이 깨졌거나 값이 숫자가
    아니거나 1 미만인 항목은 조용히 걸러낸다(fail-safe)."""
    v = get(KEY_SERVICE_THREADS, db)
    try:
        raw = json.loads(v) if v else {}
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, n in raw.items():
        try:
            n = int(n)
        except (ValueError, TypeError):
            continue
        if n >= 1:
            out[str(k)] = n
    return out


def set_service_thread_overrides(db: Session, overrides: dict[str, int]) -> dict[str, int]:
    """저장 + `data/config/perf.json` 미러 기록(위 KEY_SERVICE_THREADS
    주석 참고 — `scripts/serve.py`가 DB를 못 읽어서 필요하다).

    `overrides`에 남은 항목만 저장한다(자동으로 되돌릴 서비스는 호출자가
    미리 빼고 넘긴다 — `routes_server.py`가 그렇게 한다). 값이 1 미만인
    항목은 방어적으로 걸러낸다(fail-safe, 이상한 값으로 서비스가 스레드
    0개로 뜨는 사고보다 안전하다) — 화면 쪽 형식 검증은 `routes_server.py`
    가 먼저 하므로 여기까지 오는 값은 보통 이미 유효하다.
    """
    clean = {str(k): int(n) for k, n in overrides.items() if int(n) >= 1}
    set_value(db, KEY_SERVICE_THREADS, json.dumps(clean))
    try:
        _SERVICE_THREADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SERVICE_THREADS_FILE.write_text(json.dumps(clean), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        # 파일 기록이 실패해도 DB 저장은 이미 끝났다 — 화면은 정상
        # 동작해야 한다(fail-open). 다음 재기동에 serve.py가 이 파일을
        # 못 찾으면 그냥 자동값을 쓰므로 안전하게 실패한다.
        print(f"[settings] perf.json 기록 실패(무시): {str(e)[:120]}")
    return clean


def admin_stopped_services(db: Session | None = None) -> set[str]:
    """관리자가 서비스 관리 화면에서 "정지"시킨 도메인 서비스 키 집합.
    비어 있으면(기본) 아무것도 정지되지 않은 것 — 위 KEY_SERVICE_ADMIN_STOPPED
    주석 참고."""
    v = get(KEY_SERVICE_ADMIN_STOPPED, db)
    return {k for k in v.split(",") if k}


def set_admin_stopped_services(db: Session, keys: set[str]) -> str:
    """저장 + `data/config/service_state.json` 미러 기록(위
    KEY_SERVICE_ADMIN_STOPPED 주석 참고 — `ensure-*.ps1`이 DB를 못 읽어서
    필요하다). 어느 키를 도메인 서비스로 볼지는 이 모듈이 판단하지
    않는다(`STOPPABLE`은 `service/routes_services.py`가 정의) — 호출자가
    이미 걸러 준 집합을 그대로 저장한다."""
    v = ",".join(sorted(keys))
    set_value(db, KEY_SERVICE_ADMIN_STOPPED, v)
    try:
        _SERVICE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SERVICE_STATE_FILE.write_text(
            json.dumps({"stopped": sorted(keys)}), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        # DB 저장은 이미 끝났다 — 화면은 정상 동작해야 한다(fail-open).
        # 파일을 못 읽으면 ensure-*.ps1은 평소대로(정지 안 된 것처럼)
        # 진행하므로, 최악의 경우도 "관리자의 정지 의도가 재기동에서
        # 한 번 안 지켜지는 것"이지 서비스가 아예 안 뜨는 사고는 아니다.
        print(f"[settings] service_state.json 기록 실패(무시): {str(e)[:120]}")
    return v


def event_auto_hold_enabled(db: Session | None = None) -> bool:
    """이벤트 자동 보류 정책이 켜져 있는가. 기본 켜짐(2026-09-02 사용자
    결정) — 위 KEY_EVENT_AUTO_HOLD_ENABLED 주석 참고."""
    return get(KEY_EVENT_AUTO_HOLD_ENABLED, db) == "1"


def set_event_auto_hold_enabled(db: Session, enabled: bool) -> str:
    v = "1" if enabled else "0"
    set_value(db, KEY_EVENT_AUTO_HOLD_ENABLED, v)
    return v


def event_auto_hold_hours(db: Session | None = None) -> float:
    """재탐지 없이 몇 시간 지나면 자동 보류로 옮길지. 숫자가 아니거나
    1 미만이면 기본값 24로 눕힌다(fail-safe)."""
    v = get(KEY_EVENT_AUTO_HOLD_HOURS, db)
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 24.0
    return n if n >= 1 else 24.0


def set_event_auto_hold_hours(db: Session, hours: str) -> float:
    """저장. 화면 쪽 검증(`routes_events.py`)이 먼저 하므로 여기까지
    오는 값은 보통 이미 유효하다 — 그래도 방어적으로 한 번 더 걸러낸다
    (0 이하·숫자 아님은 24로 눕힌다, 이상한 값으로 이벤트가 즉시
    전부 보류되는 사고보다 안전하다)."""
    try:
        n = float(hours)
    except (TypeError, ValueError):
        n = 0
    v = n if n >= 1 else 24.0
    set_value(db, KEY_EVENT_AUTO_HOLD_HOURS, str(v))
    return v


# 화면에서 고를 수 있는 인파 타일 격자 — 2026-08-29 신설.
# 값은 "가로x세로" 문자열(예: "3x3")이다. 세분화할수록 원경의 작은
# 인물을 더 잡지만(2×2 데모 실측: 검출률 21%→100%, 소요 0.2초→1.6초),
# 타일 수만큼 추론 횟수가 늘어 CPU 부담도 커진다.
CROWD_TILE_GRID_OPTIONS = {
    "2x2": ("2×2 (기본)", "프레임당 약 1.6초(데모 실측 기준) — 지금 이 값입니다."),
    "3x3": ("3×3", "타일 9개 — 2×2 대비 추론 횟수 약 2.25배. 더 먼 인물까지 "
                    "잡을 수 있으나 CPU 부담도 그만큼 커집니다. 검증 안 됨 — "
                    "실측 후 적용을 권장합니다."),
    "4x4": ("4×4", "타일 16개 — 2×2 대비 추론 횟수 약 4배. 가장 촘촘하지만 "
                    "CPU 부담이 가장 큽니다. 검증 안 됨."),
}


def _parse_tile_grid(v: str) -> tuple[int, int]:
    try:
        rows, cols = v.lower().split("x")
        return (int(rows), int(cols))
    except Exception:  # noqa: BLE001
        return (2, 2)


def crowd_tile_grid(db: Session | None = None) -> str:
    """인파 검출 타일 격자의 전역 기본값("AxB" 문자열). 카메라별 명시
    설정이 없을 때만 쓰인다(``service/continuous.py::
    CrowdContinuousWatcher._build_analyzer`` 참고)."""
    v = get(KEY_CROWD_TILE_GRID, db)
    return v if v in CROWD_TILE_GRID_OPTIONS else "2x2"


def crowd_tile_grid_tuple(db: Session | None = None) -> tuple[int, int]:
    """``(rows, cols)`` 튜플로. ``DetectorCrowdSource(tile_grid=...)``에
    바로 넘길 수 있는 형태다."""
    return _parse_tile_grid(crowd_tile_grid(db))


def set_crowd_tile_grid(db: Session, value) -> str:
    """저장. 목록 밖 값은 **2x2(기본)로 눕힌다** — crowd_continuous_source와
    같은 fail-safe 원칙."""
    v = value if value in CROWD_TILE_GRID_OPTIONS else "2x2"
    set_value(db, KEY_CROWD_TILE_GRID, v)
    return v


def restream_enabled(db: Session | None = None) -> bool:
    """CCTV 재배포 허브(MediaMTX)를 쓸지. 기본 꺼짐 — 위 KEY_RESTREAM_ENABLED
    주석의 이유(새 단일 장애점) 그대로."""
    return get(KEY_RESTREAM_ENABLED, db) == "1"


def set_restream_enabled(db: Session, value) -> bool:
    """저장. 참으로 해석되지 않는 값은 전부 **꺼짐(0)으로 눕힌다** — 재배포는
    새 SPOF이므로, 잘못된 값이 조용히 "켜짐"으로 오인되는 것보다 원본 직결을
    유지하는 쪽(꺼짐)이 안전하다."""
    v = "1" if value in (True, "1", "true", "True", 1) else "0"
    set_value(db, KEY_RESTREAM_ENABLED, v)
    return v == "1"


def restream_host(db: Session | None = None) -> str:
    """탐지 서비스(같은 서버 프로세스)가 RTSP로 붙을 주소."""
    return get(KEY_RESTREAM_HOST, db) or "127.0.0.1"


def restream_public_host(db: Session | None = None) -> str:
    """관제요원 브라우저가 WHEP으로 붙을 주소. 비어 있으면 호출부가 요청의
    접속 주소를 대신 써야 한다 — 여기서는 빈 문자열을 그대로 돌려준다."""
    return get(KEY_RESTREAM_PUBLIC_HOST, db) or ""


def restream_rtsp_port(db: Session | None = None) -> str:
    return get(KEY_RESTREAM_RTSP_PORT, db) or "8554"


def restream_whep_port(db: Session | None = None) -> str:
    return get(KEY_RESTREAM_WHEP_PORT, db) or "8889"


def restream_api_port(db: Session | None = None) -> str:
    return get(KEY_RESTREAM_API_PORT, db) or "9997"


def set_restream_hosts_ports(db: Session, *, host=None, public_host=None,
                             rtsp_port=None, whep_port=None, api_port=None) -> None:
    """호스트·포트 일괄 저장. 값이 비면 그 항목은 건드리지 않는다(부분 갱신)."""
    if host:
        set_value(db, KEY_RESTREAM_HOST, str(host))
    if public_host is not None:
        set_value(db, KEY_RESTREAM_PUBLIC_HOST, str(public_host))
    if rtsp_port:
        set_value(db, KEY_RESTREAM_RTSP_PORT, str(rtsp_port))
    if whep_port:
        set_value(db, KEY_RESTREAM_WHEP_PORT, str(whep_port))
    if api_port:
        set_value(db, KEY_RESTREAM_API_PORT, str(api_port))


def restream_excluded_ids(db: Session | None = None) -> set[str]:
    """재배포에서 제외할 카메라 ID 집합 — 원본 CCTV 서버 특성상 MediaMTX의
    HLS 폴링과 근본적으로 안 맞는 지점을 여기 올린다(위 KEY 주석 참고).
    화면과 파이프라인이 같은 값을 본다(evidence_levels와 같은 패턴)."""
    raw = get(KEY_RESTREAM_EXCLUDED_IDS, db)
    return {p.strip().upper() for p in (raw or "").split(",") if p.strip()}


# core/cameras.py::ID_RE 와 같은 형식이다. 여기서 다시 import 하면
# core/cameras.py 가 이미 core/settings.py 를 import 하고 있어 순환
# 참조가 되므로, 형식만 그대로 복제한다(값 자체가 바뀔 일이 거의 없다).
_CAMERA_ID_RE = re.compile(r"^[A-Z][A-Z0-9\-_]{2,63}$")


def set_restream_excluded_ids(db: Session, ids) -> set[str]:
    """저장. 카메라 ID 형식이 아닌 값은 버린다 — 화면은 카메라 목록에서만
    고르게 하므로, 형식이 다른 값이 왔다면 직접 만든 요청이다. 존재하지
    않는 ID가 섞여 있어도(카메라가 나중에 삭제된 경우 등) 그냥 무해하게
    쓰이지 않을 뿐이라 여기서는 형식만 본다."""
    keep = sorted({str(i).strip().upper() for i in (ids or ())
                   if _CAMERA_ID_RE.match(str(i).strip().upper())})
    value = ",".join(keep)
    set_value(db, KEY_RESTREAM_EXCLUDED_IDS, value)
    return set(keep)


def restream_on_demand(db: Session | None = None) -> bool:
    """MediaMTX가 리더(RTSP 소비자·WHEP 시청자)가 있을 때만 원본에
    붙을지(R-03, 위 KEY_RESTREAM_ON_DEMAND 주석 참고). 기본 꺼짐 —
    새 동작은 검증 전에는 안전측(``restream_enabled``와 같은 원칙)."""
    return get(KEY_RESTREAM_ON_DEMAND, db) == "1"


def set_restream_on_demand(db: Session, value) -> bool:
    """저장. 참으로 해석되지 않는 값은 전부 꺼짐(0)으로 눕힌다
    (``set_restream_enabled``와 같은 fail-safe 원칙)."""
    v = "1" if value in (True, "1", "true", "True", 1) else "0"
    set_value(db, KEY_RESTREAM_ON_DEMAND, v)
    return v == "1"


def restream_on_demand_start_timeout_sec(db: Session | None = None) -> str:
    return get(KEY_RESTREAM_ON_DEMAND_START_TIMEOUT_SEC, db) or "30"


def restream_on_demand_close_after_sec(db: Session | None = None) -> str:
    return get(KEY_RESTREAM_ON_DEMAND_CLOSE_AFTER_SEC, db) or "60"


def restream_relay_ids(db: Session | None = None) -> set[str]:
    """ffmpeg 릴레이를 거칠 카메라 ID 집합 — MediaMTX 자신의 HLS
    디먹서가 손상시키는 카메라를 여기 올린다(위 KEY 주석 참고).
    ``restream_excluded_ids()``와 완전히 같은 형식·패턴이다."""
    raw = get(KEY_RESTREAM_RELAY_IDS, db)
    return {p.strip().upper() for p in (raw or "").split(",") if p.strip()}


def set_restream_relay_ids(db: Session, ids) -> set[str]:
    """저장. 형식 검증은 ``set_restream_excluded_ids()``와 동일."""
    keep = sorted({str(i).strip().upper() for i in (ids or ())
                   if _CAMERA_ID_RE.match(str(i).strip().upper())})
    value = ",".join(keep)
    set_value(db, KEY_RESTREAM_RELAY_IDS, value)
    return set(keep)


def video_quality_enabled(db: Session | None = None) -> bool:
    """상시 화질 감시 스캐너를 쓸지. 기본 꺼짐 — 위 KEY_VIDEO_QUALITY_ENABLED
    주석 참고(``restream_enabled``와 같은 fail-safe 원칙)."""
    return get(KEY_VIDEO_QUALITY_ENABLED, db) == "1"


def set_video_quality_enabled(db: Session, value) -> bool:
    """저장. 참으로 해석되지 않는 값은 전부 꺼짐(0)으로 눕힌다."""
    v = "1" if value in (True, "1", "true", "True", 1) else "0"
    set_value(db, KEY_VIDEO_QUALITY_ENABLED, v)
    return v == "1"


def video_quality_scan_interval_sec(db: Session | None = None) -> int:
    """전체 카메라를 한 바퀴 도는 주기(초). 60초~1시간으로 눕힌다 —
    너무 짧으면 뷰어 전용 카메라 재연결이 잦아지고, 너무 길면 관리자가
    설정 실수를 몇 시간째 모를 수 있다."""
    return _clamped_int(db, KEY_VIDEO_QUALITY_SCAN_INTERVAL_SEC, 900, 60, 3600)


def set_video_quality_scan_interval_sec(db: Session, value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 900
    n = min(max(n, 60), 3600)
    set_value(db, KEY_VIDEO_QUALITY_SCAN_INTERVAL_SEC, str(n))
    return n


def video_quality_warn_threshold(db: Session | None = None) -> int:
    """표본 안 디코더 오류 수가 이 값을 넘으면 "주의" 등급."""
    return _clamped_int(db, KEY_VIDEO_QUALITY_WARN_THRESHOLD, 3, 0, 100000)


def set_video_quality_warn_threshold(db: Session, value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 3
    n = min(max(n, 0), 100000)
    set_value(db, KEY_VIDEO_QUALITY_WARN_THRESHOLD, str(n))
    return n


def video_quality_crit_threshold(db: Session | None = None) -> int:
    """표본 안 디코더 오류 수가 이 값을 넘으면 "손상 심각" 등급."""
    return _clamped_int(db, KEY_VIDEO_QUALITY_CRIT_THRESHOLD, 15, 0, 100000)


def set_video_quality_crit_threshold(db: Session, value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 15
    n = min(max(n, 0), 100000)
    set_value(db, KEY_VIDEO_QUALITY_CRIT_THRESHOLD, str(n))
    return n


def evidence_levels(db: Session | None = None) -> set[str]:
    """증거를 남길 등급 집합. 화면과 파이프라인이 같은 값을 본다."""
    raw = get(KEY_EVIDENCE_LEVELS, db)
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


# 고를 수 있는 보존기간. 「안 함」을 남겨 두는 이유는, 수사·분쟁으로 특정
# 자료를 오래 붙들어야 하는 상황이 실제로 있기 때문이다.
RETENTION_CHOICES = [
    (0, "자동 파기 안 함"),
    (1, "1개월"), (3, "3개월"), (6, "6개월"),
    (12, "12개월"), (24, "24개월"), (36, "36개월"),
]


def evidence_retention_months(db: Session | None = None) -> int:
    try:
        return max(int(get(KEY_EVIDENCE_RETENTION_MONTHS, db) or 0), 0)
    except (TypeError, ValueError):
        return 0


def set_evidence_retention_months(db: Session, months) -> int:
    """저장. 목록에 없는 값은 **0(안 함)으로 눕힌다** — 임의의 값이 들어와
    엉뚱한 시점에 자료가 사라지는 것보다, 안 지우고 알리는 편이 안전하다."""
    try:
        n = int(months)
    except (TypeError, ValueError):
        n = 0
    if n not in {v for v, _ in RETENTION_CHOICES}:
        n = 0
    set_value(db, KEY_EVIDENCE_RETENTION_MONTHS, str(n))
    return n


def set_evidence_levels(db: Session, levels) -> str:
    """저장. 알 수 없는 등급은 버린다 — 화면은 목록에서만 고르게 하므로
    다른 값이 왔다면 직접 만든 요청이다."""
    from .events import LEVELS
    keep = [lv for lv in LEVELS if lv in set(levels or ())]
    value = ",".join(keep)
    set_value(db, KEY_EVIDENCE_LEVELS, value)
    return value


# 지점 카드 정렬. 「고정」이 자리 이동을 없애는 선택지다.
CARD_ORDERS = {
    "severity": ("위험도순 (기본)", "위험한 지점이 위로 올라옵니다. "
                                    "값이 바뀔 때마다 카드 자리가 바뀝니다."),
    "fixed": ("등록순 고정", "자리가 바뀌지 않습니다. 관제 중 읽던 지점을 "
                             "놓치지 않습니다."),
    "name": ("이름순 고정", "지점 이름 가나다순. 자리가 바뀌지 않습니다."),
}

# 카드 안 차량 목록 정렬. 어느 쪽이든 **표시 대상은 감속이 큰 차량**이며
# (서버가 그렇게 고른다), 여기서 정하는 것은 **줄 세우는 순서**뿐이다.
OBJECT_ORDERS = {
    "drop": ("감속순 (기본)", "많이 감속한 차량이 위로. 줄 순서가 자주 바뀝니다."),
    "id": ("번호순 고정", "차량 번호 순서로 고정합니다."),
}


_cache: dict[str, str] = {}
_lock = threading.Lock()


def load_all(db: Session) -> dict[str, str]:
    """DB 값을 읽어 기본값 위에 덮어쓴다."""
    values = dict(DEFAULTS)
    for row in db.scalars(select(AppSetting)).all():
        if row.value:
            values[row.key] = row.value
    with _lock:
        _cache.clear()
        _cache.update(values)
    return values


def get(key: str, db: Session | None = None) -> str:
    """캐시 우선. 캐시가 비어 있고 db 가 주어지면 읽어 온다."""
    with _lock:
        if key in _cache:
            return _cache[key]
    if db is not None:
        return load_all(db).get(key, DEFAULTS.get(key, ""))
    return DEFAULTS.get(key, "")


def set_value(db: Session, key: str, value: str) -> str | None:
    """설정 저장. 이전 값을 돌려준다(감사 로그 기록용)."""
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    before = row.value if row else None
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    with _lock:
        _cache[key] = value
    return before


def invalidate() -> None:
    with _lock:
        _cache.clear()


# --- 색 계산 ----------------------------------------------------------------
def parse_hex(s: str) -> tuple[int, int, int] | None:
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None


def _lin(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _shift(rgb: tuple[int, int, int], amount: int) -> str:
    """밝은 배경이면 어둡게, 어두운 배경이면 밝게 — 패널·경계선을 만든다."""
    dark = luminance(rgb) < 0.5
    out = []
    for c in rgb:
        v = c + amount if dark else c - amount
        out.append(max(0, min(255, v)))
    return "#%02X%02X%02X" % tuple(out)


def derive_theme(bg_hex: str) -> dict[str, str]:
    """배경색 하나에서 나머지 변수를 만든다.

    배경만 바꾸고 패널·글자색을 그대로 두면 대비가 무너진다. 배경 밝기에 따라
    글자색을 흑/백으로 뒤집고, 패널·경계선은 배경에서 파생시킨다.
    """
    rgb = parse_hex(bg_hex) or parse_hex(DEFAULTS[KEY_BOARD_BG])
    light_bg = luminance(rgb) >= 0.5
    text = "#161E2E" if light_bg else "#E7ECF5"
    muted = "#5A6B85" if light_bg else "#93A0BB"
    return {
        "bg": "#%02X%02X%02X" % rgb,
        "panel": _shift(rgb, 12),
        "border": _shift(rgb, 30),
        # ★ 2026-08-27 — CCTV 관리의 「탐지 지정·보정·수정」 팝오버가 배경
        # 위에서 테두리가 구분되지 않는다는 신고로 신설. `border`(+30)는
        # `panel`(+12)과 18단계 차이뿐이라, 카드처럼 이미 옅게 깔린 화면
        # 위에 뜨는 팝업에는 약하다. 팝업·모달처럼 **화면 위로 떠야 하는
        # 요소**에는 이 값(+60)을 써서 어떤 배경색을 고르더라도(밝든
        # 어둡든) 확실히 갈라져 보이게 한다.
        "border_strong": _shift(rgb, 60),
        "text": text,
        "muted": muted,
        # 로고도 글자색과 같은 규칙으로 뒤집는다. 로고 워드마크의 "Urban"은
        # 다크판에서 흰색이라, 밝은 배경에 다크판을 쓰면 글자가 사라진다.
        "logo": "light" if light_bg else "dark",
    }


def contrast_warning(bg_hex: str) -> str | None:
    """본문 글자와의 대비가 기준(4.5:1)에 못 미치면 사유를 돌려준다."""
    rgb = parse_hex(bg_hex)
    if rgb is None:
        return "색상 형식이 올바르지 않습니다. #RRGGBB 로 입력하세요."
    theme = derive_theme(bg_hex)
    ratio = contrast_ratio(rgb, parse_hex(theme["text"]))
    if ratio < 4.5:
        return (f"본문 글자와의 명도 대비가 {ratio:.1f}:1 로 접근성 기준"
                f"(4.5:1)에 못 미칩니다.")
    return None
