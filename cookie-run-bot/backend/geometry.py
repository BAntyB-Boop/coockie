"""Coordinate transforms between capture frame, game viewport and Android display.

Spaces:
  * frame px      - pixels of the captured image (desktop region or screencap)
  * desktop px    - absolute virtual-desktop pixels (MSS capture only)
  * viewport norm - [0,1] coordinates inside the game viewport (ROIs, touch points)
  * display px    - Android display pixels used by ``adb shell input``
Browser canvas <-> frame conversion lives in ``frontend/app.js`` (letterbox + DPR).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import NormPoint, NormRect, PixelRect

MIN_ROI_PX = 8


@dataclass(frozen=True)
class PxRect:
    x: int
    y: int
    w: int
    h: int


def _finite(*vals: float) -> None:
    for v in vals:
        if not math.isfinite(v):
            raise ValueError("non-finite coordinate")


def viewport_px(vp: NormRect, fw: int, fh: int) -> PxRect:
    if fw <= 0 or fh <= 0:
        raise ValueError("frame dimensions must be positive")
    x = min(fw - 1, max(0, int(round(vp.x * fw))))
    y = min(fh - 1, max(0, int(round(vp.y * fh))))
    w = max(1, min(fw - x, int(round(vp.w * fw))))
    h = max(1, min(fh - y, int(round(vp.h * fh))))
    return PxRect(x, y, w, h)


def roi_to_frame_px(roi: NormRect, vp: NormRect, fw: int, fh: int) -> PxRect:
    v = viewport_px(vp, fw, fh)
    x = v.x + int(round(roi.x * v.w))
    y = v.y + int(round(roi.y * v.h))
    w = max(1, min(v.x + v.w - x, int(round(roi.w * v.w))))
    h = max(1, min(v.y + v.h - y, int(round(roi.h * v.h))))
    return PxRect(x, y, w, h)


def roi_pixel_problems(name: str, roi: NormRect, vp: NormRect, fw: int, fh: int) -> list[str]:
    r = roi_to_frame_px(roi, vp, fw, fh)
    if r.w < MIN_ROI_PX or r.h < MIN_ROI_PX:
        return [f"{name} ROI is {r.w}x{r.h}px; minimum is {MIN_ROI_PX}x{MIN_ROI_PX}px"]
    return []


def viewport_norm_to_frame(pt: NormPoint, vp: NormRect, fw: int, fh: int) -> tuple[float, float]:
    v = viewport_px(vp, fw, fh)
    return v.x + pt.x * v.w, v.y + pt.y * v.h


def frame_to_viewport_norm(px: float, py: float, vp: NormRect, fw: int, fh: int) -> tuple[float, float]:
    _finite(px, py)
    v = viewport_px(vp, fw, fh)
    nx = (px - v.x) / v.w
    ny = (py - v.y) / v.h
    return min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))


def frame_to_desktop(px: float, py: float, region: PixelRect, fw: int, fh: int) -> tuple[int, int]:
    _finite(px, py)
    sx = region.width / fw
    sy = region.height / fh
    return int(round(region.left + px * sx)), int(round(region.top + py * sy))


def to_android_display(pt: NormPoint, vp: NormRect, fw: int, fh: int, display_w: int, display_h: int,
                       capture_is_device: bool) -> tuple[int, int]:
    """Map a viewport-normalized point to Android display pixels.

    * ADB capture: the frame *is* the (rotated) display, so frame px scale to display px.
    * Desktop/replay capture: the calibrated viewport must cover exactly the Android display.
    """
    if display_w <= 0 or display_h <= 0:
        raise ValueError("display dimensions must be positive")
    if capture_is_device:
        fx, fy = viewport_norm_to_frame(pt, vp, fw, fh)
        dx = fx * display_w / fw
        dy = fy * display_h / fh
    else:
        dx = pt.x * display_w
        dy = pt.y * display_h
    _finite(dx, dy)
    return min(display_w - 1, max(0, int(round(dx)))), min(display_h - 1, max(0, int(round(dy))))
