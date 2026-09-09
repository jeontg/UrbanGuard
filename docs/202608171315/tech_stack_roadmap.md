# 기술스택 설계 — 국가사업 요구에 맞춘 최신화 방안

> UrbanGuard · 앤시정보기술(주)
> 2026-08-17 13:15 · v1
> 짝 문서: `docs/202608171242/national_overseas_projects_analysis.md`
> 대상: 도로 침수 · 인파 혼잡 · 도로 노면 파손
> 현재 스택은 **실제 설치본에서 확인한 버전**입니다 (2026-08-17 기준)

---

## 0. 결론 먼저

○ **가장 큰 성과는 모델 교체가 아니라 런타임 교체에서 나옵니다.**
  ★ **2026-08-17 우리 모델·우리 CPU에서 실측했습니다** — 침수 분할 모델
  기준 **ONNX 2.0배 · OpenVINO 7.2배**(중앙값), **검출 결과는 완전 동일**
  (IoU 1.0000 · 신뢰도 차이 0.0000). 상세는 같은 폴더
  `runtime_benchmark_result.md` 참조.
  우리는 **CPU 한 대로 39지점 상시**라 이 배수가 곧 수용 지점 수입니다.
  **모델을 안 바꾸고 얻는 이득**이라 위험이 가장 낮습니다

○ **DB는 PostgreSQL 하나로 갑니다. 다만 확장도 넣지 않습니다.**
  ★ **2026-08-17 확인 — PostGIS·TimescaleDB·pgvector 가 설치본에 없고,
  earthdistance 는 superuser 가 필요합니다.** 그런데 39지점 반경 질의는
  **순수 SQL 로 7.2ms** 에 됩니다. **망분리 고객사에 설치 대상을 늘리지
  않는 것**이 지금 얻을 수 있는 성능보다 큽니다 (3-2절)

○ **영상 전송이 가장 낡았습니다.** 지금은 **base64 JPEG를 폴링**으로 받습니다.
  2026년 감시 분야 표준은 **WebRTC + WHEP**이고, 온프레미스는
  **go2rtc / MediaMTX**로 1초 미만을 냅니다

○ **예측은 거대 모델부터 가지 않습니다.** 고전 통계(statsforecast)로 먼저
  세우고, 필요하면 **Moirai-small(1,400만 파라미터)** 급을 검토합니다.
  우리 병목은 모델이 아니라 **관측 기간이 짧다**는 것입니다

○ ⚠️ **흔한 오해 하나를 미리 못박습니다. ONNX로 내보내도 AGPL은 사라지지
  않습니다.** 런타임만 바뀔 뿐 **가중치와 학습 코드의 라이선스는 그대로**입니다.
  라이선스는 **모델을 갈아야** 풀립니다

---

## 1. 현재 스택 (실제 설치본 확인)

추측이 아니라 `pip list`와 소스에서 확인한 값입니다.

### 1-1. 런타임·프레임워크

| 계층 | 현재 | 비고 |
|---|---|---|
| 언어 | **Python 3.12.0** | 3.13+의 free-threaded 옵션 미적용 |
| 웹 | **FastAPI 0.141.1** + **uvicorn 0.52.1** | |
| 템플릿 | **Jinja2 3.1.6** | 서버 렌더링 |
| 프런트 | **순수 JS** (`app.js` · `ugmap.js`) + CSS | **빌드 도구·CDN 없음 — 망분리 대응** |
| 화면 | 템플릿 **28개** | 반응형 |
| 소스 | Python **133개 파일 · 25,671줄** | |
| 시험 | **995건 통과 · 3건 건너뜀** | |

### 1-2. 데이터

| 계층 | 현재 |
|---|---|
| DBMS | **PostgreSQL 17.7** (포트 5433) |
| ORM | **SQLAlchemy 2.0.51** |
| 마이그레이션 | **Alembic 1.19.0** |
| 드라이버 | **psycopg 3.3.4** |
| 확장 | **없음** ← 이 문서의 핵심 제안 지점 |

### 1-3. 영상·추론

| 계층 | 현재 | 비고 |
|---|---|---|
| 추론 | **PyTorch 2.13.0 (CPU)** | GPU 없음 |
| 검출 | **ultralytics 8.4.115** | ⚠️ **AGPL** |
| 보조 | **torchvision 0.28.0** | BSD — 인파는 이쪽 |
| 영상 | **opencv-python 5.0.0.93** | |
| 추적 | **supervision 0.30.0** (ByteTrack) | ⚠️ ByteTrack deprecated 경고 발생 중 |
| 인코딩 | imageio-ffmpeg | 증거 클립 `mp4v` |

