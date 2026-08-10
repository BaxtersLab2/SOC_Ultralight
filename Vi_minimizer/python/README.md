# Python bridge (`vi_bridge.py`)

A thin, dependency-free bridge that drives the `vi_minimizer` CLI from Python so
a GUI-automation program can run its swarm on an **isolated desktop** instead of
the operator's real one. Generic open infrastructure — no application logic.

## Quick start

```python
from vi_bridge import ViMinimizer

vi = ViMinimizer()                 # finds the exe via $VI_MINIMIZER_EXE / ../target / PATH
with vi.host("soc_vi") as host:    # isolated desktop, held for the session
    vi.run("soc_vi", ["notepad.exe"])      # launch an app onto it
    print(vi.list_windows("soc_vi"))       # health-check
    vi.shutdown("soc_vi")                   # tear the swarm down
# leaving the `with` releases the desktop
```

Locate the binary in priority order: `VI_MINIMIZER_EXE` env var →
`../target/{release,debug}/vi_minimizer.exe` → `vi_minimizer` on `PATH`.
Or pass it explicitly: `ViMinimizer(exe=r"...\vi_minimizer.exe")`.

Run the live demo (a benign notepad round-trip):

```console
set VI_MINIMIZER_EXE=...\vi_minimizer.exe
py -3 demo_lifecycle.py
```

## How an automation app runs itself isolated

A GUI-automation orchestrator drives apps with **synthetic input** (SendInput /
pyautogui) and **screen capture / OCR**. Both act on the *calling thread's*
desktop. So to drive agents on the isolated desktop, the orchestrator itself
must **run on that desktop** — you can't sit on Default and reach into the hidden
one.

The bootstrap pattern (the orchestrator wraps this with its own launch details):

1. Start a keeper so the desktop exists for the whole session:
   `host = vi.host("soc_vi")`
2. Launch the orchestrator process onto it:
   `vi.run("soc_vi", ["pythonw", "orchestrator.py", ...])`
   — everything *it* spawns (browsers, model UIs) inherits the same desktop
   automatically, because a child with no explicit desktop inherits its parent's.
3. The operator keeps using their real desktop. To glance at the swarm:
   `vi.switch("soc_vi")` … `vi.switch_back()`.
4. Health-check with `vi.list_windows("soc_vi")`; end the session with
   `vi.shutdown("soc_vi")` then `host.stop()`.

> Keep business logic in the orchestrator, not here. This bridge and the crate
> are open infrastructure; what runs *inside* the isolated desktop is yours.

## API

| Method | CLI | Notes |
|---|---|---|
| `host(desktop, shutdown_on_exit=False)` | `host` | Returns a `ViHost` keeper; `.stop()` releases (context-manager friendly) |
| `run(desktop, argv, wait=False, timeout_ms=)` | `run` | Create + launch onto the desktop |
| `list_windows(desktop)` | `list` | `[{hwnd, pid, title, visible}, ...]` |
| `shutdown(desktop)` | `shutdown` | Terminate every process with a window there (refuses `Default`) |
| `kill(pid)` | `kill` | Terminate one process |
| `switch(desktop)` / `switch_back()` | `switch` / `switch-back` | Peek at / leave the hidden desktop |
| `self_test()` / `version()` / `available()` | `self-test` / `version` | Diagnostics |

All raise `ViError` on `ok:false`.
