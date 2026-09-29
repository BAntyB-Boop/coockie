import json
import math

import pytest
from pydantic import ValidationError

from backend.app import Settings, validate_settings
from backend.config import (AppConfig, CalibrationSignature, NormPoint, NormRect, PixelRect,
                            merge_client_config)
from backend.geometry import (frame_to_desktop, frame_to_viewport_norm, roi_pixel_problems, roi_to_frame_px,
                              to_android_display, viewport_norm_to_frame, viewport_px)


def test_viewport_and_roi_transforms():
    vp = NormRect(x=0.1, y=0.2, w=0.5, h=0.5)
    v = viewport_px(vp, 1000, 800)
    assert (v.x, v.y, v.w, v.h) == (100, 160, 500, 400)
    r = roi_to_frame_px(NormRect(x=0.5, y=0.5, w=0.25, h=0.25), vp, 1000, 800)
    assert (r.x, r.y, r.w, r.h) == (350, 360, 125, 100)


def test_point_roundtrip():
    vp = NormRect(x=0.1, y=0.1, w=0.8, h=0.8)
    pt = NormPoint(x=0.25, y=0.75)
    fx, fy = viewport_norm_to_frame(pt, vp, 1280, 720)
    nx, ny = frame_to_viewport_norm(fx, fy, vp, 1280, 720)
    assert math.isclose(nx, 0.25, abs_tol=1e-3) and math.isclose(ny, 0.75, abs_tol=1e-3)


def test_frame_to_desktop_scales_region():
    region = PixelRect(left=100, top=50, width=1280, height=720)
    assert frame_to_desktop(640, 360, region, 1280, 720) == (740, 410)
    assert frame_to_desktop(320, 180, region, 640, 360) == (740, 410)  # downscaled frame


def test_android_mapping_device_capture_and_desktop_capture():
    vp = NormRect(x=0.0, y=0.0, w=1.0, h=1.0)
    pt = NormPoint(x=0.5, y=0.5)
    # ADB capture: frame == rotated display
    assert to_android_display(pt, vp, 2400, 1080, 2400, 1080, True) == (1200, 540)
    # Desktop capture of an emulator window: viewport covers the Android display
    vp2 = NormRect(x=0.1, y=0.1, w=0.8, h=0.8)
    assert to_android_display(NormPoint(x=1.0, y=1.0), vp2, 1000, 600, 1920, 1080, False) == (1919, 1079)
    with pytest.raises(ValueError):
        to_android_display(pt, vp, 100, 100, 0, 100, True)


@pytest.mark.parametrize("bad", [
    {"x": 0.9, "y": 0.0, "w": 0.2, "h": 0.2},      # exceeds right edge
    {"x": 0.0, "y": 0.0, "w": 0.01, "h": 0.5},     # undersized
    {"x": float("nan"), "y": 0.0, "w": 0.2, "h": 0.2},
    {"x": 0.0, "y": float("inf"), "w": 0.2, "h": 0.2},
    {"x": -0.1, "y": 0.0, "w": 0.2, "h": 0.2},
])
def test_invalid_rects_rejected(bad):
    with pytest.raises(ValidationError):
        NormRect(**bad)


def test_roi_pixel_minimum():
    roi = NormRect(x=0, y=0, w=0.02, h=0.02)
    assert roi_pixel_problems("jump", roi, NormRect(x=0, y=0, w=1, h=1), 200, 200)
    assert not roi_pixel_problems("jump", roi, NormRect(x=0, y=0, w=1, h=1), 2000, 2000)


def test_config_rejects_bad_params_and_json_nan():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"vision": {"jump": {"processing": {"canny_low": 200, "canny_high": 100}}}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"adb": {"serial": "abc; rm -rf /"}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        AppConfig.model_validate_json('{"decision": {"tap_ms": NaN}}')


def test_merge_protects_safety_fields_and_clears_calibration():
    cur = AppConfig()
    sig = CalibrationSignature(capture_backend="synthetic", frame_w=960, frame_h=540, input_backend="dryrun")
    cur = cur.model_copy(update={"safety": cur.safety.model_copy(update={"calibration": sig})})
    incoming = json.loads(cur.model_dump_json())
    incoming["safety"]["dry_run"] = False
    incoming["safety"]["calibration"] = None
    incoming["decision"]["jump_cooldown_ms"] = 500
    merged = merge_client_config(cur, AppConfig.model_validate(incoming))
    assert merged.safety.dry_run is True
    assert merged.safety.calibration is not None  # detection change keeps calibration
    assert merged.decision.jump_cooldown_ms == 500
    incoming["input"]["jump_point"] = {"x": 0.3, "y": 0.3}
    merged2 = merge_client_config(cur, AppConfig.model_validate(incoming))
    assert merged2.safety.calibration is None  # touch point change invalidates calibration


def test_store_atomic_and_forces_dry_run(tmp_store, tmp_path):
    cfg = tmp_store.get()
    unsafe = cfg.model_copy(update={"safety": cfg.safety.model_copy(update={"dry_run": False})})
    tmp_store.set(unsafe)
    assert json.loads((tmp_path / "config.json").read_text())["safety"]["dry_run"] is False
    from backend.config import ConfigStore
    reloaded = ConfigStore(path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    assert reloaded.get().safety.dry_run is True  # never starts with real input
    assert not list(tmp_path.glob("*.tmp"))
    tmp_store.save_profile("mine")
    assert "mine" in tmp_store.list_profiles()
    with pytest.raises(ValueError):
        tmp_store.save_profile("../evil")


def test_lan_settings_require_token_and_origin():
    assert validate_settings(Settings(host="0.0.0.0"))
    errs = validate_settings(Settings(host="0.0.0.0", allow_lan=True, token="short"))
    assert any("COOKIEBOT_TOKEN" in e for e in errs)
    assert any("allowed-origin" in e for e in errs)
    ok = Settings(host="0.0.0.0", allow_lan=True, token="x" * 32, extra_origins=["https://10.0.0.2:8765"])
    assert validate_settings(ok) == []
    assert validate_settings(Settings()) == []
