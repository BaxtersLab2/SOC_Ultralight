#!/usr/bin/env python3
"""
test_loop.py — End-to-end routing pipeline test and diagnostic harness

Sends a known test prompt to Agent 1, monitors each stage of the SOC
Ultralight routing pipeline, and produces a diagnostic report showing
exactly where each failure occurs and which compensation to apply.

Commands:
  prompt [test]           Print the test prompt to copy-paste into Copilot
  watch  [test] [agents]  Monitor pipeline after you manually send the prompt
  run    [test]           Auto-inject + monitor (requires button templates)
  report                  Show results from the last saved run

Tests:
  short   ~3 lines — fits in one OCR frame, no scroll needed
  medium  ~12 lines — needs 1-2 scroll clicks
  long    ~35 lines — requires full scroll accumulation

Pipeline stages monitored:
  INJECT -> A1_RESPONDING -> TRIGGER_SEEN -> SCROLL -> SENTINEL_SEEN -> A2_RECEIVED -> MATCH

Usage example:
  py test_loop.py prompt long          # prints the prompt to copy
  py test_loop.py watch long           # monitors while you paste+send it
  py test_loop.py run short            # auto-injects and monitors
"""

import sys, os, time, json, hashlib, difflib, shutil, textwrap
import pyautogui
from PIL import ImageGrab, Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

try:
    import cv2, numpy as np
    _CV2 = True
except ImportError:
    _CV2 = False

