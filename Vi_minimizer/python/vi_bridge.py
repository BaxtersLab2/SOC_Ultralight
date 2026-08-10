"""vi_bridge.py — thin Python bridge to the vi_minimizer CLI.

Lets any Python program (e.g. the SOC orchestrator) run its GUI swarm on an
isolated Win32 desktop: create / host a desktop, launch apps onto it, enumerate
its windows, and tear it down — without commandeering the operator's real
desktop.

This is generic open infrastructure that ships with the crate; it contains no
application/business logic. SOC wraps it with its own bootstrap.

Usage:
    from vi_bridge import ViMinimizer
    vi = ViMinimizer()                      # or ViMinimizer(exe=r"...\\vi_minimizer.exe")
    host = vi.host("soc_vi")                # keep an isolated desktop for the session
    vi.run("soc_vi", ["notepad.exe"])       # launch an app onto it
    print(vi.list_windows("soc_vi"))        # health-check
    vi.shutdown("soc_vi")                   # tear the swarm down
    host.stop()                             # release the desktop
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import List, Optional


class ViError(RuntimeError):
    """A vi_minimizer call returned ok:false, or its output was unusable."""


def _parse(out: str) -> dict:
    """The CLI prints exactly one JSON object; take the last non-empty line."""
    line = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    if not line:
        raise ViError(f"no output from vi_minimizer (got {out!r})")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise ViError(f"unparseable vi_minimizer output: {out!r}") from exc


class ViHost:
    """A live desktop keeper: a running `vi_minimizer host` child process.

    Keep this object alive for as long as the isolated desktop should exist.
    Call :meth:`stop` (or let it be garbage-collected) to release the desktop.
    """

    def __init__(self, proc: subprocess.Popen, desktop: str):
        self._proc = proc
        self.desktop = desktop

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def stop(self, timeout: float = 5.0) -> None:
        """Release the desktop by closing the keeper's stdin (EOF signal)."""
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
        # Close the remaining pipes so we don't leak file handles (ResourceWarning).
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass

    def __enter__(self) -> "ViHost":
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


class ViMinimizer:
    """Driver for the vi_minimizer CLI."""

    def __init__(self, exe: Optional[str] = None):
        self.exe = exe or self._find_exe()

    @staticmethod
    def _find_exe() -> str:
        """Locate vi_minimizer.exe: $VI_MINIMIZER_EXE, then ../target/{release,
        debug}, then rely on PATH."""
        env = os.environ.get("VI_MINIMIZER_EXE")
        if env and Path(env).exists():
            return env
        here = Path(__file__).resolve().parent
        for rel in ("release", "debug"):
            cand = here.parent / "target" / rel / "vi_minimizer.exe"
            if cand.exists():
                return str(cand)
        return "vi_minimizer"

    def _run(self, *cli_args: str, timeout: float = 60.0) -> dict:
        try:
            cp = subprocess.run(
                [self.exe, *cli_args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ViError(f"vi_minimizer.exe not found at {self.exe!r}") from exc
        data = _parse(cp.stdout or "")
        if not data.get("ok", False):
            raise ViError(data.get("error", f"vi_minimizer {cli_args} failed"))
        return data

    # -- one-shot commands --

    def version(self) -> dict:
        return self._run("version")

    def available(self) -> bool:
        try:
            self.version()
            return True
        except ViError:
            return False

    def self_test(self) -> dict:
        return self._run("self-test")

    def run(
        self,
        desktop: str,
        argv: List[str],
        wait: bool = False,
        timeout_ms: int = 30000,
        cwd: Optional[str] = None,
    ) -> dict:
        """Create `desktop` (idempotent under a host) and launch `argv` on it.

        `cwd` sets the launched process's working directory (needed for apps
        that resolve resources relative to it)."""
        args = ["run", desktop]
        if wait:
            args.append("--wait")
        args += ["--timeout", str(timeout_ms)]
        if cwd:
            args += ["--cwd", cwd]
        args += ["--", *argv]
        proc_timeout = (timeout_ms / 1000 + 10) if wait else 30.0
        return self._run(*args, timeout=proc_timeout)

    def list_windows(self, desktop: str) -> List[dict]:
        return self._run("list", desktop).get("windows", [])

    def shutdown(self, desktop: str) -> dict:
        return self._run("shutdown", desktop)

    def kill(self, pid: int) -> dict:
        return self._run("kill", str(pid))

    def switch(self, desktop: str) -> dict:
        return self._run("switch", desktop)

    def switch_back(self) -> dict:
        return self._run("switch-back")

    # -- persistent keeper --

    def host(
        self,
        desktop: str,
        shutdown_on_exit: bool = False,
        ready_timeout: float = 10.0,
    ) -> ViHost:
        """Start a persistent keeper for `desktop` and wait until it is holding.

        Returns a :class:`ViHost`; keep it alive for the session's duration.
        """
        args = [self.exe, "host", desktop]
        if shutdown_on_exit:
            args.append("--shutdown-on-exit")
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        line = proc.stdout.readline() if proc.stdout else ""
        data = _parse(line) if line.strip() else {}
        if data.get("status") != "holding":
            proc.terminate()
            raise ViError(f"host failed to start (got {line!r})")
        return ViHost(proc, desktop)
