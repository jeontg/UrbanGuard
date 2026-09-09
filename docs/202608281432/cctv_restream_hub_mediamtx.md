# CCTV 재배포 허브(MediaMTX) 도입 — 4개 탐지 서비스 통합 스트리밍

앤시정보기술(주) · 전략기획부 · 2026-08-28

---

## 1. 배경

○ 4개 탐지 서비스(침수·교통위험·인파·노면)가 각자 원본 CCTV 서버(부산시
　ITS·서울시 TOPIS/spatic, 전부 HLS)에 **개별 연결**하고 있었다.

○ 실측 기록(`road/live_analyzer.py` 주석) — 같은 카메라에 **세 번째
　연결이 시도되면 CCTV 서버가 거절**한다. 오늘 DB 조회로 카메라 4곳
　(SEOUL-1042·SEOUL-19·SEOUL-920·SEOUL-207)이 이미 "연결 그룹 2개"로
　한계치에 있는 것을 확인했다(침수+교통위험은 프레임을 공유해 1그룹,
　인파·노면은 각각 별도 그룹) — 인파를 하나만 더 켜도 3번째 연결이 되어
　거절당할 실질적 위험이 있었다.

○ 해결책으로 오픈소스 미디어 서버 **MediaMTX**(MIT, Go 단일 바이너리)를
　재배포 허브로 도입한다 — 원본에는 카메라당 연결 1개만 열고, 그 뒤에서
　RTSP(탐지 서비스용)·WebRTC/WHEP(관제요원 브라우저용)로 재배포한다.

○ 사용자가 확정한 두 가지 정책:
　① **재배포 서버 장애 시 폴백 없음** — 원본 CCTV로 자동 전환하지 않고
　　"관측 없음"으로 정직하게 표시한다.
　② **브라우저 실시간 뷰는 커스텀 WHEP 플레이어** — 기존 모달 UI를
　　그대로 두고 영상 태그만 WebRTC로 바꾼다.

---

## 2. 구현 내용

