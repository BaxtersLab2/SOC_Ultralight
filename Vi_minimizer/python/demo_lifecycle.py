"""Live demo of the Python -> vi_minimizer integration.

Drives the CLI from Python to run a benign GUI app (notepad) on an isolated
desktop, enumerate it, and tear it down — the exact path SOC uses, minus SOC's
own bootstrap. Prints each step so it doubles as a smoke test.

Run:
    set VI_MINIMIZER_EXE=...\\vi_minimizer.exe   (or rely on ../target/*)
    python demo_lifecycle.py
"""

import sys
import time

from vi_bridge import ViMinimizer, ViError

DESKTOP = "vi_py_demo"


def main() -> int:
    vi = ViMinimizer()
    print(f"exe        : {vi.exe}")
    print(f"version    : {vi.version()}")
    print(f"self-test  : {vi.self_test()}")

    host = vi.host(DESKTOP)
    print(f"hosting    : {host.desktop!r} (keeper alive={host.alive})")
    try:
        vi.run(DESKTOP, ["notepad.exe"])
        print(f"launched   : notepad.exe on {DESKTOP!r}")

        # Wait for the window to appear.
        wins = []
        for _ in range(30):
            wins = vi.list_windows(DESKTOP)
            if any(w["pid"] for w in wins):
                break
            time.sleep(0.1)

        print(f"windows    : {len(wins)} on {DESKTOP!r}")
        for w in wins:
            vis = "visible" if w["visible"] else "hidden"
            print(f"    pid={w['pid']:>6}  {vis:7}  {w['title']!r}")

        report = vi.shutdown(DESKTOP)
        print(f"shutdown   : terminated={report['terminated']} failed={report['failed']}")
    finally:
        host.stop()
        print(f"released   : keeper alive={host.alive}")

    print("OK: Python drove the full isolated lifecycle.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ViError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
