# vi_minimizer

**Isolated Win32 virtual-desktop host for the [SOC Ultralight](https://github.com/BaxtersLab) agent swarm.**

SOC Ultralight drives GUI apps with synthetic mouse/keyboard input and screen
OCR. Run directly, it takes over the operator's real desktop — you can't touch
your machine while the agents work. `vi_minimizer` carves out a **private
desktop object**, launches the swarm onto it, and leaves the operator's desktop
free. It's the step that turns SOC from an operator-attended tool into a
**headless background service**, driven by whatever trigger you attach to it.

> Open infrastructure, MIT-licensed — a public component of the Baxters open
> ecosystem (like GGUF-Chatbox and V_plugin). Any private business logic runs
> *on top of* this crate; none of it lives here.

## How isolation works

Windows has three relevant object layers:

- **Window station** (`WinSta0`) — owns the clipboard and a set of desktops.
- **Desktop** — owns windows, hooks, and the input queue. Every *thread* is
  bound to one desktop; `SendInput` / pyautogui act on the thread's desktop.
- **Active input desktop** — the single desktop the monitor shows and the
  physical mouse/keyboard drive. Changed with `SwitchDesktop`.

`vi_minimizer` calls `CreateDesktopW` to make a new desktop, then
`CreateProcessW` with `STARTUPINFO.lpDesktop` set to it, so the launched swarm —
and every window and click it makes — lands on that desktop instead of
"Default". The operator's Default desktop stays interactive.

## Status

**Milestone 1 — isolation core ✅**
- `VirtualDesktop::create / open / switch` + RAII `CloseDesktop` on drop.
- `launch_on_desktop(name, argv)` → `LaunchedProcess` with `wait` / `terminate`
  (and `wait_for_ready`, so a fire-and-hold launch doesn't race the child into
  desktop destruction).

**Milestone 2 — lifecycle + SOC wiring ✅**
- `list_windows(desktop)` (hwnd/pid/title/visible) for health-checks.
- `shutdown_desktop(desktop)` — terminate every process with a window there;
  **refuses `Default`** so it can never nuke the operator's own apps.
- `host` — persistent desktop keeper (holds the desktop until stdin closes), so
  a session survives many launches.
- A dependency-free **Python bridge** (`python/vi_bridge.py`) for SOC to drive
  the CLI via `subprocess`; see [`python/README.md`](python/README.md).

Verified: `cargo test` (13 pass / 0 fail) including live integration tests that
create a real desktop, launch a GUI child on it, enumerate it, and tear it down;
plus a live Python round-trip.

```console
$ vi_minimizer self-test
{"ok":true,"action":"self-test","desktop":"vi_selftest","pid":1234,"result":"PASS", ...}

$ vi_minimizer run soc_vi -- notepad.exe
{"ok":true,"action":"run","desktop":"soc_vi","pid":5678,"waited":false}

$ vi_minimizer list soc_vi
{"ok":true,"action":"list","desktop":"soc_vi","count":1,"windows":[{"hwnd":..,"pid":5678,"visible":true,"title":"Untitled - Notepad"}]}

$ vi_minimizer shutdown soc_vi
{"ok":true,"action":"shutdown","desktop":"soc_vi","terminated":[5678],"failed":[]}

$ vi_minimizer switch soc_vi      # peek at the hidden desktop
$ vi_minimizer switch-back        # return to Default
```

## The "thumb" — a known Windows constraint (Milestone 2)

The vision is a small always-on-top **thumbnail** of the hidden desktop on the
operator's real screen, so they can watch the swarm without switching to it.
This is genuinely non-trivial on Windows:

- **DWM thumbnails** (`DwmRegisterThumbnail`) only mirror windows that DWM is
  compositing — i.e. windows on the **active** desktop. A background
  `CreateDesktop` desktop is not composited, so it has no DWM thumbnail.
- **DXGI Desktop Duplication** duplicates the output currently displayed — again
  the **active** desktop only. It cannot duplicate a background desktop.

So "hidden desktop" and "live DWM/DXGI thumbnail" are mutually exclusive as-is.
Candidate resolutions to choose from in M2:

1. **Per-window `PrintWindow` capture** of the swarm's windows on the hidden
   desktop. Works for plain Win32/Tk windows (SOC's own transcript/monitor);
   unreliable/black for GPU-accelerated Chromium (Edge/Copilot). Cheapest.
2. **Nested session** (RDP loopback / a second interactive session with its own
   virtual display that *can* be duplicated). Heavier, but a real live thumb.
3. **Lightweight VM** with a virtual display — strongest isolation, but GPU
   passthrough for the local vision models (V-plugin/GGUF) is the hard part.

Milestone 1 deliberately ships the isolation value first (input no longer
touches the operator's desktop); the operator can `switch`/`switch-back` to look.
The thumb strategy is decided next.

## SOC integration

- **Now:** SOC (Python) drives the CLI through the bridge in
  [`python/`](python/README.md) — `host` an isolated desktop, launch the swarm
  onto it, `list`/`shutdown` to manage it. Each call returns one JSON line.
- **Later:** optional PyO3 bindings so SOC calls the library in-process.

## Build

```console
cargo build --release
cargo test            # unit + live isolation integration test (Windows)
```

Windows-only (`x86_64-pc-windows-msvc`). Uses the official [`windows`](https://crates.io/crates/windows) crate.
