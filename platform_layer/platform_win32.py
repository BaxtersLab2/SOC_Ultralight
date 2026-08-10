"""Win32 backend — the original SOC platform code, moved here unchanged (S8).

pywin32 + ctypes, all imported lazily exactly as the in-line code did, so
importing this module costs nothing and never fails on a box without pywin32
(the failure surfaces at call time, same as before the seam).
"""

import time


class Win32Platform:
    name = "win32"

    # ── Window ops ────────────────────────────────────────────────────────────

    def find_windows(self):
        """Visible, titled, non-minimized top-level windows → [(hwnd, title)]."""
        import win32gui
        wins = []
        win32gui.EnumWindows(
            lambda hwnd, lst: lst.append((hwnd, win32gui.GetWindowText(hwnd)))
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)
                and not win32gui.IsIconic(hwnd)
                else True,
            wins)
        return wins

    def window_from_point(self, x, y):
        """Root window under a screen point → (hwnd, title, class, rect) or None."""
        import win32gui, win32con
        try:
            hwnd = win32gui.WindowFromPoint((int(x), int(y)))
            root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd
            if not root:
                return None
            title = win32gui.GetWindowText(root) or ""
            cls = win32gui.GetClassName(root) or ""
            rect = win32gui.GetWindowRect(root)
            return root, title, cls, rect
        except Exception:
            return None

    def focus_window(self, hwnd) -> bool:
        """Bring `hwnd` to the foreground, defeating Windows' foreground lock.

        A plain SetForegroundWindow() fails with error 0 ("No error message is
        available") whenever our process isn't already the foreground app —
        which is exactly the case when focusing another window to read or
        inject, so the relay's read leg was silently failing while this
        reported success. Escalate through the documented work-arounds,
        stopping as soon as the target is actually frontmost:
          1. Un-minimise / show the window (SW_SHOW keeps a maximised window
             maximised; a blind SW_RESTORE un-maximised it).
          2. Zero the system foreground-lock timeout.
          3. Tap ALT so Windows treats this thread as having "just received
             input" — the documented allowance for changing the foreground.
          4. Attach our input queue to the target's (and current foreground's)
             thread, set foreground, then detach.
        Returns True iff `hwnd` is genuinely foreground afterwards.
        """
        if not hwnd:
            return False
        import ctypes
        try:
            import win32gui, win32con
        except Exception:
            return False

        def _is_fg() -> bool:
            try:
                return win32gui.GetForegroundWindow() == hwnd
            except Exception:
                return False

        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        except Exception:
            pass

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if _is_fg():
            return True

        user32 = ctypes.windll.user32

        # SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001 → 0 disables the lock delay.
        try:
            user32.SystemParametersInfoW(0x2001, 0, None, 0)
        except Exception:
            pass

        # ALT key tap (down+up) — satisfies the foreground-change allowance.
        try:
            VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if _is_fg():
            return True

        # AttachThreadInput bridge: share input state with the target (and the
        # current foreground) thread so SetForegroundWindow is permitted.
        try:
            from ctypes import wintypes
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                        ctypes.POINTER(wintypes.DWORD)]
            user32.AttachThreadInput.restype = wintypes.BOOL
            user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                                 wintypes.BOOL]
            user32.BringWindowToTop.argtypes = [wintypes.HWND]

            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            fg = win32gui.GetForegroundWindow()
            tgt_tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
            fg_tid = (user32.GetWindowThreadProcessId(wintypes.HWND(fg), None)
                      if fg else 0)

            att_tgt = (tgt_tid and tgt_tid != cur_tid
                       and user32.AttachThreadInput(cur_tid, tgt_tid, True))
            att_fg = (fg_tid and fg_tid not in (cur_tid, tgt_tid)
                      and user32.AttachThreadInput(cur_tid, fg_tid, True))
            try:
                user32.BringWindowToTop(wintypes.HWND(hwnd))
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if att_tgt:
                    user32.AttachThreadInput(cur_tid, tgt_tid, False)
                if att_fg:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception:
            pass
        return _is_fg()

    def get_window_rect(self, hwnd):
        import win32gui
        try:
            return win32gui.GetWindowRect(hwnd)
        except Exception:
            return None

    def is_window(self, hwnd) -> bool:
        import win32gui
        try:
            return bool(hwnd) and bool(win32gui.IsWindow(hwnd))
        except Exception:
            return False

    def move_window(self, hwnd, x, y, w, h) -> bool:
        import win32gui, win32con
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)
            return True
        except Exception:
            return False

    # ── Cursor / mouse ────────────────────────────────────────────────────────

    def cursor_pos(self):
        import win32api
        return win32api.GetCursorPos()

    def set_cursor_pos(self, x, y):
        import win32api
        win32api.SetCursorPos((int(x), int(y)))

    def left_button_down(self) -> bool:
        import win32api
        return bool(win32api.GetAsyncKeyState(0x01) & 0x8000)

    # ── Screen ────────────────────────────────────────────────────────────────

    def virtual_screen(self):
        """Bounding box of all monitors: SM_*VIRTUALSCREEN metrics."""
        import ctypes
        _u32 = ctypes.windll.user32
        vx = _u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN — leftmost x
        vy = _u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN — topmost y
        vw = _u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN — total width
        vh = _u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN — total height
        return vx, vy, vw, vh

    # ── Injected-input hook ───────────────────────────────────────────────────

    def install_input_hook(self, mark_operator) -> bool:
        """Low-level mouse+keyboard hooks. Every event carries an injected flag —
        hardware events (flag clear) are the OPERATOR, with zero attribution
        guessing, even mid-SOC-burst. BLOCKING (runs its own message pump);
        returns False if installation fails (caller starts the fallback)."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WH_MOUSE_LL, WH_KEYBOARD_LL = 14, 13
        LLMHF_INJECTED, LLKHF_INJECTED = 0x00000001, 0x00000010
        ULONG_PTR = ctypes.c_size_t
        LRESULT = ctypes.c_ssize_t

        class MSLL(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class KBDLL(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC,
                                             wintypes.HINSTANCE, wintypes.DWORD)
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)

        @HOOKPROC
        def _on_mouse(nCode, wParam, lParam):
            if nCode >= 0:
                try:
                    ms = ctypes.cast(lParam, ctypes.POINTER(MSLL)).contents
                    if not (ms.flags & LLMHF_INJECTED):
                        mark_operator()
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        @HOOKPROC
        def _on_key(nCode, wParam, lParam):
            if nCode >= 0:
                try:
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLL)).contents
                    if not (kb.flags & LLKHF_INJECTED):
                        mark_operator()
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        hm = user32.SetWindowsHookExW(WH_MOUSE_LL, _on_mouse, None, 0)
        hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _on_key, None, 0)
        if not hm and not hk:
            return False
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # message pump
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return True

    # ── Process / app plumbing ────────────────────────────────────────────────

    _instance_mutex = None   # held open for the lifetime of the process

    def acquire_instance_lock(self, name: str) -> bool:
        import ctypes
        # use_last_error=True + ctypes.get_last_error() reads the error captured
        # immediately after the call, before any other ctypes call can clobber
        # it. The bare windll.kernel32.GetLastError() form was unreliable and
        # let extra instances slip past the lock.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype  = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        h   = kernel32.CreateMutexW(None, True, f"Local\\{name}")
        err = ctypes.get_last_error()
        if not h:
            return True          # couldn't create the mutex — don't block launch
        if err == 183:           # ERROR_ALREADY_EXISTS
            return False
        Win32Platform._instance_mutex = h
        return True

    def hide_own_console(self):
        import ctypes
        _con = ctypes.windll.kernel32.GetConsoleWindow()
        if _con:
            ctypes.windll.user32.ShowWindow(_con, 0)

    def set_app_id(self, app_id: str):
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass
