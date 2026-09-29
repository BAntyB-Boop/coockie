# Cookie Run Automation Bot & Local PWA Controller

Automates **jump and slide** inside a level, plus optional **Autoplay** menu navigation between levels
(see below). It never buys anything and never logs in. Real input is **off by default** (dry-run) and requires
calibration first.

Pipeline: `Capture → Latest Frame (1 slot) → Vision → Decision → Input worker (bounded queue)`.
Preview encoding and telemetry run separately and cannot block the control loop.

## Status of platforms

| Path | Status |
|------|--------|
| Synthetic scene / replay → vision → decision → dry-run | Tested (Linux, Python 3.11.2, 42 pytest tests) |
| Dashboard + REST + WebSockets (dry-run Start/Pause/Stop) | Tested via TestClient and a live server smoke test |
| Windows + emulator (MSS capture, ADB or keyboard input) | Implemented, **not verified on a real device/emulator** |
| macOS (MSS / ADB) | Implemented, not tested |
| Termux + Wireless ADB | Implemented, not tested |

## Install & run

**Windows** (primary): double-click or run `run_pc.bat`. It checks Python ≥ 3.10, creates `.venv`,
installs `backend/requirements-pc.txt`, verifies the imports, checks `adb` if an ADB backend is
selected, and prints `http://127.0.0.1:8765/`.

**macOS**: `./run_mac.sh`. PyDirectInput is not installed. Give your terminal
*Screen Recording* permission (System Settings → Privacy & Security) or MSS captures will be black.

**Termux**: `./run_termux.sh`. Installs `python-numpy` and `opencv-python` (x11-repo) with `pkg`,
then uses a `--system-site-packages` venv for FastAPI. Pair and connect Wireless ADB using the
host:port values shown on your phone. The dashboard's connect/pair form works too. Shizuku is not supported.

Manual: `python -m venv .venv`, `pip install -r backend/requirements-dev.txt`, `python -m backend.app`.
Tests: `python -m pytest -q`.

The server uses one Uvicorn worker, no reload, and binds to `127.0.0.1`.

## Calibration (required before real input)

1. Start the game level yourself. Choose a capture backend:
   - **ADB screencap** (`adb -s SERIAL exec-out screencap -p`): works for emulators and phones. Usually 5–15 fps.
   - **MSS desktop region**: faster. Set the capture rectangle to the emulator window. It only captures
     what's visible on screen, so obscured or minimized windows are not captured.
2. **Viewport**: drag it over the game area. With *MSS capture + ADB input* it must cover exactly the
   Android screen, because tap points are mapped viewport → display pixels.
3. **Jump ROI / Slide ROI**: put them where low and high obstacles appear just ahead of the cookie.
   Use the *Edges/Mask* view and the sliders until the green/red indicators fire only on obstacles.
4. **Tap points** (ADB) or **keys** (keyboard backend, Windows only; the emulator needs a key mapping and focus).
5. Use *Test jump/slide*. In dry-run these are only logged.
6. Click **Confirm calibration**. This stores the frame size, backends and Android display size.
   If any of those change, real input is blocked until you recalibrate. A resolution or orientation
   change while running pauses real input.
