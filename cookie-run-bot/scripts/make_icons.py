"""Generate PWA icons (PNG) with OpenCV. Run by setup scripts when icons are missing."""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "frontend" / "icons"
BG = (42, 23, 15)  # #0f172a in BGR


def draw(size: int, maskable: bool) -> np.ndarray:
    img = np.zeros((size, size, 3), np.uint8)
    img[:] = BG
    s = size / 512.0
    pad = 0.0 if maskable else 24 * s
    if not maskable:
        img[:] = 0
        cv2.rectangle(img, (0, 0), (size - 1, size - 1), BG, -1)
    inner = 0.62 if maskable else 0.74
    c = size // 2
    r = int(size * inner / 2 - pad / 2)
    cv2.circle(img, (c, c), r, (70, 150, 214), -1, cv2.LINE_AA)       # cookie
    cv2.circle(img, (c, c), r, (40, 95, 150), max(2, int(10 * s)), cv2.LINE_AA)
    for dx, dy in ((-0.35, -0.3), (0.3, -0.38), (0.05, 0.05), (-0.4, 0.3), (0.38, 0.25), (0.0, 0.5)):
        cv2.circle(img, (int(c + dx * r), int(c + dy * r)), max(3, int(r * 0.09)), (30, 55, 90), -1, cv2.LINE_AA)
    # jump (green up) and slide (red down) arrows
    a = int(r * 0.42)
    up = np.array([[c - int(r * 0.55), c - a // 3], [c - int(r * 0.55) - a // 2, c + a // 2],
                   [c - int(r * 0.55) + a // 2, c + a // 2]], np.int32)
    dn = np.array([[c + int(r * 0.55), c + a // 3 + a // 2], [c + int(r * 0.55) - a // 2, c - a // 3],
                   [c + int(r * 0.55) + a // 2, c - a // 3]], np.int32)
    cv2.fillPoly(img, [up], (94, 197, 34), cv2.LINE_AA)
    cv2.fillPoly(img, [dn], (68, 68, 239), cv2.LINE_AA)
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = {"icon-192.png": (192, False), "icon-512.png": (512, False), "icon-maskable-512.png": (512, True)}
    for name, (size, mask) in targets.items():
        if not cv2.imwrite(str(OUT / name), draw(size, mask)):
            print(f"failed to write {name}", file=sys.stderr)
            return 1
    print(f"icons written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