### 1-4. 지금 구조의 정확한 모습

| 항목 | 현재 방식 |
|---|---|
| 동시성 | **카메라당 `threading.Thread`** — 큐·브로커 없음 |
| 영상 전송 | **base64 JPEG를 JSON에 실어** 전달 |
| 화면 갱신 | **`fetch` 폴링(`setInterval`)** — WebSocket·SSE 없음 |
| 추론 위치 | **서버 CPU 집중** — 스트림을 전부 서버로 끌어옴 |
| 예측 | 침수 확산 12초(`RiskPredictor`) 외 **없음** |

> ⚠️ **이 표가 이 문서에서 가장 중요합니다.** 여기서부터 개선 항목이 나옵니다.
> 잘못된 것이 아니라 **39지점 실증에 맞춰 단순하게 만든 것**이며, 지점이
> 늘면 순서대로 한계에 닿습니다.

---

## 2. 국가사업 분석에서 나온 기술 요구

짝 문서에서 확인한 사실을 **기술 요구로 번역**하면 이렇습니다.

| 국가사업이 하고 있는 것 | 우리에게 생기는 기술 요구 |
|---|---|
| 도시침수예보가 **10분 주기 자동 분석**, CCTV를 입력으로 채택 | **외부 예보 체계에 값을 넘길 표준 연계**(API·주기·좌표계) |
| 행안부가 **영상전송 연계 인프라** 제공, DPG로 학습데이터 배포 | **외부 학습데이터를 받아 재학습하는 파이프라인** |
| 현장인파관리가 **GIS 통합상황판 + 4등급** | **공간 질의**(PostGIS) — 지금은 좌표를 문자열로 다룸 |
| 국가·국제 공통으로 **디지털트윈 + 예측** | **시계열 저장·예측**(TimescaleDB + 예측 모델) |
| 일본 국토교통성 **기설 CCTV 활용, 검지율 90%** | **CPU에서 더 많은 지점** — 런타임 최적화 |
| KISTI **XAI(설명 가능)** 방향 | **판정 근거의 구조화 저장** — 이미 일부 있음 |
| 관제요원 **1인당 477대** | **사후 검색** — 자연어·유사영상 검색(pgvector) |

---

## 3. 계층별 기술스택 권고

각 항목은 **현재 → 권고 → 근거 → 위험·난이도** 순으로 씁니다.

---

### 3-1. 추론 런타임 ★★★ 최우선

| 구분 | 내용 |
|---|---|
| 현재 | PyTorch 2.13 CPU에서 `.pt` 직접 실행 |
| **권고** | **OpenVINO를 1순위**(Intel CPU 확정 시), **ONNX Runtime을 기본·이식용**으로 |
| 근거 | ★ **실측**(2026-08-17, i7-1360P, 96회 추론) — 침수 분할 p50 **PyTorch 539.7ms → ONNX 267.4ms(2.0배) → OpenVINO 74.8ms(7.2배)** |
| 결과 동일성 | ★ **검출 개수·좌표·신뢰도 완전 일치** (IoU 1.0000, 신뢰도 차이 0.0000) — 정확도 손실 없음 |
| 왜 우리에게 큰가 | **CPU 한 대로 39지점 상시**가 우리 최대 제약입니다. 같은 CPU 예산으로 훨씬 많은 프레임을 처리할 수 있어 **지자체 확대 제안의 근거**가 됩니다 |
| ⚠️ 운영 주의 | **OpenVINO는 예열이 필요합니다.** 첫 2회차 약 950ms → 3회차부터 70~76ms. **기동 시 예열 추론을 넣지 않으면 초기에 느립니다** |
| ⚠️ 운영 주의 | **장치를 CPU로 고정**해야 합니다. 내장 GPU 커널 컴파일을 시도하다 실패한 사례를 확인했습니다(`kernel.errors.txt`) |
| ⚠️ 운영 주의 | **`task`를 반드시 명시**합니다. 내보낸 파일이 task 메타데이터를 잃어 `segment`가 `detect`로 로드되면 마스크 계수를 클래스 점수로 읽습니다 |
| 위험 | 낮음. **모델을 바꾸지 않습니다.** 결과 동일성은 실측으로 확인했습니다 |
| 난이도 | 중 — `core/model_registry.py`가 `.pt` 확장자 고정이라 `.onnx`·OpenVINO 디렉터리 인식 추가 필요 |

