"""기관 로고 교체 — 업로드 파일 저장·검증·크기 조회.

지자체마다 자기 기관 로고를 쓰고 싶어 한다. 이미지를 **소스 트리(static/)에
쓰지 않고** 별도 데이터 폴더에 두는 이유는, 코드를 배포로 덮어써도 운영 중에
올린 로고가 날아가지 않게 하기 위해서다.

기본 로고는 배경 밝기에 따라 밝은판/어두운판이 따로 있지만, **업로드 로고는
한 장뿐**이다. 두 벌을 요구하면 실무에서 쓰이지 않는다 — 대신 관리자 화면에서
현재 배경 위에 얹어 보여 주고, 대비가 나쁘면 눈으로 판단하게 한다.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..common.config import PROJECT_ROOT

log = logging.getLogger("urbanguard.branding")

LOGO_DIR = PROJECT_ROOT / "data" / "branding"

# 허용 형식. SVG는 스크립트를 품을 수 있어 받지 않는다.
ALLOWED_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_BYTES = 2 * 1024 * 1024        # 2MB — 로고에 이보다 클 이유가 없다
MAX_EDGE = 4000                    # 한 변 최대 픽셀

# 기본 로고. 배경 밝기에 따라 갈리고, 쓰이는 자리에 따라 판형이 다르다.
#   ui    — 사이드바용(가로로 눌린 4.79:1)
#   tight — 로그인 화면용(여백만 잘라낸 3.3:1)
DEFAULT_LOGO = {
    "ui": "/static/brand/urbanguard_logo_ui_{variant}.png",
    "tight": "/static/brand/urbanguard_logo_tight_{variant}.png",
}
DEFAULT_LOGO_SIZE = (556, 116)     # ui 판 기준

# 권장 크기 — 사이드바 폭 204px에 맞춘 2배수(고해상도 화면 대비).
RECOMMENDED = (408, 88)


def logo_path(filename: str) -> Path:
    """저장된 로고의 실제 경로. 경로 조작을 막으려 파일명만 취한다."""
    return LOGO_DIR / Path(filename).name


def logo_url(filename: str, variant: str = "dark", form: str = "ui") -> str:
    """화면에서 쓸 로고 주소. 업로드본이 없으면 기본 로고.

    ``form`` 은 기본 로고의 판형(`ui` 사이드바 / `tight` 로그인)이다.
    업로드본은 한 장뿐이라 어느 자리든 같은 파일을 쓴다.
    """
    if filename and logo_path(filename).is_file():
        return f"/branding/logo/{Path(filename).name}"
    return DEFAULT_LOGO.get(form, DEFAULT_LOGO["ui"]).format(variant=variant)


def image_size(filename: str) -> tuple[int, int] | None:
    """(가로, 세로) 픽셀. 읽지 못하면 None."""
    if not filename:
        return DEFAULT_LOGO_SIZE
    p = logo_path(filename)
    if not p.is_file():
        return DEFAULT_LOGO_SIZE
    try:
        from PIL import Image
        with Image.open(p) as im:
            return (im.width, im.height)
    except Exception:  # noqa: BLE001
        log.exception("로고 크기를 읽지 못했습니다: %s", p)
        return None


def format_size(size: tuple[int, int] | None) -> str:
    """화면 표기용 `(408px, 88px)` 문자열."""
    if not size:
        return "(크기 확인 불가)"
    return f"({size[0]}px, {size[1]}px)"


def save_logo(data: bytes, original_name: str) -> tuple[str, str | None]:
    """로고를 저장하고 (저장된 파일명, 오류) 를 돌려준다.

    검증에 실패하면 파일명은 빈 문자열이다. **한 번에 하나만 두고** 이전
    파일은 지운다 — 쌓아 두면 어느 것이 현재 로고인지 알 수 없다.
    """
    suffix = Path(original_name or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        return "", ("PNG · JPG · WEBP 파일만 올릴 수 있습니다. "
                    "SVG는 보안상 받지 않습니다.")
    if not data:
        return "", "빈 파일입니다."
    if len(data) > MAX_BYTES:
        return "", f"파일이 너무 큽니다. {MAX_BYTES // 1024 // 1024}MB 이하로 올려 주세요."

    # 확장자만 믿지 않는다 — 실제로 이미지인지 열어서 확인한다.
    try:
        import io

        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.width, im.height
    except Exception:  # noqa: BLE001
        return "", "이미지 파일이 아니거나 손상되었습니다."

    if w > MAX_EDGE or h > MAX_EDGE:
        return "", f"이미지가 너무 큽니다. 한 변 {MAX_EDGE}px 이하로 올려 주세요."

    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for old in LOGO_DIR.glob("logo.*"):
        try:
            old.unlink()
        except OSError:
            log.warning("이전 로고를 지우지 못했습니다: %s", old)

    name = f"logo{suffix}"
    (LOGO_DIR / name).write_bytes(data)
    log.info("로고 교체 %s (%dx%d, %d bytes)", name, w, h, len(data))
    return name, None


def clear_logo(filename: str) -> None:
    """업로드 로고를 지운다. 기본 로고로 되돌아간다."""
    if not filename:
        return
    p = logo_path(filename)
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        log.warning("로고를 지우지 못했습니다: %s", p)


def media_type(filename: str) -> str:
    return ALLOWED_SUFFIX.get(Path(filename).suffix.lower(), "application/octet-stream")
