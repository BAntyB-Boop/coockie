"""Validated configuration models and atomic persistence.

All ROIs and touch points are normalized to the *game viewport*; the viewport
itself is normalized to the captured frame. See ``geometry.py`` for transforms.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIN_ROI_NORM = 0.02
EPS = 1e-9

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = PROJECT_ROOT / "profiles"
DATA_DIR = PROJECT_ROOT / "data"
ACTIVE_CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_PROFILE_PATH = PROFILES_DIR / "default.json"
ESTOP_FILE = PROJECT_ROOT / "EMERGENCY_STOP"

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z")  # \Z: `$` would allow a trailing newline
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]%]{0,128}$")
KEY_RE = re.compile(r"^[a-z0-9]{1,12}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class NormRect(StrictModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=MIN_ROI_NORM, le=1.0)
    h: float = Field(ge=MIN_ROI_NORM, le=1.0)

    @model_validator(mode="after")
    def _inside(self) -> "NormRect":
        if self.x + self.w > 1.0 + EPS or self.y + self.h > 1.0 + EPS:
            raise ValueError("rectangle exceeds normalized bounds (x+w and y+h must be <= 1)")
        return self


class NormPoint(StrictModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class PixelRect(StrictModel):
    left: int = Field(ge=-32768, le=32768)
    top: int = Field(ge=-32768, le=32768)
    width: int = Field(ge=16, le=16384)
    height: int = Field(ge=16, le=16384)


class AdbConfig(StrictModel):
    executable: str = Field(default="adb", min_length=1, max_length=512)
    serial: str = ""
    command_timeout_s: float = Field(default=2.0, ge=0.2, le=10.0)
    capture_timeout_s: float = Field(default=3.0, ge=0.5, le=15.0)

    @field_validator("executable")
    @classmethod
    def _exe(cls, v: str) -> str:
        if any(c in v for c in "\n\r\x00;|&`$<>"):
            raise ValueError("invalid characters in ADB executable path")
        return v

    @field_validator("serial")
    @classmethod
    def _serial(cls, v: str) -> str:
        if not SERIAL_RE.match(v):
            raise ValueError("invalid device serial")
        return v


class CaptureConfig(StrictModel):
    backend: Literal["synthetic", "replay", "mss", "adb"] = "synthetic"
    region: PixelRect = PixelRect(left=0, top=0, width=1280, height=720)
    replay_path: str = Field(default="", max_length=1024)
    replay_fps: float = Field(default=30.0, ge=1.0, le=240.0)
    replay_loop: bool = True
    target_fps: float = Field(default=30.0, ge=1.0, le=120.0)


class ProcessingParams(StrictModel):
    """Image-processing thresholds (how the mask is built)."""
    method: Literal["canny", "adaptive"] = "canny"
    blur_ksize: int = Field(default=5, ge=0, le=31)
    canny_low: int = Field(default=60, ge=0, le=255)
    canny_high: int = Field(default=160, ge=1, le=255)
    adaptive_block: int = Field(default=21, ge=3, le=99)
    adaptive_c: int = Field(default=7, ge=-50, le=50)
    morph: Literal["none", "open", "close", "dilate", "erode"] = "dilate"
    morph_ksize: int = Field(default=3, ge=1, le=25)
    morph_iterations: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def _check(self) -> "ProcessingParams":
        if self.blur_ksize not in (0, 1) and self.blur_ksize % 2 == 0:
            raise ValueError("blur_ksize must be 0 or odd")
        if self.adaptive_block % 2 == 0:
            raise ValueError("adaptive_block must be odd")
        if self.canny_low >= self.canny_high:
            raise ValueError("canny_low must be below canny_high")
        return self


class DetectionParams(StrictModel):
    """Detection thresholds (how the mask is judged)."""
    require: Literal["density", "contour", "either", "both"] = "both"
    min_density: float = Field(default=0.02, ge=0.0, le=1.0)
    min_area_frac: float = Field(default=0.02, ge=0.0, le=1.0)
    max_area_frac: float = Field(default=0.95, ge=0.0, le=1.0)
    min_width_frac: float = Field(default=0.05, ge=0.0, le=1.0)
    min_height_frac: float = Field(default=0.05, ge=0.0, le=1.0)
    min_aspect: float = Field(default=0.05, ge=0.0, le=100.0)
    max_aspect: float = Field(default=20.0, ge=0.01, le=100.0)
    center_y_min: float = Field(default=0.0, ge=0.0, le=1.0)
    center_y_max: float = Field(default=1.0, ge=0.0, le=1.0)
    confirm_frames: int = Field(default=2, ge=1, le=30)
    release_frames: int = Field(default=3, ge=1, le=30)

    @model_validator(mode="after")
    def _check(self) -> "DetectionParams":
        if self.min_area_frac > self.max_area_frac:
            raise ValueError("min_area_frac must be <= max_area_frac")
        if self.min_aspect > self.max_aspect:
            raise ValueError("min_aspect must be <= max_aspect")
        if self.center_y_min > self.center_y_max:
            raise ValueError("center_y_min must be <= center_y_max")
        return self


class RoiVision(StrictModel):
    processing: ProcessingParams = ProcessingParams()
    detection: DetectionParams = DetectionParams()


class VisionConfig(StrictModel):
    jump: RoiVision = RoiVision()
    slide: RoiVision = RoiVision()


class RoiConfig(StrictModel):
    # Uncalibrated placeholders: not verified game coordinates.
    jump_roi: NormRect = NormRect(x=0.30, y=0.62, w=0.12, h=0.20)
    slide_roi: NormRect = NormRect(x=0.30, y=0.30, w=0.12, h=0.25)


class DecisionConfig(StrictModel):
    jump_cooldown_ms: int = Field(default=350, ge=30, le=5000)
    slide_cooldown_ms: int = Field(default=350, ge=30, le=5000)
    detection_ttl_ms: int = Field(default=150, ge=10, le=3000)
    arbitration: Literal["prefer_jump", "prefer_slide", "suppress_both"] = "prefer_jump"
    max_frame_age_ms: int = Field(default=150, ge=10, le=3000)
    max_command_age_ms: int = Field(default=120, ge=10, le=3000)
    tap_ms: int = Field(default=40, ge=10, le=500)
    double_jump: bool = False
    double_jump_delay_ms: int = Field(default=120, ge=30, le=1000)
    slide_hold: bool = False
    slide_hold_ms: int = Field(default=450, ge=50, le=3000)


class InputConfig(StrictModel):
    backend: Literal["dryrun", "adb", "keyboard"] = "dryrun"
    # Uncalibrated placeholders; must be calibrated before real input.
    jump_point: NormPoint = NormPoint(x=0.12, y=0.85)
    slide_point: NormPoint = NormPoint(x=0.88, y=0.85)
    jump_key: str = "space"
    slide_key: str = "down"
    queue_size: int = Field(default=4, ge=1, le=64)

    @field_validator("jump_key", "slide_key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip().lower()
        if not KEY_RE.match(v):
            raise ValueError("key must be a PyDirectInput key name (a-z, 0-9)")
        return v


class PreviewConfig(StrictModel):
    fps: float = Field(default=10.0, ge=1.0, le=30.0)
    max_width: int = Field(default=640, ge=160, le=1920)
    jpeg_quality: int = Field(default=70, ge=30, le=95)


class CalibrationSignature(StrictModel):
    capture_backend: str
    frame_w: int = Field(ge=1)
    frame_h: int = Field(ge=1)
    input_backend: str
    display_w: Optional[int] = None
    display_h: Optional[int] = None
    confirmed_at: str = ""

    def matches(self, other: "CalibrationSignature") -> bool:
        a = self.model_dump(exclude={"confirmed_at"})
        b = other.model_dump(exclude={"confirmed_at"})
        return a == b


class SafetyConfig(StrictModel):
    dry_run: bool = True
    disconnect_policy: Literal["pause", "continue_background"] = "pause"
    stale_capture_ms: int = Field(default=1000, ge=100, le=30000)
    calibration: Optional[CalibrationSignature] = None


class GameStateConfig(StrictModel):
    enabled: bool = True
    guard_actions: bool = False
    sample_interval_ms: int = Field(default=200, ge=50, le=2000)
    confirm_frames: int = Field(default=2, ge=1, le=10)
    unknown_grace_ms: int = Field(default=1000, ge=0, le=10000)


class AppConfig(StrictModel):
    version: int = 1
    capture: CaptureConfig = CaptureConfig()
    adb: AdbConfig = AdbConfig()
    viewport: NormRect = NormRect(x=0.0, y=0.0, w=1.0, h=1.0)
    rois: RoiConfig = RoiConfig()
    vision: VisionConfig = VisionConfig()
    decision: DecisionConfig = DecisionConfig()
    input: InputConfig = InputConfig()
    preview: PreviewConfig = PreviewConfig()
    safety: SafetyConfig = SafetyConfig()
    game_state: GameStateConfig = GameStateConfig()


def calibration_key(cfg: AppConfig) -> tuple:
    """Fields whose change invalidates coordinate calibration."""
    return (
        cfg.capture.backend,
        cfg.capture.region.model_dump_json(),
        cfg.viewport.model_dump_json(),
        cfg.input.backend,
        cfg.input.jump_point.model_dump_json(),
        cfg.input.slide_point.model_dump_json(),
        cfg.adb.serial,
    )


def merge_client_config(current: AppConfig, incoming: AppConfig) -> AppConfig:
    """Apply a client-submitted config while protecting server-managed safety fields.

    Dry-run and calibration can only be changed through dedicated endpoints.
    Calibration is cleared when coordinate-affecting fields change.
    """
    safety = incoming.safety.model_copy(
        update={"dry_run": current.safety.dry_run, "calibration": current.safety.calibration}
    )
    merged = incoming.model_copy(update={"safety": safety})
    if calibration_key(merged) != calibration_key(current):
        merged = merged.model_copy(
            update={"safety": merged.safety.model_copy(update={"calibration": None})}
        )
    return AppConfig.model_validate(merged.model_dump())


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigStore:
    """Thread-safe holder of the active configuration with atomic persistence."""

    def __init__(self, path: Path = ACTIVE_CONFIG_PATH, profiles_dir: Path = PROFILES_DIR,
                 persist: bool = True) -> None:
        self.path = path
        self.profiles_dir = profiles_dir
        self.persist = persist
        self._lock = threading.Lock()
        self._config = self._load_initial()

    def _load_initial(self) -> AppConfig:
        for candidate in (self.path, self.profiles_dir / "default.json"):
            if self.persist and candidate.exists():
                try:
                    cfg = AppConfig.model_validate_json(candidate.read_text(encoding="utf-8"))
                    # Never start with real input enabled.
                    return cfg.model_copy(update={"safety": cfg.safety.model_copy(update={"dry_run": True})})
                except Exception:  # corrupt file -> fall back to defaults
                    continue
        return AppConfig()

    def get(self) -> AppConfig:
        with self._lock:
            return self._config

    def set(self, cfg: AppConfig) -> AppConfig:
        cfg = AppConfig.model_validate(cfg.model_dump())
        with self._lock:
            self._config = cfg
            if self.persist:
                atomic_write_json(self.path, cfg.model_dump(mode="json"))
        return cfg

    # ---- profiles -------------------------------------------------------
    @staticmethod
    def _check_name(name: str) -> str:
        if not PROFILE_NAME_RE.match(name):
            raise ValueError("profile name must match [A-Za-z0-9_-]{1,40}")
        return name

    def list_profiles(self) -> list[str]:
        if not self.profiles_dir.exists():
            return []
        return sorted(p.stem for p in self.profiles_dir.glob("*.json") if PROFILE_NAME_RE.match(p.stem))

    def save_profile(self, name: str) -> None:
        self._check_name(name)
        cfg = self.get()
        data = cfg.model_dump(mode="json")
        data["safety"]["dry_run"] = True
        atomic_write_json(self.profiles_dir / f"{name}.json", data)

    def read_profile(self, name: str) -> AppConfig:
        self._check_name(name)
        path = self.profiles_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(name)
        return AppConfig.model_validate_json(path.read_text(encoding="utf-8"))