> **왜 ONNX도 남기는가** — OpenVINO는 Intel에 최적화돼 있습니다. **납품 서버가
> AMD이거나 리눅스 배포판이 다르면** ONNX Runtime이 안전한 기본값입니다.
> 둘 다 두고 **설치 시 고르는 구조**가 맞습니다.

> ⚠️ **AGPL은 해소되지 않습니다.** ONNX는 **실행 형식**일 뿐이고, ultralytics로
> 학습한 가중치와 학습 코드의 라이선스는 그대로입니다. **속도 문제와
> 라이선스 문제는 별개**이며 별도로 풀어야 합니다.

**참고 — 모델 세대**: YOLO26이 공개돼 **YOLO26n이 x86에서 YOLOv8n보다 약 2배**
빠르다고 보고됩니다. 다만 **운영 모델은 그대로 둔다**는 방침이므로 이번 범위
밖이며, 라이선스도 함께 확인해야 합니다.

---

### 3-2. 데이터 계층 ★★★

**PostgreSQL을 늘리지 말고 확장으로 채웁니다.**

> ### ★ 2026-08-17 실제 확인 결과 — **권고를 바꿉니다**
>
> 설치본을 직접 조회해 보니 **세 확장이 모두 없습니다.**
>
> | 확장 | 상태 |
> |---|---|
> | PostGIS · TimescaleDB · pgvector | **설치본에 없음** — 별도 바이너리 필요 |
> | earthdistance | 번들에 있으나 **superuser 필요** (`urbanguard` 는 아님) |
> | cube · btree_gist | 설치 가능 |
>
> **그런데 확장 없이도 됩니다.** 순수 SQL 하버사인으로 「부산시청 기준
> 가까운 순」을 **7.2ms** 에 얻었습니다(39지점, 실측).
>
> **지금 필요한 공간 질의는 확장 없이 처리하고, PostGIS·TimescaleDB 는
> 지점 수가 수백 단위로 늘 때 다시 판단합니다.**
>
> ⚠️ **납품 관점이 더 중요합니다.** 망분리 고객사에서 **DBA 가 확장을 따로
> 설치해야 한다면 「DB 하나 유지」라는 이점이 오히려 약해집니다.** 확장을
> 넣는 순간 설치 가이드·보안 검토·백업 절차가 다 늘어납니다.

### 그래도 확장이 필요해지는 시점

| 확장 | 언제 | 대안으로 버틸 수 있는 이유 |
|---|---|---|
| **PostGIS** | 지점 수백~수천, 폴리곤 연산 | 39지점 반경 질의는 순수 SQL 로 **7.2ms** |
| **TimescaleDB** | 초당 수천 건 적재 | 우리는 39지점 × 분당 몇 건. PostgreSQL 17 기본 파티셔닝·BRIN 으로 충분 |
| **pgvector** | 유사 상황 검색을 실제로 붙일 때 | 아직 그 기능이 없다 |

| 구분 | 내용 |
|---|---|
| **지금 할 일** | **없음** — 순수 SQL 로 처리 |
| 위험 | 낮음 — 설치 대상을 늘리지 않는다 |
| 재검토 조건 | 지점 수 수백 이상 · 유사 검색 착수 · 적재량 급증 |

---

### 3-3. 영상 전송 ★★★

| 구분 | 내용 |
|---|---|
| 현재 | **base64 JPEG를 JSON에 실어** 폴링으로 전달 |
| 문제 | base64는 원본보다 **약 33% 큽니다.** 폴링이라 **지연이 주기에 묶입니다** |
| **권고** | **go2rtc** 또는 **MediaMTX**를 영상 게이트웨이로 두고 **WebRTC + WHEP** |
| 근거 | 2026년 감시 생태계가 **WebRTC 중심으로 통합**됐고 WHEP/WHIP가 상호운용 기준. **0.5초 수준**, 온프레미스 RTSP 수집 + WHEP 배포로 **1초 미만** |
| 왜 중요한가 | 관제요원은 **위협이 화면을 벗어나기 전에** 봐야 합니다. 방송용 프로토콜(HLS 6~60초)은 관제에 부적합합니다 |
| 위험 | 중 — 구성 요소가 하나 늘어납니다. 다만 **둘 다 온프레미스 단일 실행 파일**이라 망분리에 적합 |
| 난이도 | 중 |

