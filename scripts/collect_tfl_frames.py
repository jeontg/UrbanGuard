"""침수·인파관리 학습 데이터 수집 — 런던 TfL JamCams 공개 CCTV.

## 왜 여기서 가져오는가

인파관리(crowd) 도메인은 자체 라벨 데이터가 전혀 없다(`common/data_archive.py`
"crowd_person_yolo" 항목 주석 참고). 국내에서 방범·다중이용시설 CCTV는
「개인정보 보호법」 제25조상 지자체 CCTV통합관제센터 전용망 안에서만 흐르고
외부로 받을 창구가 없다(`docs/202608251212/open_cctv_crowd_management_survey.md`
2절). 반면 런던 TfL(Transport for London)의 JamCams는 **공식 오픈데이터로
누구나 인증키만 받으면 즉시 접근**할 수 있고, 보행자 검출 학술 연구에 실제로
쓰인 선례도 있다(같은 문서 3절).

★ 2026-08-25 — 890개 지점 전체를 실측 분석한 결과(`docs/202608251307/
open_cctv_by_domain_classification.md` §4), 지하차도·터널 진입부를 찍는
지점 27개가 **침수 도메인에도 쓸 수 있음**을 확인해 이 스크립트를
2개 도메인 공용으로 확장했다(``--domain`` 인자로 구분).

## 한계 — 반드시 알아야 할 것

- **위치가 전부 런던**이다. 부산 CCTV와 화각·인종·조도가 달라, 이 데이터만으로
  학습하면 노면 손상 탐지가 겪은 것과 같은 **도메인 갭**이 재현될 위험이 있다
  (`docs/road_surface_management_plan.md` Phase 2). **학습 데이터 다양성 보강용
  이지 주력 데이터가 될 수 없다.**
- **진짜 실시간 영상이 아니다.** 이미지가 5~10분 간격으로 갱신된다 — 이 스크립트를
  짧은 간격으로 반복 실행해도 같은 이미지를 여러 번 받을 수 있다.
- **"Powered by TfL Open Data" 표기 의무**가 있다. 정확한 상업적 재이용 조건
  원문은 아직 확인하지 못했다(위 조사서 6절) — 대량 활용 전 재확인 권고.
- 이미지는 **차량 교통 카메라**가 찍은 것이라 보행자·침수와 무관한 장면이
  섞여 있다. `view` 필드로 대략적인 판단은 되지만, 실제로 원하는 장면이
  나오는지는 받은 뒤 육안 확인이 필요하다.
- 침수 지점은 **평상시(맑은 날) 사진**이다. 진짜 침수 장면이 아니라 "이
  구조물이 정상일 때 어떻게 보이는가"를 보여주는 **배경(네거티브) 샘플**이다.

## 저장 위치

기본은 학습 데이터 보관소(``D:\\dev-PoC_DATA``, ``common/data_archive.py``의
``URBANGUARD_DATA_ARCHIVE`` 환경변수로 바꿀 수 있음)이며, 탐지 기능별로
기존 폴더 체계를 그대로 따른다:

- 침수: ``02_학습데이터_침수/flood_water_own/raw/tfl_london/<카메라id>/<시각>.jpg``
- 인파: ``08_학습데이터_인파/crowd_person_own/raw/tfl_london/<카메라id>/<시각>.jpg``

``--project-local`` 을 주면 대신 프로젝트 폴더
(``data/datasets/<...>_own/raw/tfl_london/``, gitignore 대상)에 저장한다 —
보관소에 손대지 않고 시험해보고 싶을 때 쓴다.

메타데이터(지점명·좌표·화각 설명)는 같은 폴더의 ``meta.jsonl`` 에 한 줄씩
누적한다(``road/dataset_collector.py`` 와 같은 방식).

Usage:
    python scripts/collect_tfl_frames.py --domain crowd                 # 기본 60개 지점 표본
    python scripts/collect_tfl_frames.py --domain flood --all           # 전체(890개 안팎) 중 필터 없이
    python scripts/collect_tfl_frames.py --domain flood --camera-id JamCams_00002.00115 --camera-id JamCams_00001.07317
    python scripts/collect_tfl_frames.py --domain crowd --project-local # 보관소 대신 프로젝트 폴더에
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import DATA_ARCHIVE_ROOT

LIST_URL = "https://api.tfl.gov.uk/Place/Type/JamCam/"
TIMEOUT_SEC = 20
META_NAME = "meta.jsonl"

# 탐지 기능별 저장 위치 — (보관소 상대경로, 프로젝트 상대경로).
# 보관소 쪽 폴더 이름은 D:\dev-PoC_DATA\README.md "2. 무엇이 어디에 있나"와
# common/data_archive.py의 _DATASET_MAP 명명 규칙(<이름>_own)을 그대로 따른다.
DOMAIN_PATHS: dict[str, tuple[str, str]] = {
    "flood": ("02_학습데이터_침수/flood_water_own/raw/tfl_london",
             "data/datasets/flood_water_own/raw/tfl_london"),
    "crowd": ("08_학습데이터_인파/crowd_person_own/raw/tfl_london",
             "data/datasets/crowd_person_own/raw/tfl_london"),
}

# ⚠ 2026-08-25 실측 — TfL API·S3 이미지 둘 다 파이썬 urllib 기본
# User-Agent("Python-urllib/3.x")를 봇으로 보고 403으로 막는다. curl은
# 통과하고 urllib는 막히는 것으로 재현·확인했다. 브라우저형 User-Agent를
# 붙이면 정상 통과한다.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _app_key() -> str:
    return os.environ.get("TFL_APP_KEY", "").strip()


def fetch_camera_list(app_key: str) -> list[dict]:
    """전체 카메라 목록(위치·화각 설명·imageUrl 등 포함)을 한 번에 받는다."""
    url = LIST_URL + "?" + urllib.parse.urlencode({"app_key": app_key})
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"TfL API가 오류를 돌려줬습니다(HTTP {e.code}). "
            "TFL_APP_KEY가 올바른지 확인하세요.") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"TfL API에 접속하지 못했습니다 — {str(e)[:100]}") from e


def _props(cam: dict) -> dict:
    return {p["key"]: p.get("value") for p in cam.get("additionalProperties", [])}


def normalize(cam: dict) -> dict | None:
    """카메라 원본 항목 -> 이 스크립트가 쓰는 형태. 사용 불가/이미지 없으면 None."""
    props = _props(cam)
    if props.get("available") != "true":
        return None
    image_url = props.get("imageUrl")
    if not image_url:
        return None
    return {
        "id": cam.get("id"),
        "name": cam.get("commonName"),
        "lat": cam.get("lat"),
        "lon": cam.get("lon"),
        "view": props.get("view"),
        "image_url": image_url,
    }


def _download(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[collect_tfl_frames]   이미지 내려받기 실패: {str(e)[:80]}")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, choices=sorted(DOMAIN_PATHS),
                    help="어느 탐지 기능용인지 — 저장 위치가 달라진다")
    ap.add_argument("--limit", type=int, default=60,
                    help="받을 지점 수 상한(기본 60 — 남의 서버라 과하게 몰아치지 않음)")
    ap.add_argument("--all", action="store_true", help="전체 지점(--limit 무시)")
    ap.add_argument("--camera-id", action="append", default=None,
                    help="특정 카메라 id만 받기(여러 번 지정 가능)")
    ap.add_argument("--project-local", action="store_true",
                    help="보관소(D:\\dev-PoC_DATA) 대신 프로젝트 폴더에 저장")
    args = ap.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    app_key = _app_key()
    if not app_key:
        raise SystemExit(
            "[collect_tfl_frames] TFL_APP_KEY 가 .env 에 없습니다. "
            "https://api-portal.tfl.gov.uk/signup 에서 발급받아 넣으세요.")

    archive_rel, project_rel = DOMAIN_PATHS[args.domain]
    if args.project_local:
        out_root = PROJECT_ROOT / project_rel
    else:
        out_root = DATA_ARCHIVE_ROOT / archive_rel
    print(f"[collect_tfl_frames] 저장 위치: {out_root}")

    print("[collect_tfl_frames] 카메라 목록을 받는 중...")
    raw_list = fetch_camera_list(app_key)
    cams = [c for c in (normalize(c) for c in raw_list) if c is not None]
    print(f"[collect_tfl_frames] 전체 {len(raw_list)}개 중 사용 가능 {len(cams)}개")

    if args.camera_id:
        wanted = set(args.camera_id)
        cams = [c for c in cams if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cams}
        if missing:
            print(f"[collect_tfl_frames] 경고: 목록에 없거나 사용 불가한 id 무시됨: {missing}")
    elif not args.all:
        cams = cams[: args.limit]

    out_root.mkdir(parents=True, exist_ok=True)
    meta_path = out_root / META_NAME
    ts = time.strftime("%Y%m%d_%H%M%S")
    saved, failed = [], []
    with meta_path.open("a", encoding="utf-8") as meta_f:
        for cam in cams:
            data = _download(cam["image_url"])
            if data is None:
                failed.append(cam["id"])
                continue
            cam_dir = out_root / cam["id"]
            cam_dir.mkdir(parents=True, exist_ok=True)
            out_path = cam_dir / f"{ts}.jpg"
            # 이미 S3에서 JPEG로 받은 그대로 저장한다 — cv2로 다시 디코딩·
            # 인코딩하면 화질만 잃는다(불필요한 재압축).
            out_path.write_bytes(data)
            meta_f.write(json.dumps({
                "id": cam["id"], "name": cam["name"], "lat": cam["lat"],
                "lon": cam["lon"], "view": cam["view"],
                "file": str(out_path),
                "fetched_at": ts,
            }, ensure_ascii=False) + "\n")
            saved.append(str(out_path))
            print(f"[collect_tfl_frames]   저장: {cam['id']} ({cam['name']})")

    print(f"\n[collect_tfl_frames] 완료 — 저장 {len(saved)}건, 실패 {len(failed)}건")
    print(f"[collect_tfl_frames] 메타데이터: {meta_path}")
    print("[collect_tfl_frames] ⚠ 런던 CCTV라 부산과 화각이 다릅니다 — "
          "학습 데이터 다양성 보강용으로만 쓰고, 실제 원하는 장면이 나오는지는 "
          "받은 프레임을 육안으로 확인하십시오.")


if __name__ == "__main__":
    main()
