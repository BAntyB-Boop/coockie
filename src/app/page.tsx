export const dynamic = "force-static";

const files = [
  ["backend/", "FastAPI server, capture (MSS / ADB / replay / synthetic), vision, decision, input, telemetry"],
  ["frontend/", "Vanilla JS dashboard + PWA (manifest, service worker, icons)"],
  ["profiles/default.json", "Default (uncalibrated) profile - dry-run on"],
  ["tests/", "42 pytest tests: geometry, config, decision, input worker, controller, API, WebSockets"],
  ["scripts/", "benchmark.py, replay.py, make_icons.py, Termux requirements"],
  ["run_pc.bat / run_mac.sh / run_termux.sh", "Platform launchers (venv, dependency checks, server start)"],
];

export default function HomePage() {
  return (
    <main className="min-h-screen bg-slate-950 px-6 py-12 text-slate-200">
      <section className="mx-auto max-w-3xl space-y-8">
        <header className="space-y-3">
          <p className="text-sm uppercase tracking-widest text-slate-400">Project delivered in ./cookie-run-bot</p>
          <h1 className="text-4xl font-semibold text-white">Cookie Run Automation Bot &amp; Local PWA Controller</h1>
          <p className="text-slate-400">
            The bot is a local Python application (FastAPI + OpenCV). It runs on your own Windows PC with an Android
            emulator, not on this preview host. Real input starts disabled (dry-run) and requires calibration first.
          </p>
          <div className="flex gap-3 text-sm font-semibold">
            <span className="rounded-lg bg-green-600/20 px-3 py-1 text-green-400">JUMP</span>
            <span className="rounded-lg bg-red-600/20 px-3 py-1 text-red-400">SLIDE</span>
          </div>
        </header>

        <div className="rounded-2xl border border-slate-800 bg-slate-900 p-6">
          <h2 className="mb-3 text-sm uppercase tracking-wider text-slate-400">Run it</h2>
          <pre className="overflow-x-auto rounded-lg bg-slate-950 p-4 text-sm text-sky-300">
{`cd cookie-run-bot
run_pc.bat                 # Windows (primary)
./run_mac.sh               # macOS
./run_termux.sh            # Android / Termux
# then open http://127.0.0.1:8765/`}
          </pre>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-900 p-6">
          <h2 className="mb-3 text-sm uppercase tracking-wider text-slate-400">Contents</h2>
          <ul className="space-y-2 text-sm">
            {files.map(([name, desc]) => (
              <li key={name} className="flex flex-col gap-1 sm:flex-row sm:gap-4">
                <code className="shrink-0 text-sky-300 sm:w-72">{name}</code>
                <span className="text-slate-400">{desc}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </main>
  );
}