> **화면 갱신도 같이 봅니다.** 폴링 → **SSE(Server-Sent Events)** 가 가장
> 적은 비용의 개선입니다. 단방향이면 충분하고, FastAPI에서 바로 되며,
> **추가 의존성이 없습니다.** WebSocket은 양방향이 필요할 때만.

---

### 3-4. 예측·시계열 ★★

세 도메인 전부 예측이 비어 있습니다(침수 12초 확산 제외).

| 단계 | 기술 | 이유 |
|---|---|---|
| **1단계** | **statsforecast**(Nixtla) 등 고전 통계 | CPU에서 매우 빠르고, **근거를 설명할 수 있습니다**. XAI 방향과 맞음 |
| **2단계** | **Moirai-small (14M)** 등 소형 시계열 기초 모델 | 제로샷 예측. **소형(14M)/기본(91M)/대형(311M)** 중 소형만 CPU 후보 |
| 참고 | Chronos-2(2025-10)는 다변량·공변량 지원 | 강수량 같은 외생 변수를 넣을 때 |

> ⚠️ **정직하게 적습니다. 우리 병목은 모델이 아닙니다.**
> 노면은 **관측 기간이 짧고 지표가 국제 표준과 다릅니다**(건/100m vs IRI·PCI).
> ※ **2026-08-17 정정** — 당초 「인파는 흐름 자체를 안 만든다」고 적었으나
> **사실이 아닙니다.** 속도·방향분산·발산도를 계산해 **위험도 가중치의 65%**
> 로 쓰고 있습니다(`202608171851` 참조). 없는 것은 **시계열 예측**입니다.
> **입력이 없는데 예측 모델을 얹으면 숫자만 그럴듯해집니다.**
> 1단계는 모델 도입이 아니라 **시계열을 제대로 쌓는 것**(TimescaleDB)입니다.

**인파 흐름은 예외적으로 가깝습니다** — **ByteTrack이 이미 붙어 있어**
추적 결과에서 속도·방향 벡터를 뽑을 토대가 있습니다. 다만 supervision의
ByteTrack이 **deprecated 경고**를 내고 있어 교체 검토가 함께 필요합니다.

---

### 3-5. 동시성·확장 ★★

| 구분 | 내용 |
|---|---|
| 현재 | 카메라당 `threading.Thread`, 큐·브로커 없음. **Python 3.12라 GIL 적용** |
| 1차 권고 | **작업 큐 도입** — 지점 수와 스레드 수를 **분리**합니다. 지금은 지점이 늘면 스레드가 그대로 늡니다 |
| 2차 권고 | **추론 프로세스 분리** — GIL을 우회하려면 스레드가 아니라 **프로세스**여야 합니다 |
| 검토 | **Python 3.13+ free-threaded 빌드** — GIL 없는 실행. ⚠️ **생태계 호환성 미확인**, 지금 도입은 이릅니다 |
| 보류 | Redis·Kafka 등 외부 브로커 — **망분리에서 설치 대상이 느는 대가**가 이득보다 큽니다. PostgreSQL의 `LISTEN/NOTIFY`로 충분한지 먼저 검토 |

---

### 3-6. 프런트엔드 ★

| 구분 | 내용 |
|---|---|
| 현재 | **순수 JS + CSS, 빌드 도구·CDN 없음** |
| **권고** | **그대로 유지합니다** |
| 이유 | 망분리에서 CDN은 **아예 안 뜹니다.** 빌드 도구는 납품처에서 **재빌드 불가**가 됩니다. 지금 선택이 옳습니다 |
| 보완 | 지도는 **자체 타일 서버 또는 VWorld**로 교체 필요(현재 OSM 공개 타일 — 대량 이용 불가·망분리 불가) |
| 보완 | 접근성·반응형은 유지. 표준 웹 컴포넌트로 정리하면 템플릿 28개의 중복을 줄일 수 있음 |

> **「최신 트렌드」가 곧 프레임워크 도입은 아닙니다.** 공공 납품에서
> **의존성이 적은 것 자체가 요건**입니다. 이 부분은 바꾸지 않는 것이 최신입니다.