7. Turn off **DRY-RUN** (you'll be asked to confirm), then **Start**.

The shipped ROIs and tap points are placeholders, not verified game coordinates.

## Operation & safety

- States: `STOPPED → READY → RUNNING`, `PAUSED`, `ERROR`. Pause and Stop clear queued commands and
  release held keys.
- Device loss, stale frames (older than *stale capture limit*) and input failures switch to `ERROR`.
  Reconnecting never resumes input on its own.
- Emergency stop outside the dashboard: **F12** (Windows server hotkey), `python -m backend.estop`
  (creates `EMERGENCY_STOP`; clear it with `--clear`), or Ctrl+C in the server terminal.
- Dashboard disconnect policy: *Pause* (default) or *Keep running (background mode)*. Background mode is
  for a single Android device where you switch from the browser to the game.
- Double jump and slide hold are off by default and only apply if the input backend supports them.
  ADB cannot hold and release separately; slide hold uses a fixed-length `input swipe`. Killing
  the local adb process does not cancel a gesture Android has already started.

## Macro (Record & Replay Loop)

Records your exact gameplay timing in round 1, captures the restart click at the end, and saves it as **Loop 1**.
You can then replay Loop 1 automatically for any number of loops with auto-restart!

1. Start your level and click **● Record (Round 1)** (or press **F9**).
2. Play the game normally (Jump, Slide). The bot records the exact timestamps of every action.
3. When the round finishes, click the in-game **Restart / Retry** button (mouse click or restart key is captured).
4. Click **■ Stop & Save as Loop 1** (or press **F9** again).
5. Set **Target Loops** (e.g. 5, 10, or 0 for infinite) and click **▶ Play Loops** (or press **F10**).
6. The bot will automatically replay your gameplay, trigger restart, and repeat for the next loops!
7. Hotkeys:
   - **F9**: Toggle Record (Start / Stop & Save)
   - **F10**: Toggle Replay (Play / Stop)
   - **F12**: Emergency Stop

## Autoplay (menus, retries, item roll)

Reactive loop (~4 Hz): matches the current frame against screens **you teach**, taps through menus, runs the
vision bot in-level, and stops after N loops. Taught screens live in `data/screens/` (`screens.json` + PNG crops);
coordinates are capture-frame pixels. Teach from the dashboard (Autoplay card) or `POST /api/autoplay/screens`.
Matching is grayscale template matching (box +/- 24 px; `map` searches the whole frame); taps follow the found template.

| Role | Behaviour |
|---|---|
| `shop` | shop/purchase screen. Seen -> session **stops + alert**. Never tapped. |
| `danger` | shop / crystal / purchase / revive-with-crystals. Seen -> session **stops + alert**. Never tapped. |
| `no_hearts` | out-of-hearts popup: tap (close, never buy), then wait `heart_wait_min`. |
| `revive` | reports `REVIVE` and taps the taught safe choice (teach only the "No" button). |
| `ready` | reports `READY` and taps the taught Start/Play point. |
| `tap` | tap whenever seen (Play, popups, revive "No", exit-dialog "No"). |
| `result` | counts one loop per run; at N loops stops without tapping, else taps Retry. |
| `item_screen` | tap point = START. Rolls (`roll` entry) until a wanted `item` shows or `roll_cap` is hit. |
| `map_select` / `map` | finds the chosen `map` entry (optional vision profile) and taps it, else swipes (max 10). |
| `running` | in-level marker: vision bot on (off on every other screen). |

Priority when several match: `danger, no_hearts, tap, result, item_screen, map_select, running`.
Session options: `map`, `items`, `loops` (1-9999), `roll_cap` (0-100, default 10), `heart_wait_min` (12),
`unknown_back_s` (5; sends BACK once, alert + stop after 60 s on an unknown screen), `swipe` (x1,y1,x2,y2).
API: `POST /api/autoplay/start|stop`, `GET /api/autoplay/screens|match`, `status.autoplay` in `/api/status` and WS.

Safety: teach **every** crystal/shop/purchase screen as `danger` and revive "No" as `tap`. Real taps need the ADB
input backend, honour dry-run and the emergency-stop file; user Stop/Pause, F12, errors and dashboard disconnect
(policy pause) all end the session. Requires a taught `running` and `result` screen before start.

### Game State Detector

The same taught screens feed a debounced detector shared by the main bot. It reports `UNKNOWN`, `MENU`, `READY`,
`RUNNING`, `RESULT`, `REVIVE`, `SHOP`, `NO_HEARTS`, or `DANGER` in the dashboard and telemetry. Matching runs at a bounded rate,
requires consecutive confirmations, and keeps the last state briefly when a marker flickers.

Enable **Guard Jump / Slide** under *Backends & device* after teaching a `running` marker. In guard mode, Start
waits for a confirmed `RUNNING` state and queued or held automatic input is cleared as soon as a confirmed menu,
result, danger, or prolonged unknown screen appears. Detection defaults to observe-only so existing profiles
continue to work until the guard is deliberately enabled.

## LAN access (optional)

```
set COOKIEBOT_TOKEN=<random 24+ chars>
python -m backend.app --host 0.0.0.0 --allow-lan --allowed-origin https://192.168.1.20:8765 ^
       --ssl-certfile cert.pem --ssl-keyfile key.pem
```

The token goes in the `Authorization` header and the first WebSocket message. It never appears in URLs or logs.
Origins are validated. Use HTTPS (certificate or reverse proxy): Service Workers and safe token
transport need a secure context. Over plain-HTTP LAN the PWA won't install or cache.

## PWA

The PWA is only the dashboard. **It does not run Python**, so the server must be running. Static assets
are cached under a versioned cache and old caches are removed. APIs, live state and WebSockets are
never cached. When the backend is unreachable, a disconnected banner appears.

## Tools

- `python scripts/benchmark.py --backend synthetic|replay|mss|adb` reports median, p95, sample count,
  resolution and backend. `--adb-input --confirm-real-input` times adb command completion, which is not game response.
- `python scripts/replay.py video.mp4 --csv out.csv --video annotated.avi` replays a recording offline through vision and decision (dry-run).

## Known limitations

- The vision is a heuristic baseline (edges/threshold + contours + density + hysteresis). Scores are
  not probabilities. Synthetic and replay tests don't prove real-game accuracy.
- Timing uses calibrated trigger zones. Time-to-contact is not implemented (no object tracking).
- ADB screencap latency is often 60–200 ms per frame. Use MSS on desktop where possible.
- Keyboard input requires Windows and an emulator key mapping.
