"""Platform layer — the ONE seam between SOC Ultralight and the OS desktop.

Everything OS-coupled (window find/focus/geometry, cursor, mouse-button state,
virtual-screen metrics, the injected-input hook, instance lock, console/app-id
plumbing) goes through the backend object returned by get_platform(). Porting
SOC to another OS means adding a backend module here — nothing else changes
(S8/S15 of the Linux-migration plan).

Backends:
  win32  — platform_win32.Win32Platform  (pywin32 + ctypes; the original code)
  x11    — platform_x11.X11Platform      (xdotool/wmctrl/EWMH; Linux, Xorg only)

Every backend implements the SAME method set (duck-typed; no ABC so a test
fake is just a plain class):

  find_windows() -> list[(handle, title)]
      Visible, titled, non-minimized top-level windows.
  window_from_point(x, y) -> (handle, title, class_name, rect) | None
      Root/top-level window under a screen point; rect = (l, t, r, b).
  focus_window(handle) -> bool
      Un-minimize + raise + give keyboard focus.
  get_window_rect(handle) -> (l, t, r, b) | None
  is_window(handle) -> bool
  move_window(handle, x, y, w, h) -> bool
  cursor_pos() -> (x, y)
  set_cursor_pos(x, y) -> None
  left_button_down() -> bool
      True while the physical left mouse button is held.
  virtual_screen() -> (x, y, w, h)
      Bounding box of ALL monitors (multi-monitor overlay support).
  install_input_hook(mark_operator) -> bool
      BLOCKING (run on a daemon thread): OS-level hook calling mark_operator()
      on every HUMAN (non-injected) input event. Return False when the
      platform can't do this — callers already run a cross-platform fallback.
  acquire_instance_lock(name) -> bool
      True if this process now holds the single-instance lock.
  hide_own_console() -> None
  set_app_id(app_id) -> None
      Taskbar identity (win32 AppUserModelID); no-op elsewhere.
"""

import sys

_backend = None


def get_platform():
    """The process-wide backend singleton, chosen by sys.platform."""
    global _backend
    if _backend is None:
        if sys.platform == "win32":
            from .platform_win32 import Win32Platform
            _backend = Win32Platform()
        else:
            from .platform_x11 import X11Platform
            _backend = X11Platform()
    return _backend


def set_platform(backend):
    """Test seam: inject a fake backend (pass None to reset to auto-detect)."""
    global _backend
    _backend = backend