---

### 3-7. 자연어 검색 · VLM ★

| 구분 | 내용 |
|---|---|
| 시장 방향 | 「탐지에서 이해·검색으로」 — 자연어로 영상을 묻고 답을 받음 |
| ⚠️ 제약 | **망분리에서 클라우드 LLM은 불가**입니다. 온프레미스 소형 모델만 후보 |
| ⚠️ 제약 | **CPU로 39지점 상시 VLM은 불가능**합니다 |
| **현실적 권고** | **상시 추론이 아니라 사후 검색에만** 씁니다. 이벤트 발생 시점의 프레임만 임베딩해 **pgvector**에 넣고 **유사 상황 검색**부터 |
| 효과 | 「이런 장면 이전에도 있었나」에 답할 수 있습니다. 관제요원 1인 477대 부담의 실질적 완화 |

---

### 3-8. 운영·관측·보안

| 항목 | 현재 | 권고 |
|---|---|---|
| 오류 관리 | **S-92 오류 사전·이력 보유** | 유지. 이미 잘 되어 있음 |
| 감사 | **감사 로그 보유** | 유지 |
| 재기동 | **S-87 OS별 재기동** | 유지 |
| 지표 | 없음 | **OpenTelemetry** 검토 — 다만 망분리에서 수집기 추가 부담. **우선순위 낮음** |
| 비밀번호 | passlib + **bcrypt 5.0** | 유지 |
| 규제 | — | ⚠️ **AI 기본법 고영향 AI 해당 여부 법무 검토** — 기술보다 시급 |

---

## 4. 도메인별로 무엇이 필요한가

| 도메인 | 지금 없는 것 | 필요한 기술 | 선행 조건 |
|---|---|---|---|
| **침수** | 국가 예보 체계와의 **연계 규격** | PostGIS(좌표계 정합) + 표준 API | 예보 측 인터페이스 확인 |
| **침수** | 부산 실환경 **검증** | 재학습 파이프라인 | **DPG 학습데이터 접근 조건** |
| **인파** | **흐름 기반 시계열 예측** (흐름 자체는 ✅ 있음) | 흐름 이력 축적 → 예측 모델 | ✅ ByteTrack 이전 완료(`202608171918`) |
| **인파** | 시계열 **예측** | statsforecast → 소형 기초 모델 | 흐름 데이터가 먼저 |
| **노면** | **예측**(추이만 있음) | TimescaleDB + 열화 모델 | **관측 기간**·구간 길이 |
| **노면** | 국제 **지표 정합** | 검출 → 분할 재설계 | 별도 과제 (규모 큼) |
| **공통** | **처리 용량** | ONNX Runtime / OpenVINO | 없음 — 바로 가능 |
| **공통** | **사후 검색** | pgvector | 임베딩 모델 선정 |

---

## 5. 도입 순서

**위험이 낮고 효과가 확실한 것부터** 놓았습니다.

### 1단계 — 지금 바로 (모델·운영 무변경)

| # | 작업 | 효과 |
|---|---|---|
| 1 | **OpenVINO · ONNX Runtime 도입** (예열 · CPU 고정 · `task` 명시 포함) | ★ 실측 **7.2배 / 2.0배**, 결과 동일 |
| 2 | ~~PostGIS · TimescaleDB 설치~~ → **순수 SQL 로 처리** | 확장이 설치본에 없고, **39지점은 7.2ms 로 충분** |
| 3 | **폴링 → SSE** | 지연 감소, 의존성 0 |

> 세 가지 모두 **운영 모델을 건드리지 않습니다.** 실패해도 되돌리기 쉽습니다.
> 1번의 속도·결과 동일성은 **이미 실측으로 확인**했습니다
> (`runtime_benchmark_result.md`). 남은 일은 **운영 코드에 붙이는 것**입니다.

### 2단계 — 실증 확대 대비

| # | 작업 | 효과 |
|---|---|---|
| 4 | **go2rtc/MediaMTX + WebRTC/WHEP** | 1초 미만 지연 |
| 5 | **작업 큐 도입** — 지점 수와 스레드 수 분리 | 확장성 |
| 6 | **인파 흐름 벡터 산출** (ByteTrack 활용) | 가장 큰 기능 공백 해소 |
| 7 | **지도 타일 자체 서버 또는 VWorld** | 망분리 대응 · 납품 필수 |

