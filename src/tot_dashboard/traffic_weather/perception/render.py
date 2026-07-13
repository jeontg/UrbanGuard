"""Synthetic scene rendering (PIL) — dashboard snapshot + (optional) VLM input.

Ported from flood3's ``perception/render.py``. Not used on the real-video path
(the real frame is used as-is). For the synthetic scenario it visualizes the
"camera view" for the dashboard, and is also the VLM's image input when used.
"""
from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image, ImageDraw


def render_scene(dets, weather, wh: tuple[int, int] = (1280, 720),
                 lanes: int = 3) -> Image.Image:
    w, h = wh
    img = Image.new("RGB", (w, h), (72, 74, 80))  # asphalt
    d = ImageDraw.Draw(img)
    for i in range(1, lanes + 1):  # dashed lane lines
        y = int(h * i / (lanes + 1))
        for x in range(0, w, 60):
            d.line([(x, y), (x + 30, y)], fill=(190, 190, 190), width=2)
    for det in dets:  # vehicle boxes
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        d.rectangle([x1, y1, x2, y2], fill=(66, 135, 210), outline=(20, 40, 60), width=2)
    rain = weather.rain_mm_h  # rain tint (darker/bluer)
    if rain > 0:
        alpha = min(0.5, rain / 30.0 * 0.5)
        img = Image.blend(img, Image.new("RGB", (w, h), (35, 45, 75)), alpha)
        d = ImageDraw.Draw(img)
        n = int(min(rain, 25) * 6)  # rain streaks
        for k in range(n):
            x = (k * 137) % w
            y = (k * 211) % h
            d.line([(x, y), (x - 6, y + 14)], fill=(200, 210, 230), width=1)
    label = f"rain {rain:.0f}mm/h ({weather.intensity.value})  vehicles {len(dets)}"
    d.rectangle([0, 0, 300, 22], fill=(0, 0, 0))
    d.text((6, 6), label, fill=(235, 235, 235))
    return img


def frame_to_image(frame_bgr, dets) -> Image.Image:
    """Real BGR video frame (numpy) -> PIL image with vehicle boxes drawn
    (snapshot/VLM input)."""
    rgb = np.ascontiguousarray(np.asarray(frame_bgr)[:, :, ::-1])
    img = Image.fromarray(rgb)
    d = ImageDraw.Draw(img)
    for det in dets:
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        d.rectangle([x1, y1, x2, y2], outline=(66, 135, 210), width=2)
    return img


def to_jpeg_b64(image: Image.Image, max_side: int = 640) -> str:
    im = image.copy()
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