| 파일 | 변경 |
|---|---|
| `core/settings.py` | `restream.*` 설정 5종 신설(기본 꺼짐) — 기존 4단계 패턴(`crowd_continuous_source`) 그대로 |
| `core/restream.py`(신규) | URL 조립(RTSP·WHEP)·MediaMTX Control API 연동·헬스체크. **실패를 전부 삼킨다** |
| `core/cameras.py::to_block_dict()` | 재배포가 켜져 있으면 `source.url`을 RTSP로 치환, 원본은 `source.origin_url`에 보존 |
| `service/continuous.py::_stream_url()` | 인파는 `to_block_dict()`를 안 거치므로 별도로 같은 치환 |
| `service/runner.py::_build_source()` | 재배포 연결 실패 시 `kind="restream_unavailable"`로 표시(폴백 없음) |
| `service/runner.py::_block_loop()` | 이 kind일 때 침수·교통위험 판정을 건너뛰고 "관측 없음"으로 스냅샷 기록 |
| `service/routes_cameras.py` | 카메라 생성·수정·삭제 시 MediaMTX 경로 즉시 동기화(실패해도 저장은 성공) |
| `service/main.py`, `runner.py::_snapshot()` | API 응답에 `whep_url` 추가(재배포 꺼짐/응답없음이면 `null`) |
| `service/static/app.js` | 커스텀 WHEP(WebRTC) 플레이어 신설, 기존 hls.js와 나란히 자동 분기 |
| `scripts/ensure-mediamtx.ps1`·`stop-mediamtx.ps1`(신규) | PostgreSQL과 동일한 패턴(멱등 기동·Windows 핸들 상속 함정 회피) |
| `scripts/urbanguard-service.ps1` | 기동·종료·상태 확인에 MediaMTX 통합(`-KeepMediaMTX` 옵션 포함) |
| `configs/mediamtx_base.yml`(신규) | 커밋된 설정 본 — 실제 설정(`.tools\mediamtx\`)은 git 제외 |

○ **핵심 설계 원칙**: 카메라 소스 URL 저장 스키마(`Camera.source_url`)는
　변경하지 않았다 — 재배포 주소는 카메라 ID에서 결정적으로 파생될 뿐
　사람이 입력할 값이 아니다. MediaMTX 설정 YAML도 미리 굽지 않는다 —
　DB가 유일한 정본이라는 이 프로젝트의 반복 원칙 그대로, Control API로
　런타임에만 채운다.

○ **기본값은 꺼짐**이다 — 재배포 서버는 새로운 단일 장애점(SPOF)이라,
　검증 전에 기본으로 켜 두면 재배포 서버 안정성 문제가 곧바로 전체
　탐지 중단으로 번진다.

---

## 3. 검증

○ 신규 단위시험 6개 파일(URL 빌더·Control API 실패 흡수·헬스체크
　캐시·`to_block_dict()` 치환·카메라 CRUD 훅 회귀·`_block_loop` 폴백
　없음 정책) — 전체 회귀 1,930건 통과(스킵 3건 제외).

○ **실기 검증** — 서비스 재기동 후 실측:
　－ MediaMTX 바이너리가 없는 상태에서도 서비스 기동이 막히지 않고
　　"재배포 기능 없이 계속합니다"로 안내 후 정상 기동됨
　－ `/api/risk` 응답의 `stream_url`이 원본 HLS 그대로, `whep_url`은
　　`null`, `observed: true`, `source_kind: hls` — **재배포가 없는 지금
　　상태에서 기존 동작이 한 글자도 안 바뀜을 확인**
　－ `urbanguard-service.ps1 -Action status`에 MediaMTX 상태 줄이
　　정상 표시됨

○ `flood_t` 폴더는 이번 작업에서도 손대지 않았다(작업 전후 39건 동일).

○ **★ 실기 시험 중 발견한 결함(개발 과정에서 잡아 고침)** — 처음 구현에서
　`core/cameras.py::to_block_dict()`가 재배포 설정을 "캐시만 보고 DB
　세션은 새로 열지 않는" 방식으로 짰다. 그런데 `main.py`의
　`BLOCKS = _load_blocks()`는 **lifespan 시작 전, 모듈 최상단**에서
　이 함수를 호출한다 — 그 시점엔 설정 캐시가 비어 있어 **다른 프로세스
　(관리 스크립트)가 DB에 이미 켜 둔 재배포 설정을 못 읽고 항상 "꺼짐"
　으로 떨어지는 결함**이 실기 검증 중 확인됐다(설정을 켜고 재기동해도
　여전히 원본 URL로 붙음). `runner.py`의 기존 rainfall 설정 조회
　(같은 함정을 겪은 선례)와 같은 패턴으로 되돌려 수정하고, 전체 회귀
　1,930건 재확인 후(4분, 성능 저하 없음 확인) 다시 실기 검증했다.

○ **★★ 실기 검증 성공 — 실제 CCTV로 RTSP·WHEP 파이프라인 전체 확인**:
　처음 문제였던 카메라 4곳 중 **BLOCK-BEOMNAEGOL·SEOUL-1042·SEOUL-19·
　SEOUL-87 4곳이 재배포 경로로 정상 연결**돼 `observed: true,
　whep_url: http://127.0.0.1:8889/.../whep`로 실측 확인됐다. MediaMTX가
　실제 서울시 TOPIS 스트림(`topiscctv1.eseoul.go.kr`)을 성공적으로
　풀링해 RTSP로 재배포하고, 우리 탐지 코드(`cv2.VideoCapture`)가 그
　RTSP를 받아 실제 프레임(1920×1080)을 읽는 것까지 별도로 직접
　확인했다 — **아키텍처의 핵심 전제(원본 HLS를 재배포로 성공적으로
　중계할 수 있는가)가 실제 정부 CCTV 인프라를 상대로 검증됐다.**

○ 나머지 7곳(BLOCK-BUSANSTN·CENTUMSTN·OLYMPIC·SEOUL-113·207·235·920)은
　같은 재기동에서 `observed: false`("관측 없음")로 정직하게 표시됐다
　(**폴백 없음 정책이 의도대로 작동한 결과** — 조용히 원본으로 돌아가지
　않고 정직하게 실패를 드러냈다). 실기 확인 중 이 상태를 장시간
　방치하지 않기 위해 **재배포를 다시 끄고 서비스를 재기동해 전체
　11개 지점의 정상 관측을 즉시 복구**했다(복구 확인 완료, `flood_t`
　무변경 재확인 완료). 이후 라이브 서비스와 무관하게 MediaMTX만 따로
　띄워 원인을 끝까지 추적했다 — 두 가지 서로 다른 원인이 섞여 있었다.

○ **★ 원인 1 (5곳, 일시적) — 콜드 스타트/동시 연결 경합**: SEOUL-207을
　다른 36개 카메라 없이 **단독으로** 등록해 보니 곧바로
　`ready:true·stream is available`로 성공했다. 즉 37개를 한꺼번에
　켜면서 생긴 **일시적 경합**이었을 뿐 — BLOCK-BUSANSTN·SEOUL-113·
　235·920도 같은 부류로 추정된다(재현 실측은 SEOUL-207만 진행).
　**대응**: 재배포를 켤 때 카메라를 한꺼번에 켜지 말고, MediaMTX가
　각 경로를 `ready`로 안정시킬 시간을 준 뒤(수 분) 서비스를 재기동하면
　피할 수 있을 것으로 보인다.

○ **★★ 원인 2 (2곳, 지속적 — 원본 CCTV 서버 특성)**:
　BLOCK-CENTUMSTN·BLOCK-OLYMPIC은 **단독으로 3분 이상 관찰해도 단
　한 번도 성공하지 못했다.** MediaMTX 디버그 로그로 정확한 실패
　지점을 확인했다 — 두 지점 모두 부산시 ITS 서버(Wowza 계열,
　`chunklist_w<임의숫자>.m3u8` 명명 규칙으로 확인)가 **매번 새로운
　임의의 세션 경로를 발급하는데, 그 세션으로 첫 세그먼트 하나는
　받아지지만 같은 세션을 다시 조회(재생목록 폴링, 표준 HLS 동작)하면
　곧바로 연결이 끊긴다(EOF)**. 즉 이 두 카메라 원본은 "재생목록을
　한 번 받으면 그 세션으로 계속 이어 본다"는 표준 HLS 동작을
　지원하지 않고, **매 폴링마다 최상위 재생목록을 새로 받아야 하는
　방식**으로 보인다. 우리 탐지 서비스가 지금까지 문제없이 봐 온 이유는
　**FFmpeg(OpenCV가 내부적으로 씀)의 HLS 디코더가 이 방식을 우연히
　감내**해 온 것으로 보이며, MediaMTX의 자체 HLS 수신 구현은 아직 이
　경우를 못 견딘다 — **MediaMTX 자체의 한계이지 이번에 작성한 코드의
　결함이 아니다.**

---

## 4. 옵션 ③ 실기 적용 — 최종 결과 (2026-08-28 완료)

○ 사용자가 "③ 이 2곳만 당분간 원본 직결, 나머지는 재배포로 전환"을
　선택해 실기 적용을 진행했다. 처음 35개소를 한꺼번에 등록했더니
　**당초 2곳이 아니라 7곳**이 응답 없이 멈췄다 — 그대로 제외 목록에
　넣기 전에 **원인부터 개별 격리 재현으로 확인**했다(사용자 지시).

○ **격리 재현 방법** — MediaMTX Control API로 문제 카메라를 하나씩만
　등록해(다른 경쟁 없이) 정상 연결되는지 확인. 결과, 7곳의 원인이
　둘로 갈렸다:

　－ **진짜 호환 안 됨(4곳, 확정 제외)** — BLOCK-CENTUMSTN·OLYMPIC은
　　§3의 기존 발견 그대로. 이번에 **BLOCK-BUSANSTN**도 단독 기동에서
　　동일한 EOF 반복 패턴이 재현돼 같은 부류로 확정했다.
　　**BLOCK-CHORYANG**은 단독 기동에서도 다른 신호("max recorded
　　size exceeded")가 반복 재현돼, 역시 재배포와 근본적으로 안
　　맞는 별도 문제로 확정했다.
　　→ 최종 제외 목록(`restream.excluded_ids`): **BLOCK-CENTUMSTN·
　　BLOCK-OLYMPIC·BLOCK-BUSANSTN·BLOCK-CHORYANG** (4곳).

　－ **콜드 스타트 동시연결 경합(5곳, 제외 아님)** — SEOUL-101·113·
　　126·83·922. 전부 `strm4.spatic.go.kr`(서울시 TOPIS) **같은
　　원본 서버**에 몰린 카메라였다(이 서버에만 14개소가 걸려 있다).
　　단독으로는 즉시 정상 연결됐고(1920×1080 H264 확인), **8초
　　간격으로 순차 등록**하니 이 서버 소속 14개소 전부 정상
　　연결됐다 — **재배포 비호환이 아니라 순수 동시연결 수 문제**였다.
　　같은 방식으로 `its-stream3.busan.go.kr`(부산 ITS)도 확인 —
　　이 서버 소속 4곳 중 BEOMNAEGOL만 성공, BUSANSTN·CHORYANG·
　OLYMPIC 3곳이 실패해 **원본 서버 하나당 버틸 수 있는 동시 신규
　연결 수가 매우 작다**는 원래 문제의식(§1 "세 번째 연결부터
　거절")과 같은 계열의 현상임을 재확인했다.

○ **근본 수정 — `sync_all_paths()`에 호스트별 순차 등록 반영**
　(`core/restream.py`). 같은 원본 서버로 가는 등록 사이에만 최소
　3초 간격을 두고, 서로 다른 서버는 그대로 동시에 등록한다. 이
　수정 하나로 **strm4의 14개소 전부**가 콜드 스타트 실패 없이
　연결됐다(수동 재현 시험 기준) — 앞으로 재기동할 때마다 이 문제가
　재발하지 않는다.

○ **기동 시 자동 동기화 신설** — 원래 계획(§1~2)에 있었지만 아직
　코드로 안 옮겨져 있던 부분을 이번에 채웠다. `service/main.py`의
　`lifespan()`이 기동 시 `sync_all_paths()`를 **별도 스레드**에서
　1회 호출한다(앱 응답을 막지 않기 위해 — 카메라가 많은 서버는
　순차 등록에 수십 초 걸릴 수 있다). 이전에는 관리자가 수동으로
　불러야 했다.

○ **★ 실기 적용 중 발견한 결함 — "폴백 없음" 정책과 콜드 스타트의
　상호작용**. MediaMTX와 UrbanGuard 서비스를 **동시에** 재기동하면,
　MediaMTX가 카메라 경로를 아직 하나도 못 채운 순간에 파이프라인이
　먼저 RTSP 연결을 시도해 실패 → `kind="restream_unavailable"`로
　**영구** 고정된다(재시도 로직이 없다 — 다음 재기동 전까지
　"관측 없음"에서 못 벗어난다). 실측: SEOUL-113·BLOCK-BEOMNAEGOL이
　이 경합에 걸려 `observed:false`로 떨어졌다. **대응**: MediaMTX가
　이미 모든 경로를 `ready:true`로 채운 뒤에 UrbanGuard 서비스만
　재기동한다(`urbanguard-service.ps1 -Action restart -KeepDb
　-KeepMediaMTX`) — 재확인 결과 11개 상시 관측 지점 전부
　`observed:true, source_kind:hls`로 정상 복구됐다. 이 "동시
　재기동 시 영구 고착" 위험은 아직 코드로 막아 두지 않았다 —
　재시도 로직 추가는 후속 과제로 `docs/pending_tasks.md`에
　남겨 둔다.

○ **최종 실기 확인 (재기동 후, MediaMTX 33/33 경로 `ready:true`)**:
　－ 제외 4곳(BLOCK-CENTUMSTN·OLYMPIC·BUSANSTN·CHORYANG) —
　`source_kind:hls, observed:true`, `stream_url`이 원본 그대로
　(RTSP로 치환 안 됨) — 설계대로 원본 직결 유지 확인.
　－ 상시 관측 11곳(BLOCK-BEOMNAEGOL·SEOUL-1042·12·19·207·235·
　87·113·920 등) 전부 `observed:true` — MediaMTX 쪽에서도
　실제 RTSP reader 연결 확인(`readers` 배열에 실제 접속 확인).
　－ 신규 회귀시험(제외 목록·호스트별 순차 등록) 포함 `tests/core/`·
　`tests/service/` 전량 통과(1,938건 + 784건, 실패 없음).
　－ `flood_t` 폴더 무변경 재확인(39건).

---

## 5. 남은 과제

○ **재시도 로직 없음** — 위 "폴백 없음 × 콜드 스타트" 결함대로,
　`kind="restream_unavailable"`로 한 번 고정되면 다음 서비스
　재기동 전까지 복구가 안 된다. 운영 절차(반드시 MediaMTX를 먼저
　완전히 띄운 뒤 서비스를 재기동)로 우회하고 있으나, 코드 차원의
　주기적 재시도(예: N분마다 `_build_source` 재시도)를 검토할
　필요가 있다.

○ 배포 서버의 포트(RTSP 8554·WebRTC 8889/UDP 8189·Control API 9997)
　충돌·방화벽(특히 UDP 차단 여부) 확인이 필요하다.

○ MediaMTX 자체는 MIT 라이선스이나, 납품 문서(오픈소스 사용 내역)에
　추가해야 하는지 확인이 필요하다.

---

붙임: 없음. 끝.