### 3단계 — 예측·검색

| # | 작업 |
|---|---|
| 8 | statsforecast 기반 **시계열 예측**(인파·노면) |
| 9 | **pgvector 유사 상황 검색** |
| 10 | 소형 시계열 기초 모델 검토 |

### 4단계 — 별도 판단 필요

| # | 작업 | 왜 별도인가 |
|---|---|---|
| 11 | **모델 교체**(AGPL 해소 포함) | 계획 B로 보류 중 |
| 12 | 노면 **검출 → 분할** 재설계 | 규모가 큼 |
| 13 | **엣지 추론** 전환 | 카메라·NVR 교체 수반 — **지자체 비용 구조가 바뀜** |

---

## 6. 채택하지 않는 것과 이유

**넣지 않기로 한 것을 적는 것이 넣는 것만큼 중요합니다.**

| 기술 | 왜 안 하는가 |
|---|---|
| **프런트 프레임워크**(React 등) | 망분리에서 CDN 불가, 납품처 재빌드 불가. **지금 구조가 정답** |
| **Redis · Kafka** | 설치·인증·백업 대상이 늘어남. PostgreSQL `LISTEN/NOTIFY` 우선 검토 |
| **별도 벡터 DB · 시계열 DB** | pgvector·TimescaleDB로 충분. **DB 하나 유지가 공공 납품에서 더 큰 가치** |
| **Kubernetes** | 단일 서버 온프레미스에 과투자 |
| **클라우드 LLM API** | **망분리에서 불가.** 검토 자체가 무의미 |
| **3D 디지털트윈**(Cesium 등) | 국가사업은 전부 디지털트윈이지만, **3D 지형·건물 데이터 확보와 타일 서버**가 선행. 현 단계에서는 **2D + 시계열이 현실적** |
| **Python 3.13+ free-threaded** | GIL 제거는 매력적이나 **생태계 호환성 미확인**. 시기상조 |
| **GPU 도입** | 조건 확인 필요. **CPU 최적화(1단계)를 먼저 하고 나서** 판단 |

---

## 7. 제약 — 이 문서의 모든 판단이 여기서 나옵니다

| # | 제약 | 영향 |
|---|---|---|
| 1 | **망분리** | CDN·클라우드·외부 API **전부 불가**. 온프레미스 단일 설치가 요건 |
| 2 | **CPU 전용, 서버 1대** | 대형 모델·상시 VLM 불가. **런타임 최적화가 유일한 지렛대** |
| 3 | **AGPL(ultralytics)** | ONNX로 해소되지 **않음**. 모델 교체가 필요 |
| 4 | **부산 실환경 라벨 0** | 재학습 불가. **DPG 학습데이터가 열쇠** |
| 5 | **운영 모델 무변경 방침** | 이번 권고는 전부 **모델 밖** 개선 |
| 6 | **AI 기본법 고영향 AI 가능성** | 설계에 영향. **기술보다 먼저 법무 검토** |

---

## 8. 확인하지 못한 것

추측으로 채우지 않았습니다.

| # | 항목 | 왜 필요한가 |
|---|---|---|
| 1 | ~~PostgreSQL 17.7과 세 확장의 호환 버전~~ | ✅ **확인 완료** — 셋 다 설치본에 없음. 순수 SQL 로 대체(3-2절) |
| 2 | ~~우리 모델의 ONNX 내보내기 실측치~~ | ✅ **2026-08-17 실측 완료** — ONNX 2.0배 · OpenVINO 7.2배 |
| 3 | **납품 서버 CPU 제조사(Intel/AMD)** | 개발 장비는 Intel i7-1360P로 확인. **납품 사양은 여전히 미확정** — OpenVINO 적용 여부가 갈림 |
| 3-1 | **전용 서버에서의 재측정** | 실측이 라이브 서비스와 CPU를 공유한 상태였음. 비율은 유효하나 절대값은 보수적 |
| 4 | go2rtc/MediaMTX의 **국정원 보안적합성 검증** 대상 여부 | 공공 납품 전제 |
| 5 | **DPG 학습데이터 접근 조건** | 재학습 파이프라인의 전제 |
| 6 | supervision **ByteTrack 후속 API** | deprecated 경고 발생 중 |
| 7 | 도시침수예보 **연계 인터페이스 규격** | 공개 여부 자체가 미확인 |
| 8 | YOLO26 **라이선스** | 세대 교체 검토 시 |

