"""isolated_launcher.py — run a GUI program on an isolated desktop, safely.

Hosts an isolated Win32 desktop, launches a payload command onto it, and keeps a
control surface **on the operator's real desktop** so the payload can always be
health-checked and torn down — it never gets stranded on an invisible desktop.

    py -3 isolated_launcher.py soc_vi --exe ...\\vi_minimizer.exe -- pythonw soc_ultralight.py

Safety model:
- The launcher (and its console) run on the operator's Default desktop.
- `switch`/peek would take over the whole screen (a CreateDesktop desktop has no
  built-in switch-back UI), so `peek` is opt-in and auto-returns via a background
  timer that fires regardless of which desktop is active.
- Teardown (shutdown payload + release desktop) runs on exit no matter how the
  launcher ends (quit, Ctrl-C, EOF).

Generic open infrastructure — no application logic. SOC wraps it with its own
command line.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import List, Optional

from vi_bridge import ViError, ViMinimizer


def _pid_alive(pid: int) -> bool:
    """True if `pid` refers to a still-running process."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


class IsolatedSession:
    """A payload running on a hosted isolated desktop, with safe teardown."""

    def __init__(
        self,
        vi: ViMinimizer,
        desktop: str,
        argv: List[str],
        cwd: Optional[str] = None,
    ):
        self.vi = vi
        self.desktop = desktop
        self.argv = argv
        self.cwd = cwd
        self._host = None
        self.payload_pid: Optional[int] = None
        self._peek_timer: Optional[threading.Timer] = None
        self._torn_down = False

    def start(self) -> "IsolatedSession":
        self._host = self.vi.host(self.desktop)
        result = self.vi.run(self.desktop, self.argv, cwd=self.cwd)
        self.payload_pid = result.get("pid")
        return self

    def health(self) -> List[dict]:
        try:
            return self.vi.list_windows(self.desktop)
        except ViError:
            return []

    def payload_alive(self) -> bool:
        return self.payload_pid is not None and _pid_alive(self.payload_pid)

    def peek(self, seconds: float = 8.0) -> None:
        """Switch the screen to the isolated desktop, auto-returning after
        `seconds`. The timer thread fires even while the other desktop is active,
        so the operator is guaranteed to come back."""
        self.vi.switch(self.desktop)
        if self._peek_timer:
            self._peek_timer.cancel()
        self._peek_timer = threading.Timer(seconds, self._auto_return)
        self._peek_timer.daemon = True
        self._peek_timer.start()

    def _auto_return(self) -> None:
        try:
            self.vi.switch_back()
        except ViError:
            pass

    def teardown(self) -> dict:
        if self._torn_down:
            return {"terminated": []}
        self._torn_down = True
        if self._peek_timer:
            self._peek_timer.cancel()
        # Make sure we're back on Default before killing the desktop.
        try:
            self.vi.switch_back()
        except ViError:
            pass
        report = {"terminated": []}
        try:
            report = self.vi.shutdown(self.desktop)
        except ViError:
            pass
        if self._host:
            self._host.stop()
        return report

    def __enter__(self) -> "IsolatedSession":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.teardown()


def selftest(exe: Optional[str]) -> int:
    """Headless lifecycle check with a benign payload (notepad). No peek."""
    vi = ViMinimizer(exe)
    desktop = "vi_launch_selftest"
    session = IsolatedSession(vi, desktop, ["notepad.exe"])
    with session:
        pid = session.payload_pid
        assert pid, "payload should have a pid"
        found = False
        for _ in range(30):
            if any(w["pid"] == pid for w in session.health()):
                found = True
                break
            time.sleep(0.1)
        assert found, "payload window should appear on the isolated desktop"
        assert session.payload_alive(), "payload should be alive"
        print(f"  hosted {desktop!r}, payload pid {pid} healthy")
    # After the context exits, teardown has run.
    assert not _pid_alive(pid), "payload should be gone after teardown"
    print("SELFTEST PASS: host -> launch -> health -> teardown, payload cleaned up")
    return 0


def _usage() -> None:
    print(
        "usage: isolated_launcher.py <desktop> [--exe PATH] [--cwd DIR] "
        "[--selftest] [--peek-seconds N] -- <cmd> [args...]",
        file=sys.stderr,
    )


def main() -> int:
    argv = sys.argv[1:]
    payload: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        head, payload = argv[:i], argv[i + 1 :]
    else:
        head = argv

    exe: Optional[str] = None
    cwd: Optional[str] = None
    peek_seconds = 8.0
    do_selftest = False
    positional: List[str] = []
    idx = 0
    while idx < len(head):
        tok = head[idx]
        if tok == "--exe":
            exe = head[idx + 1]
            idx += 2
        elif tok == "--cwd":
            cwd = head[idx + 1]
            idx += 2
        elif tok == "--peek-seconds":
            peek_seconds = float(head[idx + 1])
            idx += 2
        elif tok == "--selftest":
            do_selftest = True
            idx += 1
        else:
            positional.append(tok)
            idx += 1

    if do_selftest:
        return selftest(exe)

    if not positional or not payload:
        _usage()
        return 2

    desktop = positional[0]
    vi = ViMinimizer(exe)
    session = IsolatedSession(vi, desktop, payload, cwd=cwd)
    session.start()
    print(f"launched {payload} on isolated desktop {desktop!r} (pid {session.payload_pid})")
    print("commands:  h=health   p=peek (takes over screen, auto-returns)   s=shutdown+quit")
    try:
        while True:
            try:
                cmd = input("iso> ").strip().lower()
            except EOFError:
                break
            if cmd in ("s", "q", "quit", "exit"):
                break
            elif cmd == "h":
                wins = session.health()
                alive = "yes" if session.payload_alive() else "NO (payload exited)"
                print(f"  payload alive: {alive};  windows on {desktop!r}: {len(wins)}")
                for w in wins:
                    if w["title"]:
                        print(f"    pid={w['pid']}  {w['title']!r}")
            elif cmd == "p":
                print(f"  peeking for {peek_seconds:g}s (screen will switch, then auto-return)...")
                session.peek(peek_seconds)
            elif cmd:
                print("  unknown command; use h / p / s")
    except KeyboardInterrupt:
        pass
    finally:
        report = session.teardown()
        print(f"torn down {desktop!r}; terminated {report.get('terminated', [])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ViError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