pytesseract.pytesseract.tesseract_cmd = (
    shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

_DIR     = os.path.dirname(os.path.abspath(__file__))
_CFG     = os.path.join(_DIR, "config.json")
_BTN_DIR = os.path.join(_DIR, "buttons database")
_LOG_DIR = os.path.join(_DIR, "test_results")
os.makedirs(_LOG_DIR, exist_ok=True)

pyautogui.FAILSAFE = True

# ── Test case definitions ─────────────────────────────────────────────────────
#
# Each prompt instructs Copilot to output a routing message verbatim.
# KEY_PHRASES are substrings we verify appear in the routed text at Agent 2.

_BODY_SHORT = (
    "test_id=SHORT the verification phrase is: alpha bravo charlie delta."
)
_BODY_MEDIUM = (
    "test_id=MEDIUM this is a multi-line routing relay test.\n"
    "Line A: the quick brown fox jumps over the lazy dog.\n"
    "Line B: pack my box with five dozen liquor jugs.\n"
    "Line C: how vexingly quick daft zebras jump.\n"
    "Line D: sphinx of black quartz judge my vow.\n"
    "Line E: the five boxing wizards jump quickly.\n"
    "Line F: jackdaws love my big sphinx of quartz.\n"
    "Line G: the jay pig fox zebra and my wolves quack.\n"
    "End of body."
)
_BODY_LONG = "\n".join(
    [f"Line {i:03d}: the quick brown fox jumps over the lazy dog at sequence position {i}."
     for i in range(1, 36)]
) + "\nEnd of long body."

TESTS = {
    "short": {
        "label":      "SHORT  (1 frame, no scroll)",
        "body":       _BODY_SHORT,
        "key_phrases": ["alpha bravo charlie delta", "test_id=SHORT"],
        "expect_scroll": False,
    },
    "medium": {
        "label":      "MEDIUM  (2-3 frames, light scroll)",
        "body":       _BODY_MEDIUM,
        "key_phrases": ["test_id=MEDIUM", "quick brown fox", "boxing wizards"],
        "expect_scroll": True,
    },
    "long": {
        "label":      "LONG  (scroll accumulation required)",
        "body":       _BODY_LONG,
        "key_phrases": ["Line 001:", "Line 018:", "Line 035:", "End of long body"],
        "expect_scroll": True,
    },
}

def _build_prompt(test_name, dest="agent2"):
    body = TESTS[test_name]["body"]
    routing_text = f"To {dest.capitalize()}, {body}\n\nend message now"
    return (
        f"Please repeat the text between the markers below ONE TIME, "
        f"exactly as written. Do not change any words, add punctuation, "
        f"add a greeting, or include the markers themselves in your reply.\n\n"
        f">>>BEGIN>>>\n"
        f"{routing_text}\n"
        f"<<<END<<<\n"
    )

def _expected_routing_text(test_name, dest="agent2"):
    body = TESTS[test_name]["body"]
    return f"To {dest}, {body}\n\nend message now"

# ── OCR helpers ───────────────────────────────────────────────────────────────

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

def _ocr(bbox):
    return pytesseract.image_to_string(_prepare(_grab(bbox)), config="--psm 6")

def _hash(text):
    return hashlib.md5(text.encode()).hexdigest()[:8]

def _has_trigger(text):
    tl = text.lower()
    return next((ag for ag in ("agent1","agent2","agent3") if f"to {ag}" in tl), None)

def _has_sentinel(text):
    return "end message now" in text.lower()

def _similarity(a, b):
    return difflib.SequenceMatcher(
        None,
        " ".join(a.lower().split()),
        " ".join(b.lower().split())
    ).ratio()

def _merge(base, new):
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

# ── Button injection helpers ──────────────────────────────────────────────────

def _find_button(template_name, threshold=0.75):
    """Locate a button on screen via template matching. Returns (cx,cy) or None."""
    if not _CV2:
        return None
    path = os.path.join(_BTN_DIR, template_name)
    if not os.path.exists(path):
        return None
    screen = np.array(ImageGrab.grab(all_screens=True))
    screen_bgr = cv2.cvtColor(screen, cv2.COLOR_RGB2BGR)
    tmpl = cv2.imread(path)
    if tmpl is None:
        return None
    res = cv2.matchTemplate(screen_bgr, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val < threshold:
        return None
    cx = max_loc[0] + tmpl.shape[1] // 2
    cy = max_loc[1] + tmpl.shape[0] // 2
    return (cx, cy)

def _inject_to_agent1(text):
    """Paste text into Agent 1's input field and click Send. Returns True on success."""
    import pyperclip
    input_pos = _find_button("agent1_input.png")
    send_pos  = _find_button("agent1_send.png")
    if not input_pos or not send_pos:
        return False
    pyperclip.copy(text)
    pyautogui.click(*input_pos)
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.5)
    pyautogui.click(*send_pos)
    return True

# ── Stage monitor ─────────────────────────────────────────────────────────────

class Stage:
    def __init__(self, name):
        self.name     = name
        self.status   = "PENDING"   # PENDING | PASS | FAIL | SKIP
        self.elapsed  = None
        self.note     = ""
        self.data     = {}

    def passed(self, note="", **data):
        self.status  = "PASS"
        self.note    = note
        self.data    = data

    def failed(self, note="", **data):
        self.status = "FAIL"
        self.note   = note
        self.data   = data

    def skipped(self, reason=""):
        self.status = "SKIP"
        self.note   = reason

def _run_pipeline(test_name, cfg, inject_fn=None,
                  src="agent1", dst="agent2",
                  timeout_inject=5,
                  timeout_a1_respond=90,
                  timeout_trigger=60,
                  timeout_sentinel=90,
                  timeout_a2=30):
    """
    Monitor the full routing pipeline.
    inject_fn: callable() → bool, or None (monitoring only).
    Returns list[Stage].
    """
    stages = {name: Stage(name) for name in [
        "INJECT", "A1_RESPONDING", "TRIGGER_SEEN",
        "SENTINEL_SEEN", "A2_RECEIVED", "CONTENT_MATCH"]}

    expected = _expected_routing_text(test_name, dst)
    key_phrases = TESTS[test_name]["key_phrases"]

    src_bbox = tuple(cfg[src]["ocr_region"])
    dst_bbox = tuple(cfg[dst]["ocr_region"]) if dst in cfg else None

    t0 = time.time()
    def elapsed():
        return time.time() - t0

    # ── INJECT ────────────────────────────────────────────────────────────────
    s = stages["INJECT"]
    if inject_fn:
        prompt_text = _build_prompt(test_name, dst)
        ok = inject_fn(prompt_text)
        if ok:
            s.passed(f"Auto-injected {len(prompt_text)} chars → {src}", chars=len(prompt_text))
        else:
            s.failed("Template matching failed — buttons not found")
            for name in list(stages)[1:]:
                stages[name].skipped("INJECT failed")
            return list(stages.values())
    else:
        s.passed("Manual inject (monitoring only)", manual=True)
    s.elapsed = elapsed()

    # ── A1_RESPONDING ─────────────────────────────────────────────────────────
    s = stages["A1_RESPONDING"]
    baseline_hash = _hash(_ocr(src_bbox))
    print(f"  Waiting for {src} to respond ", end="", flush=True)
    deadline = time.time() + timeout_a1_respond
    while time.time() < deadline:
        h = _hash(_ocr(src_bbox))
        print(".", end="", flush=True)
        if h != baseline_hash:
            s.passed(f"Content changed (hash {baseline_hash}→{h})", old=baseline_hash, new=h)
            s.elapsed = elapsed()
            break
        time.sleep(2.0)
    else:
        s.failed(f"No change in {src} after {timeout_a1_respond}s")
        s.elapsed = elapsed()
        for name in ["TRIGGER_SEEN","SENTINEL_SEEN","A2_RECEIVED","CONTENT_MATCH"]:
            stages[name].skipped("A1_RESPONDING failed")
        print()
        return list(stages.values())
    print()

    # ── TRIGGER_SEEN ──────────────────────────────────────────────────────────
    s = stages["TRIGGER_SEEN"]
    accumulated = ""
    trigger_agent = None
    scroll_clicks = 0
    print(f"  Watching for trigger+sentinel ", end="", flush=True)
    deadline = time.time() + timeout_trigger
    while time.time() < deadline:
        text = _ocr(src_bbox)
        accumulated = _merge(accumulated, text)
        trig = _has_trigger(text)
        sent = _has_sentinel(text)
        print("." if trig or sent else "_", end="", flush=True)
        if trig and s.status == "PENDING":
            trigger_agent = trig
            s.passed(f"Trigger found: To {trig}", hash=_hash(text))
            s.elapsed = elapsed()
        if sent:
            stages["SENTINEL_SEEN"].passed("Sentinel in same frame as trigger", scroll_clicks=0)
            stages["SENTINEL_SEEN"].elapsed = elapsed()
            break
        if s.status == "PASS":
            # Trigger seen but no sentinel — need to scroll
            pyautogui.moveTo(
                (src_bbox[0]+src_bbox[2])//2,
                (src_bbox[1]+src_bbox[3])//2, duration=0.0)
            pyautogui.scroll(-3)
            scroll_clicks += 1
        time.sleep(0.5)
    else:
        if s.status == "PENDING":
            s.failed(f"Trigger never found in {timeout_trigger}s", accumulated_lines=len(accumulated.splitlines()))
        if stages["SENTINEL_SEEN"].status == "PENDING":
            stages["SENTINEL_SEEN"].failed(
                f"Sentinel not found after {scroll_clicks} scroll clicks",
                scroll_clicks=scroll_clicks)
    print()

    if stages["SENTINEL_SEEN"].status == "PENDING":
        # Give extra time — keep scrolling up to timeout_sentinel total
        deadline2 = time.time() + timeout_sentinel
        print(f"  Extended sentinel watch ", end="", flush=True)
        while time.time() < deadline2:
            text = _ocr(src_bbox)
            accumulated = _merge(accumulated, text)
            print("." if _has_sentinel(text) else "_", end="", flush=True)
            if _has_sentinel(text):
                stages["SENTINEL_SEEN"].passed(
                    f"Sentinel found (extended, {scroll_clicks} clicks)",
                    scroll_clicks=scroll_clicks)
                stages["SENTINEL_SEEN"].elapsed = elapsed()
                break
            pyautogui.moveTo(
                (src_bbox[0]+src_bbox[2])//2,
                (src_bbox[1]+src_bbox[3])//2, duration=0.0)
            pyautogui.scroll(-3)
            scroll_clicks += 1
            time.sleep(0.5)
        else:
            if stages["SENTINEL_SEEN"].status == "PENDING":
                stages["SENTINEL_SEEN"].failed(
                    f"Sentinel never found ({scroll_clicks} clicks, {timeout_sentinel}s)",
                    scroll_clicks=scroll_clicks)
                stages["SENTINEL_SEEN"].elapsed = elapsed()
        print()

    # ── A2_RECEIVED ───────────────────────────────────────────────────────────
    s = stages["A2_RECEIVED"]
    if stages["SENTINEL_SEEN"].status != "PASS" or dst_bbox is None:
        s.skipped("SENTINEL_SEEN did not pass" if dst_bbox else "dst agent not configured")
        stages["CONTENT_MATCH"].skipped("A2_RECEIVED skipped")
    else:
        baseline_dst = _hash(_ocr(dst_bbox))
        print(f"  Watching {dst} for injected content ", end="", flush=True)
        deadline = time.time() + timeout_a2
        while time.time() < deadline:
            dst_text = _ocr(dst_bbox)
            print(".", end="", flush=True)
            found_phrases = [p for p in key_phrases if p.lower() in dst_text.lower()]
            if found_phrases:
                s.passed(f"Found {len(found_phrases)}/{len(key_phrases)} key phrases",
                         found=found_phrases, missing=[p for p in key_phrases if p not in found_phrases])
                s.elapsed = elapsed()
                break
            time.sleep(1.5)
        else:
            dst_text = _ocr(dst_bbox)
            found_phrases = [p for p in key_phrases if p.lower() in dst_text.lower()]
            s.failed(
                f"Key phrases not found in {dst} after {timeout_a2}s",
                found=found_phrases,
                missing=[p for p in key_phrases if p.lower() not in dst_text.lower()])
            s.elapsed = elapsed()
        print()

        # ── CONTENT_MATCH ─────────────────────────────────────────────────────
        cs = stages["CONTENT_MATCH"]
        score = _similarity(expected, accumulated)
        if score >= 0.85:
            cs.passed(f"Similarity {score*100:.1f}%", score=score)
        else:
            diff = list(difflib.unified_diff(
                expected.splitlines(), accumulated.splitlines(),
                fromfile="expected", tofile="ocr_accumulated", lineterm=""))
            cs.failed(f"Similarity {score*100:.1f}% (threshold 85%)",
                      score=score, diff=diff[:30])
        cs.elapsed = elapsed()

    return list(stages.values())

# ── Compensation table ────────────────────────────────────────────────────────

COMPENSATIONS = {
    "INJECT": [
        "Run Auto-Calibrate in SOC Ultralight to re-train button templates",
        "Check that agent1_input.png and agent1_send.png exist in 'buttons database/'",
        "Use 'watch' mode and inject manually as a workaround",
    ],
    "A1_RESPONDING": [
        "Verify Copilot window is open and the prompt was sent",
        "Check agent1 OCR region in config.json — may be capturing wrong area",
        "Run: py pc.py watch agent1 — confirm OCR region shows live content",
    ],
    "TRIGGER_SEEN": [
        "OCR missed 'To AgentX' — check agent1 OCR region top boundary (y0 in config.json)",
        "Run: py calibrate.py scan agent1 — see if trigger is visible in current frame",
        "Copilot may have reformatted the output (added markdown) — verify prompt wording",
        "Try expanding OCR region upward (decrease y0) to capture more of the window",
    ],
    "SENTINEL_SEEN": [
        "Scroll accumulation timed out — increase SCROLL_ACCUM_TIMEOUT in soc_ultralight.py",
        "Scroll speed too fast — run: py calibrate.py sweep agent1 to find sweet spot",
        "Sentinel text OCR'd incorrectly — run: py calibrate.py scan agent1 after scrolling to bottom",
        "Agent window not tall enough — scroll units may need adjustment (SCROLL_UNITS in calibrate.py)",
        "Copilot added extra text after sentinel — verify exact response format",
    ],
    "A2_RECEIVED": [
        "SOC Ultralight routing did not fire — check that OCR is running (green indicator)",
        "Injection grace period conflict — another inject may have just finished",
        "Check agent2 window is visible and its input field is unobstructed",
        "Run: py pc.py watch agent2 — confirm agent2 OCR region shows live content",
    ],
    "CONTENT_MATCH": [
        "OCR introduced character errors — run: py calibrate.py sweep agent1 for better scroll speed",
        "Scroll accumulation lost lines — check _merge overlap detection (overlap window = 12 lines)",
        "Message was truncated — SCROLL_ACCUM_TIMEOUT may have fired mid-message",
        "Run: py calibrate.py capture agent1 then compare calibration/ground_truth_agent1.txt",
    ],
}

# ── Report printer ────────────────────────────────────────────────────────────

def _print_report(stages, test_name, run_ts):
    label = TESTS[test_name]["label"]
    w = 68
    print()
    print("=" * w)
    print(f" TEST REPORT: {label}")
    print(f" Run: {run_ts}")
    print("=" * w)
    print(f"  {'Stage':<20} {'Status':<8} {'t+':<8} Notes")
    print("  " + "-" * (w - 2))

    first_fail = None
    for s in stages:
        t = f"{s.elapsed:.1f}s" if s.elapsed is not None else "-"
        print(f"  {s.name:<20} {s.status:<8} {t:<8} {s.note}")
        if s.status == "FAIL" and first_fail is None:
            first_fail = s

    all_passed = all(s.status in ("PASS","SKIP") for s in stages)
    print()
    if all_passed:
        total = next((s.elapsed for s in reversed(stages) if s.elapsed), 0)
        print(f"  RESULT: ALL STAGES PASSED  total={total:.1f}s")
    else:
        print(f"  RESULT: FAILED at {first_fail.name}")

    # Failure details
    failed_stages = [s for s in stages if s.status == "FAIL"]
    if failed_stages:
        print()
        print(" DIAGNOSIS")
        print(" " + "-" * (w - 1))
        for s in failed_stages:
            print(f"\n  [{s.name}] {s.note}")
            if "diff" in s.data:
                print("  Diff (expected vs OCR accumulated):")
                for l in s.data["diff"][:20]:
                    print("    ", l)
            if "missing" in s.data and s.data["missing"]:
                print(f"  Missing key phrases: {s.data['missing']}")
            if "scroll_clicks" in s.data:
                print(f"  Scroll clicks used: {s.data['scroll_clicks']}")
            print()
            print(f"  Compensations to try:")
            for i, c in enumerate(COMPENSATIONS.get(s.name, []), 1):
                print(f"    {i}. {c}")

    print()
    print("=" * w)

    # Save report to file
    ts_safe = run_ts.replace(":", "-").replace(" ", "_")
    log_path = os.path.join(_LOG_DIR, f"run_{test_name}_{ts_safe}.json")
    report = {
        "test": test_name, "timestamp": run_ts,
        "stages": [{
            "name": s.name, "status": s.status,
            "elapsed": s.elapsed, "note": s.note,
            "data": {k: v for k, v in s.data.items() if k != "diff"}
        } for s in stages]
    }
    with open(log_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Results saved: {log_path}")
    print()

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_prompt(args):
    """Print the test prompt for manual copy-paste into Copilot."""
    test = args[0] if args else "short"
    if test not in TESTS:
        print(f"Unknown test: {test!r}  choices: {', '.join(TESTS)}")
        return
    dest = args[1] if len(args) > 1 else "agent2"
    prompt = _build_prompt(test, dest)
    label  = TESTS[test]["label"]
    print(f"\n{'='*68}")
    print(f" TEST PROMPT: {label}")
    print(f" Destination: {dest}")
    print(f"{'='*68}")
    print()
    print(prompt)
    print()
    print(f"{'='*68}")
    print(f" Copy the text above and paste it into Copilot (Agent 1).")
    print(f" Then run:  py test_loop.py watch {test}")
    print(f"{'='*68}\n")

def cmd_watch(args):
    """Monitor pipeline — you inject the prompt manually."""
    test = args[0] if args else "short"
    if test not in TESTS:
        print(f"Unknown test: {test!r}  choices: {', '.join(TESTS)}")
        return
    src  = args[1] if len(args) > 1 else "agent1"
    dst  = args[2] if len(args) > 2 else "agent2"
    cfg  = _cfg()

    if src not in cfg:
        print(f"Source agent {src!r} not in config.json")
        return

    label  = TESTS[test]["label"]
    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*68}")
    print(f" WATCH MODE: {label}  src={src}  dst={dst}")
    print(f" Send the prompt to {src} now, then watch here.")
    print(f" (py test_loop.py prompt {test}  to see the prompt)")
    print(f"{'='*68}\n")
    print("  Press Ctrl+C to abort.\n")

    stages = _run_pipeline(test, cfg, inject_fn=None, src=src, dst=dst)
    _print_report(stages, test, run_ts)

def cmd_run(args):
    """Auto-inject test prompt via template matching, then monitor."""
    test = args[0] if args else "short"
    if test not in TESTS:
        print(f"Unknown test: {test!r}  choices: {', '.join(TESTS)}")
        return
    src = args[1] if len(args) > 1 else "agent1"
    dst = args[2] if len(args) > 2 else "agent2"
    cfg = _cfg()

    if not _CV2:
        print("cv2 not installed — cannot auto-inject. Use 'watch' mode instead.")
        return
    if src not in cfg:
        print(f"Source agent {src!r} not in config.json")
        return

    label  = TESTS[test]["label"]
    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*68}")
    print(f" RUN MODE: {label}  src={src}  dst={dst}")
    print(f"{'='*68}\n")

    stages = _run_pipeline(test, cfg,
                           inject_fn=_inject_to_agent1,
                           src=src, dst=dst)
    _print_report(stages, test, run_ts)

def cmd_list(_):
    print("\nAvailable tests:")
    for name, t in TESTS.items():
        phrases = ", ".join(f'"{p}"' for p in t["key_phrases"][:2])
        print(f"  {name:<8}  {t['label']}")
        print(f"           key phrases: {phrases}")
    print()

COMMANDS = {
    "prompt": cmd_prompt,
    "watch":  cmd_watch,
    "run":    cmd_run,
    "list":   cmd_list,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0)
    try:
        COMMANDS[sys.argv[1]](sys.argv[2:])
    except KeyboardInterrupt:
        print("\n\n  Aborted by user (Ctrl+C)")
