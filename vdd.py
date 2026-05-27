"""
vdd.py — Parsec Virtual Display Driver control for SOC Ultralight
==================================================================
Controls the virtual monitor created by parsec-vdd (MIT licence).
Requires one-time admin install via setup_vdd.bat.

After install, all commands run without elevation.

Usage:
    from vdd import VddController
    ctrl = VddController()
    ctrl.add(width=1920, height=2160)   # add virtual monitor
    ctrl.remove()                        # remove it
    ctrl.is_available()                  # True if vdd driver installed
"""

import subprocess, re, shutil


class VddController:
    DEFAULT_W = 1920
    DEFAULT_H = 2160

    def __init__(self):
        self._exe = shutil.which("vdd") or self._find_vdd()

    @staticmethod
    def _find_vdd():
        import os
        candidates = [
            r"C:\Program Files\Parsec\vdd.exe",
            r"C:\Program Files (x86)\Parsec\vdd.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def is_available(self) -> bool:
        return bool(self._exe)

    def version(self) -> str | None:
        if not self._exe:
            return None
        try:
            r = subprocess.run([self._exe, "-v"],
                               capture_output=True, text=True, timeout=5)
            return (r.stdout or r.stderr).strip()
        except Exception:
            return None

    def list_displays(self) -> list[int]:
        """Return list of active virtual display indices."""
        if not self._exe:
            return []
        try:
            r = subprocess.run([self._exe, "-l"],
                               capture_output=True, text=True, timeout=5)
            return [int(x) for x in re.findall(r"\d+", r.stdout)]
        except Exception:
            return []

    def add(self, width: int = DEFAULT_W, height: int = DEFAULT_H,
            refresh: int = 60) -> bool:
        """Add a virtual monitor at the given resolution. Returns True on success."""
        if not self._exe:
            return False
        try:
            r = subprocess.run([self._exe, "-a"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return False
            # Set resolution on the newly added display (index 1 by default)
            displays = self.list_displays()
            idx = str(displays[-1]) if displays else "1"
            subprocess.run(
                [self._exe, "set", idx, f"{width}x{height}@{refresh}"],
                capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def remove(self, index: int | None = None) -> bool:
        """Remove virtual monitor by index (or last one if None)."""
        if not self._exe:
            return False
        try:
            args = [self._exe, "-r"]
            if index is not None:
                args.append(str(index))
            r = subprocess.run(args, capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def remove_all(self) -> bool:
        if not self._exe:
            return False
        try:
            r = subprocess.run([self._exe, "-r", "all"],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False
