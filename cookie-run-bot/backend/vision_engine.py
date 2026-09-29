"""Configurable baseline obstacle detector.

Scores are heuristic (0..1) summaries of how far measurements exceed thresholds.
They are NOT calibrated probabilities and do not imply accuracy in any real game.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from .config import AppConfig, DetectionParams, ProcessingParams
from .geometry import PxRect, roi_to_frame_px, viewport_norm_to_frame, viewport_px

JUMP_COLOR = (94, 197, 34)    # BGR green (#22c55e)
SLIDE_COLOR = (68, 68, 239)   # BGR red (#ef4444)
VIEWPORT_COLOR = (248, 189, 56)


@dataclass
class RoiResult:
    name: str
    roi: PxRect
    raw_hit: bool
    confirmed: bool
    onset_id: int
    density: float
    score: float
    boxes: list[tuple[int, int, int, int]]  # frame px (x, y, w, h)
    contours_total: int
    contours_kept: int
    diagnostics: dict = field(default_factory=dict)
    mask: Optional[np.ndarray] = None  # debug only

    def to_dict(self) -> dict:
        return {
            "raw": self.raw_hit, "confirmed": self.confirmed, "onset_id": self.onset_id,
            "density": round(self.density, 4), "score": round(self.score, 3),
            "boxes": self.boxes[:8], "contours_total": self.contours_total,
            "contours_kept": self.contours_kept, "diag": self.diagnostics,
        }


@dataclass
class VisionResult:
    frame_id: int
    frame_ts: float
    width: int
    height: int
    jump: RoiResult
    slide: RoiResult
    duration_ms: float


class Hysteresis:
    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.active = False
        self.onsets = 0

    def update(self, raw: bool, confirm: int, release: int) -> bool:
        if raw:
            self.hits += 1
            self.misses = 0
        else:
            self.misses += 1
            self.hits = 0
        if not self.active and self.hits >= confirm:
            self.active = True
            self.onsets += 1
        elif self.active and self.misses >= release:
            self.active = False
        return self.active


def preprocess(roi_bgr: np.ndarray, p: ProcessingParams) -> np.ndarray:
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    if p.blur_ksize >= 3:
        gray = cv2.GaussianBlur(gray, (p.blur_ksize, p.blur_ksize), 0)
    if p.method == "canny":
        mask = cv2.Canny(gray, p.canny_low, p.canny_high)
    else:
        limit = min(gray.shape[0], gray.shape[1])
        block = min(p.adaptive_block, limit if limit % 2 == 1 else limit - 1)
        block = max(3, block)
        mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                                     block, p.adaptive_c)
    if p.morph != "none":
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (p.morph_ksize, p.morph_ksize))
        op = {"open": cv2.MORPH_OPEN, "close": cv2.MORPH_CLOSE}.get(p.morph)
        if op is not None:
            mask = cv2.morphologyEx(mask, op, k, iterations=p.morph_iterations)
        elif p.morph == "dilate":
            mask = cv2.dilate(mask, k, iterations=p.morph_iterations)
        else:
            mask = cv2.erode(mask, k, iterations=p.morph_iterations)
    return mask


def analyze(mask: np.ndarray, d: DetectionParams, roi: PxRect) -> tuple[bool, float, float, list, int, dict]:
    h, w = mask.shape[:2]
    area = float(w * h)
    density = float(cv2.countNonZero(mask)) / area if area else 0.0
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    kept: list[tuple[int, int, int, int]] = []
    best_area = 0.0
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        a = (bw * bh) / area
        if a < d.min_area_frac or a > d.max_area_frac:
            continue
        if bw / w < d.min_width_frac or bh / h < d.min_height_frac:
            continue
        aspect = bw / max(bh, 1)
        if aspect < d.min_aspect or aspect > d.max_aspect:
            continue
        cy = (y + bh / 2) / h
        if cy < d.center_y_min or cy > d.center_y_max:
            continue
        kept.append((roi.x + x, roi.y + y, bw, bh))
        best_area = max(best_area, a)
    density_ok = density >= d.min_density
    contour_ok = bool(kept)
    raw = {
        "density": density_ok, "contour": contour_ok,
        "either": density_ok or contour_ok, "both": density_ok and contour_ok,
    }[d.require]
    ds = min(1.0, density / d.min_density) if d.min_density > 0 else 1.0
    cs = min(1.0, best_area / d.min_area_frac) if d.min_area_frac > 0 else (1.0 if kept else 0.0)
    score = 0.5 * ds + 0.5 * cs
    diag = {"density_ok": density_ok, "contour_ok": contour_ok, "best_area_frac": round(best_area, 4)}
    return raw, density, score, kept, len(contours), diag


class VisionEngine:
    def __init__(self) -> None:
        self._hyst = {"jump": Hysteresis(), "slide": Hysteresis()}

    def reset(self) -> None:
        self._hyst = {"jump": Hysteresis(), "slide": Hysteresis()}

    def _roi(self, name: str, img: np.ndarray, cfg: AppConfig, keep_mask: bool) -> RoiResult:
        fh, fw = img.shape[:2]
        norm = cfg.rois.jump_roi if name == "jump" else cfg.rois.slide_roi
        rv = cfg.vision.jump if name == "jump" else cfg.vision.slide
        r = roi_to_frame_px(norm, cfg.viewport, fw, fh)
        crop = img[r.y:r.y + r.h, r.x:r.x + r.w]
        mask = preprocess(crop, rv.processing)
        raw, density, score, boxes, total, diag = analyze(mask, rv.detection, r)
        hy = self._hyst[name]
        confirmed = hy.update(raw, rv.detection.confirm_frames, rv.detection.release_frames)
        return RoiResult(name, r, raw, confirmed, hy.onsets, density, score, boxes, total, len(boxes),
                         diag, mask if keep_mask else None)

    def process(self, frame_id: int, frame_ts: float, img: np.ndarray, cfg: AppConfig,
                keep_masks: bool = True) -> VisionResult:
        t0 = time.perf_counter()
        j = self._roi("jump", img, cfg, keep_masks)
        s = self._roi("slide", img, cfg, keep_masks)
        dt = (time.perf_counter() - t0) * 1000.0
        return VisionResult(frame_id, frame_ts, img.shape[1], img.shape[0], j, s, dt)


# ------------------------------------------------------------- preview rendering
def _scaled(img: np.ndarray, max_width: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    if w <= max_width:
        return img.copy(), 1.0
    s = max_width / w
    return cv2.resize(img, (max_width, max(1, int(round(h * s)))), interpolation=cv2.INTER_AREA), s


def render_view(view: str, img: np.ndarray, result: Optional[VisionResult], cfg: AppConfig,
                max_width: int) -> np.ndarray:
    """Build a preview image. The source frame is never modified (copies only)."""
    if view == "mask" and result is not None:
        base = (cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR) // 3).astype(np.uint8)
        for rr, color in ((result.jump, JUMP_COLOR), (result.slide, SLIDE_COLOR)):
            if rr.mask is None:
                continue
            r = rr.roi
            m = rr.mask[: r.h, : r.w] > 0
            region = base[r.y:r.y + r.h, r.x:r.x + r.w]
            region[m] = color
        out, s = _scaled(base, max_width)
        _draw_rois(out, result, cfg, s, img.shape[1], img.shape[0], fill=False)
        return out
    out, s = _scaled(img, max_width)
    if view == "raw" or result is None:
        return out
    _draw_rois(out, result, cfg, s, img.shape[1], img.shape[0], fill=True)
    return out


def _draw_rois(out: np.ndarray, result: VisionResult, cfg: AppConfig, s: float, fw: int, fh: int,
               fill: bool) -> None:
    vp = viewport_px(cfg.viewport, fw, fh)
    cv2.rectangle(out, (int(vp.x * s), int(vp.y * s)), (int((vp.x + vp.w) * s) - 1, int((vp.y + vp.h) * s) - 1),
                  VIEWPORT_COLOR, 1)
    for rr, color in ((result.jump, JUMP_COLOR), (result.slide, SLIDE_COLOR)):
        r = rr.roi
        p1 = (int(r.x * s), int(r.y * s))
        p2 = (int((r.x + r.w) * s), int((r.y + r.h) * s))
        if fill and rr.confirmed:
            sub = out[p1[1]:p2[1], p1[0]:p2[0]]
            if sub.size:
                tint = np.full_like(sub, color)
                cv2.addWeighted(tint, 0.35, sub, 0.65, 0, dst=sub)
        cv2.rectangle(out, p1, p2, color, 2 if rr.confirmed else 1)
        for (x, y, w, h) in rr.boxes[:8]:
            cv2.rectangle(out, (int(x * s), int(y * s)), (int((x + w) * s), int((y + h) * s)), (255, 255, 255), 1)
    for pt, color in ((cfg.input.jump_point, JUMP_COLOR), (cfg.input.slide_point, SLIDE_COLOR)):
        x, y = viewport_norm_to_frame(pt, cfg.viewport, fw, fh)
        cv2.drawMarker(out, (int(x * s), int(y * s)), color, cv2.MARKER_CROSS, 14, 2)


def encode_jpeg(img: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()