---

## 9. 권고 요약

| 순위 | 할 일 | 이유 | 위험 |
|---|---|---|---|
| 1 | **OpenVINO 도입**(Intel 확정 시) · **ONNX Runtime 병행** | 모델 안 바꾸고 **실측 7.2배 / 2.0배**, 결과 동일 | 낮음 |
| 2 | ~~PostGIS + TimescaleDB~~ → **순수 SQL** | 확장이 없고 39지점은 7.2ms 로 충분. **설치 대상을 안 늘린다** | 없음 |
| 3 | **폴링 → SSE** | 의존성 0으로 지연 개선 | 낮음 |
| 4 | **AI 기본법 법무 검토** | 기술보다 시급 | — |
| 5 | **WebRTC/WHEP 전환** | 관제 지연 1초 미만 | 중 |
| 6 | **인파 흐름 벡터** | 가장 큰 기능 공백 | 중 |
| 7 | **DPG 학습데이터 조건 확인** | 재학습의 전제 | — |

---

## 10. 출처 (2026-08-17 확인)

**추론 런타임**
- [Ultralytics Docs — Model Benchmarking](https://docs.ultralytics.com/modes/benchmark)
- [Ultralytics Docs — YOLO26 Deployment Options Compared](https://docs.ultralytics.com/guides/model-deployment-options)
- [arXiv — YOLO26: Key Architectural Enhancements and Performance Benchmarking](https://arxiv.org/html/2509.25164v4)
- [arXiv — Ultralytics YOLO Evolution: YOLO26, YOLO11, YOLOv8, YOLOv5](https://arxiv.org/html/2510.09653v3)
- [OpenVINO Documentation — Benchmark Tool](https://docs.openvino.ai/2026/get-started/learn-openvino/openvino-samples/benchmark-tool.html)
- [MDPI Electronics — Accelerating Deep Learning Inference: Comparative Analysis of Modern Acceleration Frameworks](https://www.mdpi.com/2079-9292/14/15/2977)

**데이터 계층**
- [Tiger Data — PostgreSQL as a Vector Database: pgvector Tutorial](https://www.tigerdata.com/blog/postgresql-as-a-vector-database-using-pgvector)
- [PostgreSQL Extensions: PostGIS, TimescaleDB, pgvector](https://dasroot.net/posts/2026/03/postgresql-extensions-postgis-timescaledb-pg-vector/)
- [Monitoring PostgreSQL Extensions (pgvector, TimescaleDB, PostGIS) in 2026](https://medium.com/@philmcc/best-tools-for-monitoring-postgresql-extensions-pgvector-timescaledb-postgis-in-2026-61df8caf2926)
- [PostgreSQL Vector Search in 2026: pgvector vs pgvectorscale](https://devstarsj.github.io/2026/04/04/postgresql-pgvector-pgvectorscale-rag-production-guide-2026/)

**영상 전송**
- [go2rtc — Real-Time Streaming & Camera Protocol](https://go2rtc.com/)
- [MDPI Computers — Low-Latency Autonomous Surveillance: Hybrid RTSP-WebRTC Architecture with YOLOv11](https://www.mdpi.com/2073-431X/15/1/62)
- [Visylix — HLS vs WebRTC for Security](https://visylix.com/blog/hls-vs-webrtc-streaming-protocol-security)
- [nanocosmos — WebRTC Latency: Comparing Low-Latency Streaming Protocols (2026)](https://www.nanocosmos.net/blog/webrtc-latency/)

**예측·시계열**
- [MachineLearningMastery — The 2026 Time Series Toolkit: 5 Foundation Models](https://machinelearningmastery.com/the-2026-time-series-toolkit-5-foundation-models-for-autonomous-forecasting/)
- [Nixtla — foundation-time-series-arena](https://github.com/Nixtla/nixtla/tree/main/experiments/foundation-time-series-arena)
- [AI Code Invest — Latest Time Series Forecasting Models: Chronos to iTransformer](https://aicodeinvest.com/latest-timeseries-forecasting-models-2026/)

---

> **작성 범위 안내** — 이 문서는 **신규 파일**이며 기존 문서는 수정하지
> 않았습니다. **코드도 변경하지 않았습니다** — 이 문서는 설계 제안입니다.
