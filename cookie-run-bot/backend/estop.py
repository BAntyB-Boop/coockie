"""Emergency stop from outside the dashboard.

    python -m backend.estop          # create EMERGENCY_STOP -> the bot stops within ~100 ms
    python -m backend.estop --clear  # remove it so Start is allowed again
"""
from __future__ import annotations

import sys

from .config import ESTOP_FILE


def main(argv: list[str]) -> int:
    if "--clear" in argv:
        ESTOP_FILE.unlink(missing_ok=True)
        print(f"removed {ESTOP_FILE}")
    else:
        ESTOP_FILE.write_text("stop\n", encoding="utf-8")
        print(f"created {ESTOP_FILE}; the bot will stop and refuse to start until cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
