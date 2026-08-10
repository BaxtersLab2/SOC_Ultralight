"""X11 backend (S15) — SOC's platform seam on Linux/Xorg.

Implements the platform_layer interface with python-xlib + python-ewmh.
Xorg ONLY (Wayland breaks the whole OCR/inject stack — the OS ships Xorg
sessions, and the Podman proving ground runs Xvfb).

Design notes:
- EWMH (_NET_CLIENT_LIST*) is the primary window source; where no window
  manager is running (bare Xvfb), fall back to raw query_tree enumeration of
  viewable top-levels so tests still exercise the code path.
- Handles are X window IDs (ints), mirroring win32 hwnds; every public method
  accepts either the raw id or an Xlib window object.
- install_input_hook returns False: SOC's cross-platform position-watcher
  fallback (pyautogui-based) is the attribution detector on Linux. XRecord is
  a future upgrade, not a v1 requirement.
"""

import os
import socket


class X11Platform:
    name = "x11"

    def __init__(self):
        self._display = None
        self._ewmh = None
        self._lock_sock = None   # instance lock, held for process lifetime

    # ── Connection plumbing ───────────────────────────────────────────────────

    def _dpy(self):
        if self._display is None:
            from Xlib import display
            self._display = display.Display()
        return self._display

    def _wm(self):
        if self._ewmh is None:
            from ewmh import EWMH
            self._ewmh = EWMH(self._dpy())
        return self._ewmh

    def _resolve(self, handle):
        """Raw window id → Xlib window object (objects pass through)."""
        if handle is None:
            return None
        if isinstance(handle, int):
            try:
                return self._dpy().create_resource_object("window", handle)
            except Exception:
                return None
        return handle

    @staticmethod
    def _wid(win):
        """Xlib window object → raw id (ints pass through)."""
        return win if isinstance(win, int) else getattr(win, "id", None)

    def _title(self, win):
        """_NET_WM_NAME (UTF-8) with WM_NAME fallback; '' when unnamed."""
        try:
            name = self._wm().getWmName(win)
            if name:
                return name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
        except Exception:
            pass
        try:
            name = win.get_wm_name()
            return name if isinstance(name, str) else (name or b"").decode("latin-1", "replace")
        except Exception:
            return ""

    def _abs_rect(self, win):
        """Absolute (l, t, r, b) of a window via translate_coords to root."""
        try:
            geo = win.get_geometry()
            root = self._dpy().screen().root
            # dst.translate_coords(src, 0, 0): src's origin in dst coords —
            # so root←win gives the window's absolute origin directly.
            pos = root.translate_coords(win, 0, 0)
            x, y = pos.x, pos.y
            return x, y, x + geo.width, y + geo.height
        except Exception:
            return None

    def _clients_stacked(self):
        """Top-level client windows, bottom→top. EWMH first, raw fallback."""
        try:
            wins = self._wm().getClientListStacking()
            if wins:
                return list(wins)
        except Exception:
            pass
        try:
            wins = self._wm().getClientList()
            if wins:
                return list(wins)
        except Exception:
            pass
        # No WM (bare Xvfb): viewable direct children of root, in tree order
        # (query_tree returns bottom→top stacking, same convention).
        out = []
        try:
            from Xlib import X
            for w in self._dpy().screen().root.query_tree().children:
                try:
                    if w.get_attributes().map_state == X.IsViewable:
                        out.append(w)
                except Exception:
                    pass
        except Exception:
            pass
        return out

    # ── Window ops ────────────────────────────────────────────────────────────

    def find_windows(self):
        """Visible, titled top-level windows → [(window_id, title)]."""
        wins = []
        for w in self._clients_stacked():
            title = self._title(w)
            if not title:
                continue
            try:
                hidden = False
                try:
                    state = self._wm().getWmState(w, str=True) or []
                    hidden = "_NET_WM_STATE_HIDDEN" in state
                except Exception:
                    pass
                if not hidden:
                    wins.append((self._wid(w), title))
            except Exception:
                pass
        wins.reverse()   # topmost first, matching operator expectation
        return wins

    def window_from_point(self, x, y):
        """Topmost client containing (x, y) → (id, title, class, rect) or None."""
        x, y = int(x), int(y)
        for w in reversed(self._clients_stacked()):   # top→bottom
            rect = self._abs_rect(w)
            if not rect:
                continue
            l, t, r, b = rect
            if l <= x < r and t <= y < b:
                cls = ""
                try:
                    ch = w.get_wm_class()
                    cls = ch[1] if ch else ""
                except Exception:
                    pass
                return self._wid(w), self._title(w), cls, rect
        return None

    def focus_window(self, handle) -> bool:
        """Activate (un-minimize + raise + focus). EWMH activate AND raw
        raise/focus both run — belt and suspenders, since WMs vary in which
        request they honor promptly (openbox needed the raw reinforcement)."""
        win = self._resolve(handle)
        if win is None:
            return False
        ok = False
        try:
            self._wm().setActiveWindow(win)
            self._dpy().flush()
            ok = True
        except Exception:
            pass
        try:
            from Xlib import X
            win.map()                      # un-iconify under no-WM
            win.configure(stack_mode=X.Above)
            win.set_input_focus(X.RevertToParent, X.CurrentTime)
            self._dpy().flush()
            ok = True
        except Exception:
            pass
        return ok

    def get_window_rect(self, handle):
        win = self._resolve(handle)
        return self._abs_rect(win) if win is not None else None

    def is_window(self, handle) -> bool:
        win = self._resolve(handle)
        if win is None:
            return False
        try:
            win.get_geometry()
            return True
        except Exception:
            return False

    def move_window(self, handle, x, y, w, h) -> bool:
        win = self._resolve(handle)
        if win is None:
            return False
        try:
            try:
                self._wm().setMoveResizeWindow(
                    win, gravity=0, x=int(x), y=int(y), w=int(w), h=int(h))
            except Exception:
                win.configure(x=int(x), y=int(y), width=int(w), height=int(h))
            self._dpy().flush()
            return True
        except Exception:
            return False

    # ── Cursor / mouse ────────────────────────────────────────────────────────

    def cursor_pos(self):
        p = self._dpy().screen().root.query_pointer()
        return p.root_x, p.root_y

    def set_cursor_pos(self, x, y):
        root = self._dpy().screen().root
        root.warp_pointer(int(x), int(y))
        self._dpy().flush()

    def left_button_down(self) -> bool:
        from Xlib import X
        p = self._dpy().screen().root.query_pointer()
        return bool(p.mask & X.Button1Mask)

    # ── Screen ────────────────────────────────────────────────────────────────

    def virtual_screen(self):
        """Root window geometry = the multi-monitor bounding box on X11."""
        geo = self._dpy().screen().root.get_geometry()
        return 0, 0, geo.width, geo.height

    # ── Injected-input hook ───────────────────────────────────────────────────

    def install_input_hook(self, mark_operator) -> bool:
        """Not implemented on X11 v1 — SOC's position-watcher fallback covers
        attribution (XRecord is the future upgrade). Returning False makes the
        caller start that fallback, exactly like a failed win32 hook."""
        return False

    # ── Process / app plumbing ────────────────────────────────────────────────

    def acquire_instance_lock(self, name: str) -> bool:
        """Abstract-namespace unix socket: self-releasing on process death."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind("\0" + name)
            self._lock_sock = s   # keep a reference for the process lifetime
            return True
        except OSError:
            return False
        except Exception:
            return True   # platform without AF_UNIX abstract ns: don't block startup

    def hide_own_console(self):
        pass   # no attached-console concept for GUI launches on Linux

    def set_app_id(self, app_id: str):
        pass   # taskbar identity comes from the .desktop file on Linux
