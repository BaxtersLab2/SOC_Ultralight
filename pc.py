#!/usr/bin/env python3
"""
pc.py — Claude Code computer-use helper
========================================
Run from Bash to let Claude control the screen during debugging sessions.

Commands:
  screenshot [x0 y0 x1 y1]          grab region (or full primary), save to snap_screen.png
  ocr [agent1|agent2|x0 y0 x1 y1]   OCR a region, print text + dedup hash
  click x y                          left-click at screen coords
  rclick x y                         right-click
  move x y                           move mouse (no click)
  paste "text" x y                   click xy, paste text to clipboard, Ctrl+V
  type "text"                        type text at current focus
  keypress key [key ...]             pyautogui hotkey  e.g.  keypress ctrl v
  find template.png [threshold]      template-match, print center x y
  minimize [title]                   minimize window containing title (default: Visual Studio Code)
  restore  [title]                   restore window containing title
  pos                                print current mouse position
"""

import sys, os, time, json, hashlib, shutil, ctypes
import pyperclip, pyautogui
from PIL import ImageGrab, Image
import pytesseract

# ── Tesseract ─────────────────────────────────────────────────────────────────
pytesseract.pytesseract.tesseract_cmd = (
    shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

# ── cv2 optional ──────────────────────────────────────────────────────────────
try:
    import cv2, numpy as np
    _CV2 = True
except ImportError:
    _CV2 = False

# ── Config ────────────────────────────────────────────────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_SNAP_DIR  = os.path.dirname(__file__)

pyautogui.FAILSAFE = True  # move mouse to top-left corner to abort

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_cfg():
    try:
        return json.load(open(_CFG_PATH))
    except Exception:
        return {}

def _win32_find(title_fragment: str) -> int | None:
    user32 = ctypes.windll.user32
    found = []
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if title_fragment.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None

def _prepare_img(img: Image.Image) -> Image.Image:
    from PIL import ImageEnhance, ImageFilter, ImageOps
    img = img.convert("L")
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    img = ImageOps.autocontrast(img)
    return img

def _grab(bbox=None) -> Image.Image:
    return ImageGrab.grab(bbox=bbox, all_screens=True)

def _ocr_region(bbox, label="region") -> str:
    img  = _grab(bbox)
    path = os.path.join(_SNAP_DIR, f"snap_{label}.png")
    img.save(path)
    text = pytesseract.image_to_string(_prepare_img(img), config="--psm 6")
    h    = hashlib.md5(text.encode()).hexdigest()[:8]
    print(f"[ocr:{label}] hash={h}  size={img.width}x{img.height}  saved={path}")
    print("-" * 60)
    for line in text.splitlines():
        if line.strip():
            print(" ", line)
    print("-" * 60)
    return text

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_screenshot(args):
    if len(args) == 4:
        bbox = tuple(int(a) for a in args)
        label = "region"
    else:
        bbox  = None
        label = "screen"
    img  = _grab(bbox)
    path = os.path.join(_SNAP_DIR, f"snap_{label}.png")
    img.save(path)
    print(f"Saved {path}  ({img.width}x{img.height})")

def cmd_ocr(args):
    cfg = _load_cfg()
    if args and args[0] in ("agent1", "agent2", "agent3"):
        aid  = args[0]
        bbox = tuple(cfg[aid]["ocr_region"])
        _ocr_region(bbox, label=aid)
    elif len(args) == 4:
        bbox = tuple(int(a) for a in args)
        _ocr_region(bbox, label="custom")
    else:
        print("Usage: ocr agent1|agent2  OR  ocr x0 y0 x1 y1")

def cmd_click(args):
    x, y = int(args[0]), int(args[1])
    pyautogui.click(x, y)
    print(f"Clicked ({x}, {y})")

def cmd_rclick(args):
    x, y = int(args[0]), int(args[1])
    pyautogui.rightClick(x, y)
    print(f"Right-clicked ({x}, {y})")

def cmd_move(args):
    x, y = int(args[0]), int(args[1])
    pyautogui.moveTo(x, y, duration=0.2)
    print(f"Moved to ({x}, {y})")

def cmd_paste(args):
    # paste "text" x y
    text = args[0]
    x, y = int(args[1]), int(args[2])
    pyperclip.copy(text)
    pyautogui.click(x, y)
    time.sleep(0.25)
    pyautogui.hotkey("ctrl", "v")
    print(f"Pasted {len(text)} chars at ({x}, {y})")

def cmd_type(args):
    text = args[0]
    pyautogui.typewrite(text, interval=0.03)
    print(f"Typed {len(text)} chars")

def cmd_keypress(args):
    pyautogui.hotkey(*args)
    print(f"Hotkey: {'+'.join(args)}")

def cmd_find(args):
    if not _CV2:
        print("cv2 not installed — cannot do template matching")
        return
    template_path = args[0]
    threshold = float(args[1]) if len(args) > 1 else 0.75
    screen = np.array(_grab())
    screen_bgr = cv2.cvtColor(screen, cv2.COLOR_RGB2BGR)
    tmpl  = cv2.imread(template_path)
    if tmpl is None:
        print(f"Template not found: {template_path}")
        return
    result = cv2.matchTemplate(screen_bgr, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val >= threshold:
        cx = max_loc[0] + tmpl.shape[1] // 2
        cy = max_loc[1] + tmpl.shape[0] // 2
        print(f"Found  conf={max_val:.3f}  center=({cx}, {cy})")
    else:
        print(f"Not found  best_conf={max_val:.3f}  threshold={threshold}")

def cmd_minimize(args):
    title = " ".join(args) if args else "Visual Studio Code"
    hwnd = _win32_find(title)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        time.sleep(0.8)
        print(f"Minimized: hwnd={hwnd}")
    else:
        print(f"Window not found: {title!r}")

def cmd_restore(args):
    title = " ".join(args) if args else "Visual Studio Code"
    hwnd = _win32_find(title)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        print(f"Restored: hwnd={hwnd}")
    else:
        print(f"Window not found: {title!r}")

def cmd_pos(_):
    x, y = pyautogui.position()
    print(f"Mouse position: ({x}, {y})")

# ── Dispatch ──────────────────────────────────────────────────────────────────

COMMANDS = {
    "screenshot": cmd_screenshot,
    "ocr":        cmd_ocr,
    "click":      cmd_click,
    "rclick":     cmd_rclick,
    "move":       cmd_move,
    "paste":      cmd_paste,
    "type":       cmd_type,
    "keypress":   cmd_keypress,
    "find":       cmd_find,
    "minimize":   cmd_minimize,
    "restore":    cmd_restore,
    "pos":        cmd_pos,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0)
    COMMANDS[sys.argv[1]](sys.argv[2:])
