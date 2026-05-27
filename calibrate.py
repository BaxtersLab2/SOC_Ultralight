#!/usr/bin/env python3
"""
calibrate.py — SOC Ultralight scroll + OCR calibration

Commands:
  scan    [agent]    Single frame check: is trigger/sentinel visible? Which way to scroll?
  capture [agent]    Scroll through at safe speed, save full accumulated text as ground truth
  sweep   [agent]    Test multiple scroll speeds vs ground truth, print sweet-spot table
  match   [agent]    One-shot similarity: compare current live frame vs saved ground truth

Workflow:
  1. Have the LLM produce a long response in the agent window
  2. py calibrate.py capture agent1   <- scroll through slowly, saves ground truth
  3. py calibrate.py sweep   agent1   <- tests all speeds, prints sweet-spot table
  4. Note the sweet spot speed and set SCROLL_ACCUM_MIN_INTERVAL in soc_ultralight.py

Works with any configured agent (agent1 = Copilot, agent2/3 = VS Code Claude).
"""

import sys, os, time, json, hashlib, difflib, shutil
import pyautogui
from PIL import ImageGrab, Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

pytesseract.pytesseract.tesseract_cmd = (
    shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

_DIR   = os.path.dirname(os.path.abspath(__file__))
_CFG   = os.path.join(_DIR, "config.json")
_GTDIR = os.path.join(_DIR, "calibration")
os.makedirs(_GTDIR, exist_ok=True)

SAFE_SPEED     = 0.7    # seconds between clicks when capturing ground truth
SCROLL_SPEEDS  = [0.6, 0.5, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15]
PASS_THRESHOLD = 0.90   # similarity ratio counted as a clean read
SCROLL_UNITS   = 3      # pyautogui scroll units per click (pos=up, neg=down)
MAX_CLICKS     = 120    # safety cap per pass

pyautogui.FAILSAFE = True

# ── Core helpers ───────────────────────────────────────────────────────────────

def _cfg():
    return json.load(open(_CFG))

def _prepare(img):
    img = img.convert("L")
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    img = ImageOps.autocontrast(img)
    return img

def _grab(bbox):
    return ImageGrab.grab(bbox=bbox, all_screens=True)

def _ocr(img):
    return pytesseract.image_to_string(_prepare(img), config="--psm 6")

def _hash(text):
    return hashlib.md5(text.encode()).hexdigest()[:8]

def _center(bbox):
    x0, y0, x1, y1 = bbox
    return (x0 + x1) // 2, (y0 + y1) // 2

def _has_trigger(text):
    tl = text.lower()
    return next((ag for ag in ("agent1", "agent2", "agent3") if f"to {ag}" in tl), None)

def _has_sentinel(text):
    return "end message now" in text.lower()

def _merge(base, new):
    """Stitch two OCR frames, deduplicating overlapping lines."""
    base_lines = [l for l in base.splitlines() if l.strip()]
    new_lines  = [l for l in new.splitlines()  if l.strip()]
    if not base_lines:
        return new
    overlap = 0
    for n in range(min(len(base_lines), len(new_lines), 12), 0, -1):
        if base_lines[-n:] == new_lines[:n]:
            overlap = n
            break
    return "\n".join(base_lines + new_lines[overlap:])

def _similarity(a, b):
    """Normalized similarity, whitespace-insensitive."""
    return difflib.SequenceMatcher(
        None,
        " ".join(a.lower().split()),
        " ".join(b.lower().split())
    ).ratio()

def _gt_path(agent):
    return os.path.join(_GTDIR, f"ground_truth_{agent}.txt")

def _load_gt(agent):
    p = _gt_path(agent)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else None

def _save_gt(agent, text):
    with open(_gt_path(agent), "w", encoding="utf-8") as f:
        f.write(text)

def _scroll_to_top(bbox, max_clicks=40):
    """Scroll up until text stops changing. Returns number of up-clicks used."""
    cx, cy = _center(bbox)
    pyautogui.moveTo(cx, cy, duration=0.2)
    prev_hash  = None
    same_count = 0
    for i in range(max_clicks):
        h = _hash(_ocr(_grab(bbox)))
        if h == prev_hash:
            same_count += 1
            if same_count >= 3:
                return i
        else:
            same_count = 0
        prev_hash = h
        pyautogui.scroll(SCROLL_UNITS)   # up
        time.sleep(0.35)
    return max_clicks

def _scroll_pass(bbox, interval, verbose=True):
    """
    Scroll from current position to bottom, accumulating OCR at each frame.
    Prints a dot per changed frame, 's' per static frame.
    Returns: (accumulated_text, clicks, elapsed_secs, sentinel_found)
    """
    cx, cy = _center(bbox)
    accumulated    = ""
    clicks         = 0
    sentinel_found = False
    prev_hash      = None
    no_change_run  = 0
    start          = time.time()

    while clicks < MAX_CLICKS:
        img  = _grab(bbox)
        text = _ocr(img)
        h    = _hash(text)

        changed = (h != prev_hash)
        if verbose:
            print("." if changed else "s", end="", flush=True)

        accumulated = _merge(accumulated, text)

        if _has_sentinel(text):
            sentinel_found = True
            break

        if not changed:
            no_change_run += 1
            if no_change_run >= 5:
                break  # stuck at bottom — no sentinel found
        else:
            no_change_run = 0

        prev_hash = h
        pyautogui.moveTo(cx, cy, duration=0.0)
        pyautogui.scroll(-SCROLL_UNITS)  # down
        clicks += 1
        time.sleep(interval)

    return accumulated, clicks, time.time() - start, sentinel_found

# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_scan(args):
    """Single-frame boundary check — no scrolling."""
    cfg   = _cfg()
    agent = args[0] if args else "agent1"
    if agent not in cfg:
        print(f"Unknown agent: {agent!r}  available: {', '.join(cfg)}")
        return

    bbox = tuple(cfg[agent]["ocr_region"])
    text = _ocr(_grab(bbox))

    trigger  = _has_trigger(text)
    sentinel = _has_sentinel(text)
    h        = _hash(text)

    print(f"\n=== SCAN: {agent}  region={bbox}  hash={h} ===")
    print(f"  Trigger  (top)    : {'YES — To ' + trigger if trigger else 'NO'}")
    print(f"  Sentinel (bottom) : {'YES' if sentinel else 'NO'}")
    if trigger and sentinel:
        print("  RESULT: Full message visible — no scrolling needed")
    elif trigger and not sentinel:
        print("  RESULT: Top visible, bottom cut off — scroll DOWN")
    elif not trigger and sentinel:
        print("  RESULT: Bottom visible, top cut off — scroll UP")
    else:
        print("  RESULT: Mid-message — scroll UP first, then DOWN")

    print()
    for line in text.splitlines():
        if line.strip():
            print(" ", line)

    snap = os.path.join(_DIR, f"snap_{agent}_scan.png")
    _grab(bbox).save(snap)
    print(f"\n  Snapshot saved: {snap}")


def cmd_capture(args):
    """Scroll through full message at safe speed and save as ground truth."""
    cfg   = _cfg()
    agent = args[0] if args else "agent1"
    if agent not in cfg:
        print(f"Unknown agent: {agent!r}  available: {', '.join(cfg)}")
        return

    bbox = tuple(cfg[agent]["ocr_region"])

    print(f"\nCapturing ground truth for {agent}  (safe speed = {SAFE_SPEED}s/click)")
    print("Scrolling to top ", end="", flush=True)
    n = _scroll_to_top(bbox)
    print(f"({n} up-clicks)")

    print("Scrolling down   ", end="", flush=True)
    acc, clicks, elapsed, sentinel = _scroll_pass(bbox, SAFE_SPEED)
    print(f"\n  {clicks} clicks  {elapsed:.1f}s  sentinel={'YES' if sentinel else 'NO'}")

    if not sentinel:
        print("  WARNING: sentinel (end message now) not found — ground truth may be incomplete")

    _save_gt(agent, acc)
    lines = [l for l in acc.splitlines() if l.strip()]
    print(f"\n  Saved: {_gt_path(agent)}")
    print(f"  Size : {len(acc)} chars  {len(lines)} lines")

    print("\n  --- first 5 lines ---")
    for l in lines[:5]:
        print("   ", l)
    print("  --- last 5 lines ---")
    for l in lines[-5:]:
        print("   ", l)


def cmd_match(args):
    """Compare current live OCR frame against saved ground truth."""
    cfg   = _cfg()
    agent = args[0] if args else "agent1"
    if agent not in cfg:
        print(f"Unknown agent: {agent!r}  available: {', '.join(cfg)}")
        return

    gt = _load_gt(agent)
    if gt is None:
        print(f"No ground truth — run: py calibrate.py capture {agent}")
        return

    bbox  = tuple(cfg[agent]["ocr_region"])
    text  = _ocr(_grab(bbox))
    score = _similarity(gt, text)
    label = "PASS" if score >= PASS_THRESHOLD else "FAIL"
    print(f"\n  Similarity (single frame): {score*100:.1f}%  {label}")

    diff = list(difflib.unified_diff(
        gt.splitlines(), text.splitlines(),
        fromfile="ground_truth", tofile="ocr_live", lineterm=""))
    if diff:
        print("\n  Diff:")
        for l in diff[:60]:
            print("   ", l)
    else:
        print("  Perfect match!")


def cmd_sweep(args):
    """Test multiple scroll speeds against ground truth and print sweet-spot table."""
    cfg   = _cfg()
    agent = args[0] if args else "agent1"
    if agent not in cfg:
        print(f"Unknown agent: {agent!r}  available: {', '.join(cfg)}")
        return

    gt = _load_gt(agent)
    if gt is None:
        print(f"No ground truth for {agent}")
        print(f"  Run first: py calibrate.py capture {agent}")
        return

    bbox    = tuple(cfg[agent]["ocr_region"])
    results = []

    for speed in SCROLL_SPEEDS:
        print(f"\n--- {speed}s/click ---  ", end="", flush=True)
        print("top ", end="", flush=True)
        _scroll_to_top(bbox)
        print("| down ", end="", flush=True)

        acc, clicks, elapsed, sentinel = _scroll_pass(bbox, speed)
        score  = _similarity(gt, acc)
        status = "PASS" if score >= PASS_THRESHOLD else "FAIL"
        print(f"\n     {clicks} clicks  {elapsed:.1f}s  sentinel={'Y' if sentinel else 'N'}  "
              f"{score*100:.1f}%  {status}")

        out = os.path.join(_GTDIR, f"sweep_{agent}_{int(speed * 1000)}ms.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(acc)

        results.append((speed, clicks, elapsed, score, sentinel))

    # Summary table
    print("\n" + "=" * 65)
    print(f" SWEEP RESULTS  agent={agent}  pass={PASS_THRESHOLD*100:.0f}%  "
          f"scroll_units={SCROLL_UNITS}")
    print("=" * 65)
    print(f"  {'Speed':>8}  {'Clicks':>6}  {'Time':>6}  {'Score':>7}  Result")
    print("  " + "-" * 60)
    sweet_spot = None
    for speed, clicks, elapsed, score, sentinel in results:
        status = "PASS" if score >= PASS_THRESHOLD else "FAIL"
        mark   = "  <- SWEET SPOT" if (score >= PASS_THRESHOLD and sweet_spot is None) else ""
        if score >= PASS_THRESHOLD and sweet_spot is None:
            sweet_spot = speed
        print(f"   {speed:.2f}s  {clicks:>6}  {elapsed:>5.1f}s  "
              f"{score*100:>6.1f}%  {status}{mark}")

    print()
    if sweet_spot:
        clicks_for_sweet = next(c for s, c, *_ in results if s == sweet_spot)
        total_t = next(e for s, c, e, *_ in results if s == sweet_spot)
        print(f"  Sweet spot : {sweet_spot}s per click  "
              f"({1/sweet_spot:.1f} clicks/sec,  {clicks_for_sweet} clicks,  {total_t:.1f}s total)")
        print(f"  Action     : set SCROLL_ACCUM_MIN_INTERVAL = {sweet_spot} in soc_ultralight.py")
    else:
        print("  No speed passed threshold.")
        print("  Try: increase PASS_THRESHOLD tolerance,  reduce SCROLL_UNITS,  "
              "or widen the OCR region in config.json")
    print()


# ── Dispatch ───────────────────────────────────────────────────────────────────

COMMANDS = {
    "scan":    cmd_scan,
    "capture": cmd_capture,
    "match":   cmd_match,
    "sweep":   cmd_sweep,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0)
    COMMANDS[sys.argv[1]](sys.argv[2:])
