"""
install.py — SOC Ultralight installer

Walks a Y/N flow:
  1. Verify Python dependencies (from requirements.txt)
  2. Offer optional V plugin install (vision agent)
  3. If V plugin selected, verify/help install GGUF Chatbox
  4. Save user choices to config.json so SOCU's runtime can verify them

Re-runnable. If the V plugin or GGUF Chatbox are already installed and valid,
skips silently and tells the user. Pass --reconfigure to force re-prompting.
"""

from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
PLUGINS_DIR     = BASE_DIR / "plugins"
V_PLUGIN_DIR    = PLUGINS_DIR / "v_plugin"
V_PLUGIN_REPO   = "https://github.com/BaxtersLab/V_plugin.git"
V_PLUGIN_TAG    = "main"     # change to a tag like "v0.1.0" to pin
GGUF_CHATBOX_REPO_URL = "https://github.com/BaxtersLab/GGUF-Chatbox"
CONFIG_FILE     = BASE_DIR / "config.json"
REQUIREMENTS    = BASE_DIR / "requirements.txt"

# Known GGUF Chatbox install/source locations to probe (Windows-only product).
GGUF_CHATBOX_PROBE_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GGUFChatbox",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GGUF Chatbox",
    Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "GGUF Chatbox",
    Path.home() / "Desktop" / "Baxters Apps" / "gguf chatbox",   # author's dev path
    Path.home() / "Desktop" / "GGUF-Chatbox",
]

VLM_PORT = 8082

MIN_VRAM_MB = 6_000   # ~6 GB floor — smallest useful VLM
REC_VRAM_MB = 12_000  # ~12 GB for comfortable operation


