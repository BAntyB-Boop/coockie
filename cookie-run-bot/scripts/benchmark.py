"""Measure capture, vision, decision and dry-run dispatch latency.

Examples:
  python scripts/benchmark.py --backend synthetic --frames 300
  python scripts/benchmark.py --backend replay --replay replays/run.mp4
  python scripts/benchmark.py --backend adb --serial emulator-5554 --frames 50
  python scripts/benchmark.py --backend mss --region 0,0,1280,720

ADB input timing (--adb-input, requires --confirm-real-input) measures local
`adb shell input` command completion only, NOT confirmed game response time.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.capture import ADBCapture, MSSCapture, ReplayCapture, SyntheticCapture  # noqa: E402
from backend.config import AdbConfig, AppConfig, CaptureConfig, PixelRect  # noqa: E402
from backend.decision_engine import DecisionEngine  # noqa: E402
from backend.input_handler import ADBInput, DryRunInput, InputContext  # noqa: E402
from backend.telemetry import percentile  # noqa: E402
from backend.vision_engine import VisionEngine  # noqa: E402


def stats(name: str, vals: list[float]) -> dict:
    return {"metric": name, "n": len(vals), "median_ms": round(percentile(vals, 50) or 0, 3),
            "p95_ms": round(percentile(vals, 95) or 0, 3)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", choices=["synthetic", "replay", "mss", "adb"], default="synthetic")
    p.add_argument("--frames", type=int, default=200)
    p.add_argument("--replay", default="")
    p.add_argument("--serial", default="")
    p.add_argument("--adb", default="adb")
    p.add_argument("--region", default="0,0,1280,720", help="left,top,width,height for mss")
    p.add_argument("--profile", default="", help="profile JSON for ROIs/vision parameters")
    p.add_argument("--adb-input", action="store_true", help="also time adb tap command completion")
    p.add_argument("--confirm-real-input", action="store_true", help="required with --adb-input (sends real taps)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    cfg = AppConfig.model_validate_json(Path(a.profile).read_text()) if a.profile else AppConfig()
    adb = AdbConfig(executable=a.adb, serial=a.serial)
    if a.backend == "synthetic":
        cap = SyntheticCapture()
    elif a.backend == "replay":
        cap = ReplayCapture(a.replay, loop=True)
    elif a.backend == "adb":
        cap = ADBCapture(adb)
    else:
        l, t, w, h = (int(x) for x in a.region.split(","))
        cap = MSSCapture(CaptureConfig(backend="mss", region=PixelRect(left=l, top=t, width=w, height=h)))
    cap.open()
    vision, decision, dry = VisionEngine(), DecisionEngine(), DryRunInput()
    cap_ms, vis_ms, dec_ms, disp_ms, adb_ms = [], [], [], [], []
    shape = None
    for i in range(a.frames):
        t0 = time.perf_counter()
        img = cap.render(i / 30.0) if isinstance(cap, SyntheticCapture) else cap.grab()
        t1 = time.perf_counter()
        shape = img.shape
        res = vision.process(i, time.monotonic(), img, cfg)
        t2 = time.perf_counter()
        out = decision.evaluate(res, time.monotonic(), True, cfg, dry.capabilities())
        t3 = time.perf_counter()
        ctx = InputContext(cfg, img.shape[1], img.shape[0], cap.is_device)
        for c in out.commands:
            d0 = time.perf_counter()
            dry.tap(c.action, ctx, c.tap_ms)
            disp_ms.append((time.perf_counter() - d0) * 1000)
        cap_ms.append((t1 - t0) * 1000)
        vis_ms.append((t2 - t1) * 1000)
        dec_ms.append((t3 - t2) * 1000)
    if a.adb_input:
        if not a.confirm_real_input:
            print("--adb-input sends REAL taps; add --confirm-real-input", file=sys.stderr)
            return 2
        inp = ADBInput(adb)
        ctx = InputContext(cfg, shape[1], shape[0], cap.is_device)
        for _ in range(10):
            t0 = time.perf_counter()
            inp.tap("jump", ctx, 40)
            adb_ms.append((time.perf_counter() - t0) * 1000)
    cap.close()
    report = {
        "backend": a.backend, "resolution": f"{shape[1]}x{shape[0]}" if shape else None, "frames": a.frames,
        "results": [stats("capture", cap_ms), stats("vision", vis_ms), stats("decision", dec_ms),
                    stats("dry_run_dispatch", disp_ms)]
                   + ([stats("adb_tap_command_completion (not game response)", adb_ms)] if adb_ms else []),
        "note": "synthetic/replay results do not establish real-game accuracy or latency",
    }
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"backend={report['backend']} resolution={report['resolution']} frames={a.frames}")
        for r in report["results"]:
            print(f"  {r['metric']:<52} n={r['n']:<5} median={r['median_ms']:>8.3f} ms  p95={r['p95_ms']:>8.3f} ms")
        print(f"  note: {report['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
