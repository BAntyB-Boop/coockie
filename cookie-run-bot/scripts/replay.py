"""Offline replay: run vision + decision (dry-run) over a recorded video.

  python scripts/replay.py replays/run.mp4 --profile profiles/default.json --csv out.csv --video out.avi
  python scripts/replay.py --synthetic --seconds 10

Timing uses the video's own frame timestamps (fake clock), so decisions are
reproducible. Results describe this recording only, not real-game accuracy.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from backend.capture import SyntheticCapture  # noqa: E402
from backend.config import AppConfig  # noqa: E402
from backend.decision_engine import DecisionEngine  # noqa: E402
from backend.input_handler import DryRunInput  # noqa: E402
from backend.vision_engine import VisionEngine, render_view  # noqa: E402


def frames_from(args):
    if args.synthetic:
        syn = SyntheticCapture()
        n = int(args.seconds * args.fps)
        for i in range(n):
            yield i + 1, i / args.fps, syn.render(i / args.fps)
        return
    cap = cv2.VideoCapture(args.video_in)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {args.video_in}")
    fps = cap.get(cv2.CAP_PROP_FPS) or args.fps
    i = 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        i += 1
        yield i, (i - 1) / fps, img
    cap.release()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("video_in", nargs="?", default="")
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--profile", default="")
    p.add_argument("--csv", default="")
    p.add_argument("--video", default="", help="write annotated MJPG .avi")
    a = p.parse_args()
    if not a.synthetic and not a.video_in:
        p.error("give a video path or --synthetic")
    cfg = AppConfig.model_validate_json(Path(a.profile).read_text()) if a.profile else AppConfig()
    vision, decision, caps = VisionEngine(), DecisionEngine(), DryRunInput().capabilities()
    writer = None
    rows = []
    counts = {"jump": 0, "slide": 0}
    for fid, ts, img in frames_from(a):
        res = vision.process(fid, ts, img, cfg)
        out = decision.evaluate(res, ts, True, cfg, caps)  # fake clock = video timestamp
        for c in out.commands:
            counts[c.action] += 1
        rows.append({"frame": fid, "t_s": round(ts, 4),
                     "jump_conf": int(res.jump.confirmed), "jump_density": round(res.jump.density, 4),
                     "jump_score": round(res.jump.score, 3), "slide_conf": int(res.slide.confirmed),
                     "slide_density": round(res.slide.density, 4), "slide_score": round(res.slide.score, 3),
                     "actions": "+".join(c.action for c in out.commands),
                     "suppressed": ";".join(f"{k}:{v}" for k, v in out.suppressed.items())})
        if a.video:
            view = render_view("annotated", img, res, cfg, img.shape[1])
            if writer is None:
                writer = cv2.VideoWriter(a.video, cv2.VideoWriter_fourcc(*"MJPG"), a.fps, (view.shape[1], view.shape[0]))
            writer.write(view)
    if writer is not None:
        writer.release()
    if a.csv and rows:
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"frames={len(rows)} jump_actions={counts['jump']} slide_actions={counts['slide']} (dry-run, this recording only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