# ── GPU probe ─────────────────────────────────────────────────────────────────
def _query_vram_mb() -> list[tuple[str, int]]:
    """Return [(gpu_name, vram_mb), ...] via nvidia-smi. Empty list if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        results = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2 and parts[1].isdigit():
                results.append((parts[0], int(parts[1])))
        return results
    except Exception:
        return []

def step_gpu_check() -> bool:
    """Probe GPU VRAM. Returns True if hardware meets minimum, False otherwise."""
    hdr("Step 2 / 5   GPU hardware check")
    gpus = _query_vram_mb()
    if not gpus:
        warn("nvidia-smi not found or no NVIDIA GPU detected.")
        info("AMD / Intel GPU or CPU-only mode: V plugin will run but may be slow.")
        info("Minimum for acceptable performance: ~6 GB VRAM (or 16+ GB system RAM for CPU).")
        return ask_yn("Continue with V plugin install anyway?", default=False)

    capable = False
    for name, mb in gpus:
        gb = mb / 1024
        if mb >= REC_VRAM_MB:
            ok(f"{name}  —  {gb:.1f} GB VRAM  ✓ recommended")
            capable = True
        elif mb >= MIN_VRAM_MB:
            warn(f"{name}  —  {gb:.1f} GB VRAM  (meets minimum; smaller models only)")
            capable = True
        else:
            fail(f"{name}  —  {gb:.1f} GB VRAM  (below {MIN_VRAM_MB // 1024} GB minimum)")

    if not capable:
        print()
        info("Your GPU does not meet the minimum requirements for V plugin.")
        info("SOC Ultralight will remain lightweight (no Agent 4).")
        info("Re-run install.py if you upgrade your hardware.")
        return False

    return True


# ── Tiny terminal helpers (no third-party deps) ───────────────────────────────
def hdr(title: str) -> None:
    bar = "─" * (len(title) + 4)
    print(f"\n┌{bar}┐\n│  {title}  │\n└{bar}┘")

def ok(msg: str)   -> None: print(f"  \u2713 {msg}")
def warn(msg: str) -> None: print(f"  ! {msg}")
def fail(msg: str) -> None: print(f"  \u2717 {msg}")
def info(msg: str) -> None: print(f"    {msg}")

def ask_yn(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        raw = input(question + suffix).strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"): return True
        if raw in ("n", "no"):  return False
        print("    Please answer y or n.")

def ask_choice(question: str, choices: list[str]) -> int:
    while True:
        for i, c in enumerate(choices, 1):
            print(f"    [{i}] {c}")
        raw = input(question + ": ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return int(raw) - 1
        print("    Invalid choice.")


# ── Step 1: Python deps ───────────────────────────────────────────────────────
def step_python_deps() -> bool:
    hdr("Step 1 / 4   Python dependencies")
    if not REQUIREMENTS.exists():
        warn(f"requirements.txt not found at {REQUIREMENTS} — skipping")
        return True
    if not ask_yn("Run `pip install -r requirements.txt` now?", default=True):
        info("Skipped — you can run it later with: pip install -r requirements.txt")
        return True
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
        ok("Python dependencies installed")
        return True
    except subprocess.CalledProcessError as e:
        fail(f"pip install failed: {e}")
        return False


# ── Step 2: V plugin ──────────────────────────────────────────────────────────
def v_plugin_installed() -> bool:
    return (V_PLUGIN_DIR / "v_plugin.py").exists() \
        or (PLUGINS_DIR / "v_plugin.py").exists()

def step_v_plugin(force_reconfigure: bool) -> bool:
    hdr("Step 3 / 5   Optional: V plugin (vision agent)")
    print("    Adds a 4th agent powered by a local vision GGUF model.")
    print("    Requires GGUF Chatbox running on localhost:8082.")
    print()

    if v_plugin_installed() and not force_reconfigure:
        ok("V plugin already installed.")
        if ask_yn("Update it (git pull)?", default=False):
            return _git_pull_v_plugin()
        return True

    if not ask_yn("Install V plugin now?", default=False):
        info("Skipped. SOCU will run lightweight (no Agent 4 button).")
        info("Re-run install.py later to add it.")
        _save_config_key("v_plugin_installed", False)
        return True

    if not _git_available():
        fail("git not found in PATH — cannot clone V plugin.")
        info(f"Install git first, then re-run, or clone manually:")
        info(f"  git clone {V_PLUGIN_REPO} {V_PLUGIN_DIR}")
        return False

    PLUGINS_DIR.mkdir(exist_ok=True)
    print(f"    Cloning {V_PLUGIN_REPO} -> {V_PLUGIN_DIR} ...")
    try:
        subprocess.check_call([
            "git", "clone", "--branch", V_PLUGIN_TAG, "--depth", "1",
            V_PLUGIN_REPO, str(V_PLUGIN_DIR),
        ])
        ok("V plugin cloned")
        _save_config_key("v_plugin_installed", True)
        return True
    except subprocess.CalledProcessError as e:
        fail(f"git clone failed: {e}")
        return False

def _git_pull_v_plugin() -> bool:
    try:
        subprocess.check_call(["git", "-C", str(V_PLUGIN_DIR), "pull"])
        ok("V plugin updated")
        return True
    except subprocess.CalledProcessError as e:
        fail(f"git pull failed: {e}")
        return False

def _git_available() -> bool:
    try:
        subprocess.check_output(["git", "--version"])
        return True
    except Exception:
        return False


# ── Step 3: GGUF Chatbox ──────────────────────────────────────────────────────
def step_gguf_chatbox(force_reconfigure: bool) -> bool:
    """Only fires if V plugin will be installed (it's the dependency consumer)."""
    if not v_plugin_installed():
        hdr("Step 3 / 5   GGUF Chatbox check  (skipped — V plugin not installed)")
        return True

    hdr("Step 4 / 5   GGUF Chatbox (vision server backend)")
    saved = _load_config_key("gguf_chatbox_path")
    if saved and Path(saved).exists() and not force_reconfigure:
        ok(f"GGUF Chatbox path already saved: {saved}")
        if _probe_port_8082_listening():
            ok(f"Port {VLM_PORT} is currently listening — vision server appears to be running.")
        else:
            info(f"Port {VLM_PORT} not currently listening. Start the Vision Server in")
            info("GGUF Chatbox before using Agent 4.")
        return True

    print("    Checking known install/source locations...")
    found = None
    for p in GGUF_CHATBOX_PROBE_PATHS:
        if p.exists():
            ok(f"found {p}")
            found = p
            break
        else:
            info(f"  not present: {p}")

    if not found:
        print()
        print("    GGUF Chatbox not detected. Options:")
        choice = ask_choice("    Choice", [
            "I already have it — let me enter the path",
            f"Open download page ({GGUF_CHATBOX_REPO_URL})",
            "Skip for now (V plugin installed but won't work until Chatbox is set up)",
        ])
        if choice == 0:
            raw = input("    Enter full path to GGUF Chatbox folder or executable: ").strip().strip('"')
            p = Path(raw)
            if p.exists():
                found = p
                ok("path accepted")
            else:
                fail("path does not exist — skipping")
        elif choice == 1:
            _open_url(GGUF_CHATBOX_REPO_URL)
            info("Opened in your browser. After installing, re-run install.py.")
            return True
        else:
            warn("Skipped. Run install.py --reconfigure later to set the path.")
            return True

    if found:
        _save_config_key("gguf_chatbox_path", str(found))
        ok(f"saved gguf_chatbox_path = {found}")
        # Port availability check (informational only)
        if _probe_port_8082_listening():
            ok(f"Port {VLM_PORT} is currently in use — vision server may already be running.")
        elif _probe_port_8082_free():
            ok(f"Port {VLM_PORT} is free — ready for vision server start.")
        else:
            warn(f"Port {VLM_PORT} state could not be determined.")

    print()
    print("    Vision setup checklist inside GGUF Chatbox:")
    info("• Open Server tray → Vision Server section")
    info("• Set paths: vision model .gguf  +  mmproj .gguf")
    info("• Click Start Vision Server")
    info("• Confirm endpoint: http://127.0.0.1:8082/v1/chat/completions")
    return True


# ── Step 4: Done ──────────────────────────────────────────────────────────────
def step_done() -> None:
    hdr("Step 5 / 5   Done")
    info("Launch SOCU:           python soc_ultralight.py")
    if v_plugin_installed():
        info("Start vision server:   open GGUF Chatbox -> Start Vision Server")
        info("Agent 4 button (👁 A4) appears in the control row once the plugin loads.")
    print()


# ── Config helpers ────────────────────────────────────────────────────────────
def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_config_key(key: str, value) -> None:
    data = _load_config()
    data[key] = value
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def _load_config_key(key: str):
    return _load_config().get(key)


# ── Network probes ────────────────────────────────────────────────────────────
def _probe_port_8082_listening() -> bool:
    """True if something is currently bound to 8082 on localhost."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", VLM_PORT)) == 0
    finally:
        s.close()

def _probe_port_8082_free() -> bool:
    """True if port 8082 can be bound right now (nothing else listening)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", VLM_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()

def _open_url(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        info(f"Open this URL manually: {url}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="SOC Ultralight installer")
    ap.add_argument("--reconfigure", action="store_true",
                    help="Re-prompt even for already-saved settings.")
    args = ap.parse_args()

    print()
    print("==============================================")
    print("  SOC Ultralight  ·  Installer")
    print("==============================================")

    if not step_python_deps():
        return 1
    if step_gpu_check():
        if not step_v_plugin(args.reconfigure):
            return 1
        if not step_gguf_chatbox(args.reconfigure):
            return 1
    step_done()
    return 0


if __name__ == "__main__":
    sys.exit(main())
