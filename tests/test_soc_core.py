#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the agent1 overflow watchdog, the session manager,
and the CD-changer model-id parser.

Stdlib only — run from the repo root with:

    py -3 -m unittest discover tests -v

Tests build bare SOCUltralight instances (no Tk, no OCR loop) and drive the
extracted decision methods directly.
"""
import datetime
import sys
import tempfile
import threading
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import soc_ultralight as soc


def bare_app(**attrs):
    """A SOCUltralight instance without __init__ — only the attributes the
    method under test actually reads, so tests document real dependencies."""
    app = object.__new__(soc.SOCUltralight)
    for k, v in attrs.items():
        setattr(app, k, v)
    return app


class OverflowWatchdogTests(unittest.TestCase):
    """_agent1_overflow_check — the S23 overflow recovery decision."""

    def armed_app(self, now, **overrides):
        attrs = dict(
            _agent1_expect_since=now - soc.AGENT1_OVERFLOW_TIMEOUT,
            _agent1_copy_fail_at=0.0,
            _agent1_overflow_tries=0,
            _manual_hold={},
        )
        attrs.update(overrides)
        return bare_app(**attrs)

    def test_disarmed_is_none(self):
        now = time.time()
        app = self.armed_app(now, _agent1_expect_since=0.0)
        self.assertEqual(app._agent1_overflow_check(now), "none")

    def test_armed_but_not_timed_out_is_none(self):
        now = time.time()
        app = self.armed_app(now, _agent1_expect_since=now - 1.0)
        self.assertEqual(app._agent1_overflow_check(now), "none")
        self.assertEqual(app._agent1_overflow_tries, 0)

    def test_timeout_fires_retry_and_rearms_timer(self):
        now = time.time()
        app = self.armed_app(now)
        self.assertEqual(app._agent1_overflow_check(now), "retry")
        self.assertEqual(app._agent1_overflow_tries, 1)
        # Timer re-armed at 'now' so the next retry waits a full timeout again.
        self.assertEqual(app._agent1_expect_since, now)

    def test_retry_bound_then_exhausted_disarms(self):
        now = time.time()
        app = self.armed_app(now)
        for i in range(soc.AGENT1_OVERFLOW_MAX_TRIES):
            self.assertEqual(app._agent1_overflow_check(now), "retry", f"try {i + 1}")
            # Re-expire the re-armed timer for the next round.
            app._agent1_expect_since = now - soc.AGENT1_OVERFLOW_TIMEOUT
        self.assertEqual(app._agent1_overflow_check(now), "exhausted")
        # Regression: the give-up path MUST disarm, or the watchdog spins
        # emitting 'exhausted' every OCR tick forever.
        self.assertEqual(app._agent1_expect_since, 0.0)
        self.assertEqual(app._agent1_overflow_check(now), "none")

    def test_copy_cooldown_blocks_retry(self):
        now = time.time()
        app = self.armed_app(now, _agent1_copy_fail_at=now - 0.5)
        self.assertEqual(app._agent1_overflow_check(now), "none")

    def test_manual_hold_blocks_retry(self):
        now = time.time()
        app = self.armed_app(now, _manual_hold={"agent1": True})
        self.assertEqual(app._agent1_overflow_check(now), "none")


class SessionManagerTests(unittest.TestCase):
    """_archive_transcript / _session_reestablish — the S24 session refresh."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = soc.TRANSCRIPT_DIR
        soc.TRANSCRIPT_DIR = Path(self._tmp.name)
        self.logs = []

    def tearDown(self):
        soc.TRANSCRIPT_DIR = self._orig_dir
        self._tmp.cleanup()

    def log_app(self, **attrs):
        return bare_app(_log=self.logs.append, **attrs)

    def test_archive_with_no_transcript_is_noop(self):
        app = self.log_app()
        self.assertEqual(app._archive_transcript(), "nothing to archive")

    def test_archive_moves_transcript_recoverably(self):
        day = datetime.datetime.now().strftime("%Y-%m-%d")
        src = soc.TRANSCRIPT_DIR / f"conversation_{day}.md"
        src.write_text("agent1: hello\n", encoding="utf-8")

        app = self.log_app()
        archived = app._archive_transcript()

        # Recoverable move, never a delete: source gone, archive copy intact.
        self.assertFalse(src.exists())
        dst = soc.TRANSCRIPT_DIR / "archive" / archived
        self.assertTrue(dst.exists(), f"archive file missing: {archived}")
        self.assertEqual(dst.read_text(encoding="utf-8"), "agent1: hello\n")
        self.assertTrue(archived.startswith(f"conversation_{day}__session_"))

    class _Var:
        def __init__(self, value):
            self._value = value

        def get(self):
            return self._value

    def reestablish_app(self, project, summary_file):
        injected = []
        app = self.log_app(
            _inject_grace={},
            _project_name_var=self._Var(project),
            _p1a_summary_file=summary_file,
        )
        app._inject_to_agent = lambda aid, msg, bypass_mode_check=False: injected.append(
            (aid, msg, bypass_mode_check))
        return app, injected

    def test_reestablish_injects_sop_continuation_and_summary(self):
        summary_path = Path(self._tmp.name) / "summary.md"
        summary_path.write_text("The project state.", encoding="utf-8")
        app, injected = self.reestablish_app("kits", str(summary_path))

        before = time.time()
        app._session_reestablish()

        self.assertEqual(len(injected), 1)
        aid, msg, bypass = injected[0]
        self.assertEqual(aid, "agent1")
        self.assertTrue(bypass, "session refresh must bypass the mode check")
        self.assertTrue(msg.startswith(soc.AGENT1_SOP))
        self.assertIn("SESSION REFRESH", msg)
        self.assertIn("project 'kits'", msg)
        self.assertIn("PROJECT SUMMARY", msg)
        self.assertIn("The project state.", msg)
        # Inject grace shields the fresh window from the OCR loop for ~30 s.
        self.assertGreaterEqual(app._inject_grace["agent1"], before + 29)

    def test_reestablish_without_summary_omits_summary_block(self):
        app, injected = self.reestablish_app("", None)
        app._session_reestablish()
        _, msg, _ = injected[0]
        self.assertNotIn("PROJECT SUMMARY", msg)
        self.assertIn("SESSION REFRESH", msg)


class CdParseModelsTests(unittest.TestCase):
    """_cd_parse_models — both /v1/models shapes the proxy can return."""

    def test_openai_shape(self):
        payload = {"data": [{"id": "gemma-3-4b-it.Q4_K_M.gguf"}]}
        self.assertEqual(
            soc.SOCUltralight._cd_parse_models(payload), "gemma-3-4b-it.Q4_K_M.gguf")

    def test_llamacpp_shape_prefers_model_then_name(self):
        payload = {"models": [{"name": "gemma", "model": "D:/gguf/gemma.gguf"}]}
        self.assertEqual(
            soc.SOCUltralight._cd_parse_models(payload), "D:/gguf/gemma.gguf")
        self.assertEqual(
            soc.SOCUltralight._cd_parse_models({"models": [{"name": "gemma"}]}), "gemma")

    def test_empty_and_junk_payloads_are_none(self):
        self.assertIsNone(soc.SOCUltralight._cd_parse_models({}))
        self.assertIsNone(soc.SOCUltralight._cd_parse_models({"data": []}))
        self.assertIsNone(soc.SOCUltralight._cd_parse_models(None))
        self.assertIsNone(soc.SOCUltralight._cd_parse_models("nonsense"))
        self.assertIsNone(soc.SOCUltralight._cd_parse_models({"data": [{"id": "  "}]}))


class RollCallTurnTakingTests(unittest.TestCase):
    """_await_ack — the turn-taking primitive. Each agent must wait for ITS OWN
    SOC-ACK, so two agents sharing one window (agent2 + agent3 in one VS Code)
    can't cross-satisfy each other's attendance."""

    class _Cfg:
        def __init__(self, region):
            self.ocr_region = region

    def _app(self, region):
        return bare_app(agents={"agent2": self._Cfg(region)})

    def test_detects_own_ack_and_marks_present(self):
        app = self._app((0, 0, 10, 10))
        marked = []
        app._mark_attendance = marked.append
        with patch.object(soc, "ImageGrab") as ig, \
                patch.object(soc, "pytesseract") as pt, \
                patch.object(soc, "_prepare_img_for_ocr", lambda x: x), \
                patch.object(soc, "_preprocess_ocr", lambda x: x):
            ig.grab.return_value = "IMG"
            pt.image_to_string.return_value = "SOC-ACK-2"
            self.assertTrue(app._await_ack("agent2", timeout=2))
        self.assertEqual(marked, ["agent2"])

    def test_ignores_another_agents_ack(self):
        # Awaiting agent2 while only agent3's ack is on screen → must NOT mark,
        # must time out. This is the turn-taking guarantee that the pre-change
        # broadcast (all pinged at once) could not make.
        app = self._app((0, 0, 10, 10))
        app._mark_attendance = lambda a: self.fail(f"wrongly marked {a}")
        with patch.object(soc, "ImageGrab") as ig, \
                patch.object(soc, "pytesseract") as pt, \
                patch.object(soc, "_prepare_img_for_ocr", lambda x: x), \
                patch.object(soc, "_preprocess_ocr", lambda x: x):
            ig.grab.return_value = "IMG"
            pt.image_to_string.return_value = "SOC-ACK-3"
            self.assertFalse(app._await_ack("agent2", timeout=0.2))

    def test_no_region_returns_false_immediately(self):
        app = self._app(None)
        self.assertFalse(app._await_ack("agent2", timeout=0.1))


class CdAutoSwapTests(unittest.TestCase):
    """Automatic CD change: magazine resolution, swap trigger, park + watcher."""

    QWY = r"C:\m\Qwythos-9B-BF16.gguf"
    QWY_MM = r"C:\m\mmproj-qwythos.gguf"
    GEMMA = r"C:\m\gemma4-v2-Q8_0.gguf"

    def _magazine(self):
        return [
            {"model_path": self.QWY, "mmproj_path": self.QWY_MM, "label": ""},
            {"model_path": self.GEMMA, "mmproj_path": "", "label": ""},
        ]

    def _app(self, **attrs):
        app = bare_app(_cd_disk={"agent4": "qwythos", "agent5": "gemma4"}, **attrs)
        app._cd_magazine = self._magazine
        return app

    # ── _cd_disk_paths: token → magazine entry ────────────────────────────────
    def test_disk_paths_vision_disk_with_mmproj(self):
        self.assertEqual(self._app()._cd_disk_paths("agent4"),
                         (self.QWY, self.QWY_MM))

    def test_disk_paths_text_disk_mmproj_none(self):
        self.assertEqual(self._app()._cd_disk_paths("agent5"),
                         (self.GEMMA, None))

    def test_disk_paths_unconfigured_agent_is_none(self):
        app = self._app()
        app._cd_disk = {"agent4": "", "agent5": ""}
        self.assertIsNone(app._cd_disk_paths("agent4"))

    def test_disk_paths_no_magazine_match_is_none(self):
        app = self._app()
        app._cd_disk = {"agent4": "some-other-model", "agent5": ""}
        self.assertIsNone(app._cd_disk_paths("agent4"))

    # ── _cd_trigger_swap: POST body + endpoint-down fallback ──────────────────
    def test_trigger_swap_posts_model_and_mmproj(self):
        import io, json as _json
        app = self._app(_log=lambda m: None)
        captured = {}

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = _json.loads(req.data.decode())
            return _Resp(b'{"ok":true,"loading":true}')

        with patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(app._cd_trigger_swap("agent4"))
        self.assertEqual(captured["url"], soc.CD_SWAP_URL)
        self.assertEqual(captured["body"]["model_path"], self.QWY)
        self.assertEqual(captured["body"]["mmproj_path"], self.QWY_MM)

    def test_trigger_swap_text_disk_omits_mmproj(self):
        import io, json as _json
        app = self._app(_log=lambda m: None)
        captured = {}

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            captured["body"] = _json.loads(req.data.decode())
            return _Resp(b'{"ok":true,"already":true}')

        with patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(app._cd_trigger_swap("agent5"))
        self.assertNotIn("mmproj_path", captured["body"])

    def test_trigger_swap_endpoint_down_is_false(self):
        logs = []
        app = self._app(_log=logs.append)
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(app._cd_trigger_swap("agent4"))
        self.assertTrue(any("unreachable" in m for m in logs))

    def test_trigger_swap_no_disk_resolved_is_false_without_network(self):
        app = self._app()
        app._cd_disk = {"agent4": "", "agent5": ""}
        # No urlopen patch: a network call here would error the test — proving
        # the no-match path never touches the endpoint.
        self.assertFalse(app._cd_trigger_swap("agent4"))

    # ── _cd_auto_swap: parks the message and starts ONE watcher ───────────────
    def test_auto_swap_parks_envelope_and_starts_watcher(self):
        app = self._app(_cd_parked={}, _cd_watchers=set(),
                        _cd_park_lock=threading.Lock())
        app._cd_disk_file_ok = lambda a: True   # file-existence guard (not under test here)
        app._cd_trigger_swap = lambda a: True
        started = []
        with patch.object(soc.threading, "Thread",
                          lambda **kw: type("T", (), {"start": lambda s: started.append(kw)})()):
            self.assertTrue(app._cd_auto_swap("agent4", "look at screen", "agent1"))
            self.assertTrue(app._cd_auto_swap("agent4", "second msg", "agent2"))
        self.assertEqual(len(app._cd_parked["agent4"]), 2)
        self.assertIn("To Agent4\nlook at screen\nend message now",
                      [e for e, _ in app._cd_parked["agent4"]])
        self.assertEqual(len(started), 1)          # ONE watcher for both messages

    def test_auto_swap_false_when_trigger_fails(self):
        app = self._app(_cd_parked={}, _cd_watchers=set(),
                        _cd_park_lock=threading.Lock())
        app._cd_disk_file_ok = lambda a: True   # isolate the trigger-fails path
        app._cd_trigger_swap = lambda a: False
        self.assertFalse(app._cd_auto_swap("agent4", "x", None))
        self.assertEqual(app._cd_parked, {})       # nothing parked on failure

    # ── _cd_disk_file_ok: refuse a swap to a moved/deleted file ───────────────
    def test_disk_file_missing_refuses_swap(self):
        # A magazine path to a deleted file would make the chatbox kill the
        # running server then fail to load — refuse it upstream + beacon.
        beacons = []
        app = self._app(_log=lambda m: None)
        app._cd_update_beacon = lambda t, c: beacons.append((t, c))
        with patch("os.path.exists", return_value=False):
            self.assertFalse(app._cd_disk_file_ok("agent4"))
        self.assertTrue(any("MISSING" in t for t, _ in beacons))

    def test_disk_file_present_allows_swap(self):
        app = self._app(_log=lambda m: None)
        app._cd_update_beacon = lambda t, c: None
        with patch("os.path.exists", return_value=True):
            self.assertTrue(app._cd_disk_file_ok("agent4"))

    def test_auto_swap_blocked_when_file_missing(self):
        # End-to-end: a missing file must NOT trigger a swap or park anything.
        triggered = []
        app = self._app(_cd_parked={}, _cd_watchers=set(),
                        _cd_park_lock=threading.Lock(), _log=lambda m: None)
        app._cd_update_beacon = lambda t, c: None
        app._cd_trigger_swap = lambda a: triggered.append(a) or True
        with patch("os.path.exists", return_value=False):
            self.assertFalse(app._cd_auto_swap("agent4", "look", "agent1"))
        self.assertEqual(triggered, [])            # never reached the swap trigger
        self.assertEqual(app._cd_parked, {})       # nothing parked

    # ── _cd_swap_watcher: graceful wait then redispatch ───────────────────────
    def _watcher_app(self, loaded_disk):
        routed = []
        app = self._app(
            _cd_parked={"agent4": [("To Agent4\nlook\nend message now", "agent1")]},
            _cd_watchers={"agent4"},
            _cd_park_lock=threading.Lock(),
            _cd_swap_for=None, _cd_swap_since=0.0, _cd_status_lbl=None,
            _log=lambda m: None,
        )
        app._cd_update_beacon = lambda t, c: None
        app._cd_loaded_disk = lambda force=False: loaded_disk
        app._route_text = lambda text, src: routed.append((text, src))
        return app, routed

    def test_watcher_redispatches_when_disk_up(self):
        app, routed = self._watcher_app(self.QWY)
        app._cd_swap_watcher("agent4")
        self.assertEqual(routed, [("To Agent4\nlook\nend message now", "agent1")])
        self.assertEqual(app._cd_parked, {})
        self.assertEqual(app._cd_watchers, set())

    def test_watcher_timeout_drops_parked(self):
        app, routed = self._watcher_app("wrong-disk.gguf")
        with patch.object(soc, "CD_SWAP_LOAD_TIMEOUT", 0.05), \
             patch.object(soc, "CD_SWAP_POLL_INTERVAL", 0.01):
            app._cd_swap_watcher("agent4")
        self.assertEqual(routed, [])               # nothing dispatched
        self.assertEqual(app._cd_parked, {})       # queue cleared (not leaked)
        self.assertEqual(app._cd_watchers, set())

    def test_local_agent_redispatch_uses_system_source(self):
        # THE shared-window bug: A6's message was parked with the recorded window
        # source "agent5" (A7's reply was OCR'd from the shared chatbox window).
        # Local agents all inject into the canonical agent5 window, so reusing
        # "agent5" as the source makes the directional self-route guard DROP the
        # delivery ("agent5 seen in its own window"). The redispatch MUST use a
        # distinct system source ("cd_changer") so the guard passes.
        routed = []
        app = self._app(
            _cd_parked={"agent6": [("To Agent6\nhi\nend message now", "agent5")]},
            _cd_watchers={"agent6"},
            _cd_park_lock=threading.Lock(),
            _cd_swap_for=None, _cd_swap_since=0.0, _cd_status_lbl=None,
            _log=lambda m: None,
        )
        app._cd_update_beacon = lambda t, c: None
        app._cd_loaded_disk = lambda force=False: self.GEMMA   # A6 auto-maps to slot-1 gemma
        app._cd_chat_clear = lambda: True
        app._route_text = lambda text, src: routed.append((text, src))
        with patch.object(soc, "CD_CHAT_CLEAR_SETTLE", 0):
            app._cd_swap_watcher("agent6")
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0][1], "cd_changer")   # NOT "agent5" (would self-skip)
        self.assertIn("To Agent6", routed[0][0])

    def test_agent4_redispatch_keeps_real_source(self):
        # A4 has its OWN window (HTTP), no directional collision, so its redispatch
        # must preserve the true origin for the mission banner.
        app, routed = self._watcher_app(self.QWY)
        app._cd_swap_watcher("agent4")
        self.assertEqual(routed[0][1], "agent1")

    # ── _cd_chat_clear: remote New-Chat between hops (hop hygiene) ────────────
    def test_chat_clear_posts_to_endpoint(self):
        import io
        app = self._app(_log=lambda m: None)
        captured = {}

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return _Resp(b'{"ok":true}')

        with patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(app._cd_chat_clear())
        self.assertEqual(captured["url"], soc.CD_CHAT_CLEAR_URL)
        self.assertEqual(captured["method"], "POST")

    def test_chat_clear_endpoint_down_is_false(self):
        logs = []
        app = self._app(_log=logs.append)
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(app._cd_chat_clear())
        self.assertTrue(any("chat clear unavailable" in m for m in logs))

    # ── watcher hop hygiene: window agents get a clear BEFORE redispatch ──────
    def test_watcher_does_not_clear_window_layers_persist(self):
        # Hop-hygiene window-wipe RETIRED (2026-07-14): the chatbox now keeps a
        # persistent conversation LAYER per slot, so the watcher must NOT clear on
        # a local-agent redispatch — clearing would destroy that agent's own
        # context and used to eat the reply on the clear boundary (blank window).
        # It just dispatches.
        order = []
        app = self._app(
            _cd_parked={"agent6": [("To Agent6\nhi\nend message now", "agent1")]},
            _cd_watchers={"agent6"},
            _cd_park_lock=threading.Lock(),
            _cd_swap_for=None, _cd_swap_since=0.0, _cd_status_lbl=None,
            _log=lambda m: None,
        )
        app._cd_update_beacon = lambda t, c: None
        app._cd_loaded_disk = lambda force=False: self.GEMMA  # A6's slot-2 disk
        app._cd_chat_clear = lambda: order.append("clear") or True
        app._route_text = lambda text, src: order.append("dispatch")
        app._cd_swap_watcher("agent6")
        self.assertEqual(order, ["dispatch"])      # NO clear — layers persist

    def test_watcher_no_clear_for_agent4_either(self):
        cleared = []
        app, routed = self._watcher_app(self.QWY)
        app._cd_chat_clear = lambda: cleared.append(True) or True
        app._cd_swap_watcher("agent4")
        self.assertEqual(cleared, [])              # no window wipe for any agent
        self.assertEqual(len(routed), 1)           # dispatch still happened


class LocalDiskAgentTests(unittest.TestCase):
    """A6/A7 extension: routing digits 6/7, magazine slot auto-mapping
    (A5→MODEL 1, A6→MODEL 2, A7→MODEL 3), and the local-agent head-guidance."""

    MAG = [
        {"model_path": r"C:\m\Qwythos.gguf", "mmproj_path": r"C:\m\mm-q.gguf"},
        {"model_path": r"C:\m\gemma4-v2-Q8_0.gguf", "mmproj_path": ""},
        {"model_path": r"C:\m\granite-vision-3.2-2b-Q8_0.gguf",
         "mmproj_path": r"C:\m\mmproj-granite.gguf"},
    ]

    def _app(self, tokens=None):
        app = bare_app(_cd_disk=tokens or {
            "agent4": "", "agent5": "", "agent6": "", "agent7": ""})
        app._cd_magazine = lambda: list(self.MAG)
        return app

    # ── routing regexes accept 6/7 ────────────────────────────────────────────
    def test_sentinel_re_matches_digit_7(self):
        m = soc.SENTINEL_RE.search("To Agent7\nHello from Agent 1!\nend message now")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "7")

    def test_inline_re_matches_digit_6(self):
        m = soc.INLINE_RE.search("to agent6: quick note")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "6")

    # ── top header takes priority over an inline directive in the body ────────
    def _route_app(self):
        """A bare app wired for _route_text's swap-park path: every disk reports
        'not loaded' so both block and inline routes PARK via _cd_auto_swap,
        which records the agent id."""
        parked = []
        app = bare_app(_mode="module_block", _self_mod_gate=None,
                       _log=lambda m: None)
        app._cd_disk_ready = lambda lid: (False, "swap needed")
        app._cd_auto_swap = lambda aid, body, src: (parked.append(aid) or True)
        return app, parked

    def test_block_header_suppresses_inline_in_body(self):
        # "To Agent7 … Relay to Agent6: …": the top envelope must win. Even though
        # A7's block PARKS for a swap (routed stays 0), the inline "to Agent6:" in
        # the body must NOT fork a second route. Regression for the routed-vs-
        # matched gate bug (a parked header left routed==0 and leaked the inline).
        app, parked = self._route_app()
        app._route_text(
            "To Agent7\nRelay to Agent6: say purple elephants\nend message now",
            "agent1")
        self.assertEqual(parked, ["agent7"])   # ONLY A7 — A6 inline suppressed

    def test_inline_fallback_still_fires_without_a_block(self):
        # No sentinel-delimited header at all → matched==0 → the inline fallback
        # must still route (guard against the gate change over-suppressing).
        app, parked = self._route_app()
        app._route_text("to agent6: say purple elephants", "agent1")
        self.assertEqual(parked, ["agent6"])

    # ── shared-window hold release on the local→local hop ─────────────────────
    def _relay_app(self):
        """A bare app wired for the local→local relay hop. A prior dispatch left
        the shared-window hold set (_waiting_reply='agent5'); every disk reports
        'not loaded' so the onward route PARKS via _cd_auto_swap (recording the
        agent id)."""
        parked = []
        class _Root:
            def after(self, *a, **k):
                pass
        app = bare_app(
            _mode="module_block", _self_mod_gate=None, _log=lambda m: None,
            _waiting_reply="agent5", _waiting_body_hash="prev", root=_Root(),
            _bridge_last_seen=0.0,
        )
        app._update_ocr_hold_label = lambda: None
        app._cd_disk_ready = lambda lid: (False, "swap needed")
        app._cd_auto_swap = lambda aid, body, src: (parked.append(aid) or True)
        return app, parked

    def test_local_hop_releases_shared_hold_so_redispatch_isnt_blocked(self):
        # THE local-relay killer (observed live 2026-07-14): A1→A5 set
        # _waiting_reply='agent5'. A5 (Qwythos) then replies 'To Agent6' — read
        # FROM the shared GGUF window (source 'agent5') and PARKED for the gemma
        # swap. Because the park bypasses _try_route, the normal hold-release
        # never fired, so _waiting_reply stayed 'agent5'; the A6 redispatch (also
        # _try_route('agent5')) was then blocked by the stale hold ('⏸ holding —
        # waiting for agent5 reply') and the parked message was dropped. The source
        # local agent HAS answered, so the shared-window hold must release here.
        app, parked = self._relay_app()
        app._route_text("To Agent6\nask Agent7 to solve 2+2\nend message now",
                        "agent5")
        self.assertEqual(parked, ["agent6"])         # parked for the gemma swap
        self.assertIsNone(app._waiting_reply)        # hold RELEASED (was 'agent5')

    def test_hold_not_released_when_reply_is_from_non_shared_window(self):
        # Only a genuine shared-window reply (source 'agent5') releases the hold.
        # A message that originates elsewhere (e.g. A1's own window) routed to a
        # local disk must leave a live 'agent5' hold untouched.
        app, parked = self._relay_app()
        app._route_text("To Agent6\nhello\nend message now", "agent1")
        self.assertEqual(parked, ["agent6"])
        self.assertEqual(app._waiting_reply, "agent5")   # untouched

    # ── magazine slot auto-mapping ────────────────────────────────────────────
    def test_slot_fallback_maps_agents_to_slots(self):
        app = self._app()
        self.assertEqual(app._cd_tokens("agent5"), ["qwythos.gguf"])
        self.assertEqual(app._cd_tokens("agent6"), ["gemma4-v2-q8_0.gguf"])
        self.assertEqual(app._cd_tokens("agent7"), ["granite-vision-3.2-2b-q8_0.gguf"])

    def test_explicit_token_overrides_slot_fallback(self):
        app = self._app(tokens={"agent4": "", "agent5": "", "agent6": "",
                                "agent7": "qwythos"})
        self.assertEqual(app._cd_tokens("agent7"), ["qwythos"])

    def test_agent7_resolves_slot3_disk_with_mmproj(self):
        app = self._app()
        self.assertEqual(
            app._cd_disk_paths("agent7"),
            (r"C:\m\granite-vision-3.2-2b-Q8_0.gguf", r"C:\m\mmproj-granite.gguf"))

    def test_empty_slot_yields_no_tokens(self):
        app = self._app()
        app._cd_magazine = lambda: [self.MAG[0], {"model_path": ""}, {}]
        self.assertEqual(app._cd_tokens("agent6"), [])
        self.assertEqual(app._cd_tokens("agent7"), [])

    def test_agent4_has_no_slot_fallback(self):
        app = self._app()
        self.assertEqual(app._cd_tokens("agent4"), [])   # token-only agent

    # ── head-guidance ─────────────────────────────────────────────────────────
    def test_local_header_names_agent_and_envelope(self):
        h = soc._local_agent_header("7")
        self.assertIn("you are Agent 7", h)
        self.assertIn("To Agent<number>", h)
        self.assertIn("end message now", h)
        self.assertTrue(h.endswith("\n\n"))     # separates cleanly from the body

    def test_agent6_header_teaches_the_workspace_tool(self):
        # Agent 6 (the hands) must be told about the a6-tool block on every message,
        # and still carry the envelope + clean trailing separator.
        h6 = soc._local_agent_header("6")
        self.assertIn("you are Agent 6", h6)
        self.assertIn("```a6-tool", h6)
        self.assertIn("create_folder", h6)
        self.assertIn("To Agent<number>", h6)
        self.assertIn("end message now", h6)
        self.assertTrue(h6.endswith("\n\n"))

    def test_tool_rail_is_agent6_only(self):
        # A5 and A7 are not the hands — they must NOT get the tool guidance.
        for other in ("5", "7"):
            self.assertNotIn("a6-tool", soc._local_agent_header(other),
                             f"Agent {other} must not get the A6 tool rail")

    def test_agent6_sop_covers_the_tool_and_ops(self):
        # The canonical A6 SOP (file or GROUND_RULES fallback) documents the block
        # format, every op, and the routing envelope.
        sop = soc.AGENT6_SOP
        self.assertIn("a6-tool", sop)
        for op in ("create_folder", "create_file", "read_file", "list_dir",
                   "add_workspace_folder", "remove_workspace_folder",
                   "list_workspace_folders"):
            self.assertIn(op, sop, f"SOP missing op {op}")
        self.assertIn("end message now", sop)


class HandsGuardTests(unittest.TestCase):
    """Stage-1 hands scheduler: the operator outranks agent hands, always."""

    def setUp(self):
        soc._hands_state["operator_until"] = 0.0
        soc._hands_state["synthetic_until"] = 0.0

    def tearDown(self):
        soc._hands_state["operator_until"] = 0.0
        soc._hands_state["synthetic_until"] = 0.0

    def test_wrapped_call_runs_and_marks_synthetic(self):
        calls = []
        fn = soc._hands_wrap(lambda: calls.append(1))
        fn()
        self.assertEqual(calls, [1])
        self.assertGreater(soc._hands_state["synthetic_until"], time.time() - 1)

    def test_worker_thread_waits_until_operator_idle(self):
        # Isolate from the live watcher. Importing soc_ultralight starts a 5 Hz
        # cursor poll that pushes operator_until forward on any real movement,
        # so a mouse twitch during the run could hold the worker past the join
        # and fail this spuriously (measured ~1 in 8). Drive the gate from a
        # fixed deadline instead: the behaviour under test is "a worker thread
        # yields until the operator is idle", not the watcher's detection,
        # which the attribution tests below cover separately.
        deadline = time.time() + 0.6
        fired = []
        fn = soc._hands_wrap(lambda: fired.append(time.time()))
        t0 = time.time()
        with patch.object(soc, "_hands_operator_active",
                          lambda: time.time() < deadline):
            th = threading.Thread(target=fn)
            th.start(); th.join(timeout=5)
        self.assertTrue(fired, "wrapped call never fired")
        self.assertGreaterEqual(fired[0] - t0, 0.5,
                                "agent hands did not yield to the operator")

    def test_main_thread_bypasses_gate(self):
        # Synthetic input from the Tk main thread is operator-initiated
        # (a button THEY clicked) — it must not deadlock against the human.
        soc._hands_state["operator_until"] = time.time() + 30
        calls = []
        fn = soc._hands_wrap(lambda: calls.append(1))
        t0 = time.time()
        fn()                      # unittest runs on the main thread
        self.assertEqual(calls, [1])
        self.assertLess(time.time() - t0, 1.0)

    def test_operator_active_flag(self):
        self.assertFalse(soc._hands_operator_active())
        soc._hands_state["operator_until"] = time.time() + 5
        self.assertTrue(soc._hands_operator_active())

    def test_pause_freezes_worker_hands_until_released(self):
        # Isolate from the live watcher: a real human moving the real mouse
        # during the test run would legitimately hold the operator gate too.
        soc._estop_set(True)
        fired = []
        fn = soc._hands_wrap(lambda: fired.append(1))
        with patch.object(soc, "_hands_operator_active", lambda: False):
            th = threading.Thread(target=fn)
            th.start()
            time.sleep(0.5)
            self.assertEqual(fired, [], "hands acted DURING a pause")
            soc._estop_set(False)
            th.join(timeout=5)
        self.assertEqual(fired, [1], "hands did not resume after pause release")

    def test_estop_does_not_block_main_thread_operator_input(self):
        soc._estop_set(True)
        try:
            fired = []
            soc._hands_wrap(lambda: fired.append(1))()   # main thread
            self.assertEqual(fired, [1])
        finally:
            soc._estop_set(False)

    # ── target-distance attribution (the burst-masking fix) ──────────────────
    def test_move_outside_grace_is_operator(self):
        soc._hands_state["synthetic_until"] = 0.0
        self.assertTrue(soc._hands_move_is_operator((500, 500)))

    def test_move_near_soc_target_in_grace_is_soc(self):
        soc._hands_state["synthetic_until"] = time.time() + 5
        soc._hands_state["target"] = (500.0, 500.0)
        self.assertFalse(soc._hands_move_is_operator((510, 505)))   # within radius

    def test_move_far_from_target_in_grace_is_operator(self):
        # THE fix: a human wiggle mid-SOC-burst lands off-target and is seen.
        soc._hands_state["synthetic_until"] = time.time() + 5
        soc._hands_state["target"] = (500.0, 500.0)
        self.assertTrue(soc._hands_move_is_operator((800, 300)))

    def test_move_in_grace_no_target_is_soc(self):
        soc._hands_state["synthetic_until"] = time.time() + 5
        soc._hands_state["target"] = None
        self.assertFalse(soc._hands_move_is_operator((640, 480)))

    def test_wrapped_coordinate_call_records_target(self):
        soc._hands_state["target"] = None
        fn = soc._hands_wrap(lambda x, y: None)
        fn(1234, 567)
        self.assertEqual(soc._hands_state["target"], (1234.0, 567.0))


class CopilotCopyPathTests(unittest.TestCase):
    """Regression: the A1 copy path referenced `skip_lead` without declaring it —
    every 'launching copy' died to a silent NameError in a windowless thread
    (observed live 2026-07-12 as an unexplained copy stall)."""

    def test_copilot_scan_declares_skip_lead(self):
        code = soc.SOCUltralight._ocr_force_scan_copilot.__code__
        self.assertIn("skip_lead", code.co_varnames[:code.co_argcount],
                      "skip_lead must be a real parameter, not a phantom global")

    def test_force_scan_passes_skip_lead_and_logs_errors(self):
        import inspect
        src = inspect.getsource(soc.SOCUltralight._ocr_force_scan)
        self.assertIn("_ocr_force_scan_copilot(skip_lead)", src)
        self.assertIn("copy path error", src)   # silent-death guard present


class AckEchoFilterTests(unittest.TestCase):
    """Pure-ack bodies are attendance echoes and must never route as content —
    routing one starts the self-reinforcing ack loop (observed live: A1 stuck
    emitting 'To Agent2 / SOC-ACK-1' forever)."""

    def test_plain_ack_matches(self):
        self.assertTrue(soc._PURE_ACK_RE.match("SOC-ACK-1"))
        self.assertTrue(soc._PURE_ACK_RE.match("soc ack 6"))
        self.assertTrue(soc._PURE_ACK_RE.match("  SOC-ACK-1.  "))

    def test_ocr_garbled_ack_matches(self):
        self.assertTrue(soc._PURE_ACK_RE.match("SOC-ACK-l"))    # l → 1 garble
        self.assertTrue(soc._PURE_ACK_RE.match("SOC-ACK-|"))

    def test_repeated_acks_match(self):
        self.assertTrue(soc._PURE_ACK_RE.match("SOC-ACK-1 SOC-ACK-1"))

    def test_real_content_does_not_match(self):
        self.assertFalse(soc._PURE_ACK_RE.match(
            "Tell Agent2 to send Agent1 the message: jello world"))
        self.assertFalse(soc._PURE_ACK_RE.match(
            "SOC-ACK-1 saved, ready for next block"))   # confirmation ≠ pure ack
        self.assertFalse(soc._PURE_ACK_RE.match("module block A1 complete"))


class Agent1StallScrollTests(unittest.TestCase):
    """A1 stall-breaker decision (_agent1_should_stall_scroll): scroll A1 to the
    bottom ONLY when the run is genuinely wedged on A1 — nothing routed recently
    AND no local agent mid-inference — so a healthy A5/6/7 ping-pong is never
    interrupted and the operator's own mousing always wins."""

    def _cfg(self, region=(1198, 103, 1631, 570)):
        import types as _t
        return _t.SimpleNamespace(ocr_region=region)

    def _app(self, now, **over):
        attrs = dict(
            _ocr_running=True,
            _estop=False,
            _gpu_holder=None,
            _last_route_time=now - soc.A1_STALL_SCROLL_AFTER - 5,
            agents={"agent1": self._cfg()},
        )
        attrs.update(over)
        return bare_app(**attrs)

    def test_fires_when_stalled(self):
        now = time.time()
        app = self._app(now)
        with patch.object(soc, "_hands_operator_active", lambda: False):
            self.assertTrue(app._agent1_should_stall_scroll(now))

    def test_silent_when_route_recent(self):
        now = time.time()
        app = self._app(now, _last_route_time=now - 2)
        with patch.object(soc, "_hands_operator_active", lambda: False):
            self.assertFalse(app._agent1_should_stall_scroll(now))

    def test_silent_during_local_inference(self):
        # A5/6/7 generating (GPU lock held as "agent5") is slow WORK, not a stall.
        now = time.time()
        app = self._app(now, _gpu_holder="agent5")
        with patch.object(soc, "_hands_operator_active", lambda: False):
            self.assertFalse(app._agent1_should_stall_scroll(now))

    def test_silent_when_ocr_off(self):
        now = time.time()
        app = self._app(now, _ocr_running=False)
        with patch.object(soc, "_hands_operator_active", lambda: False):
            self.assertFalse(app._agent1_should_stall_scroll(now))

    def test_silent_when_paused(self):
        now = time.time()
        app = self._app(now, _estop=True)
        with patch.object(soc, "_hands_operator_active", lambda: False):
            self.assertFalse(app._agent1_should_stall_scroll(now))

    def test_silent_when_operator_mousing(self):
        now = time.time()
        app = self._app(now)
        with patch.object(soc, "_hands_operator_active", lambda: True):
            self.assertFalse(app._agent1_should_stall_scroll(now))

    def test_silent_when_agent1_has_no_region(self):
        import types as _t
        now = time.time()
        app = self._app(now, agents={"agent1": _t.SimpleNamespace(ocr_region=None)})
        with patch.object(soc, "_hands_operator_active", lambda: False):
            self.assertFalse(app._agent1_should_stall_scroll(now))


class CopilotCopyCandidateTests(unittest.TestCase):
    """_copilot_copy_candidates — ordered, clamped, deduped click targets for the
    hover-dwell copy path. Fix for the recurring A1 stall (single blind click fired
    before Copilot's hover-revealed copy icon armed) AND the safety rule that it
    must only click confident OUTPUT-anchored points — never blind-click near the
    bottom, which can hit Copilot's INPUT copy icon and copy the wrong text."""

    def cand(self, copy_xy, fb_x=1498, sentinel_hover_y=500, ry0=100, ry1=570):
        return soc.SOCUltralight._copilot_copy_candidates(
            copy_xy, fb_x, sentinel_hover_y, ry0, ry1)

    def test_template_match_is_tried_first(self):
        c = self.cand((1346, 528))
        self.assertEqual(c[0], (1346, 528))

    def test_no_template_walks_sentinel_column_in_order(self):
        # AGENT1_COPY_NUDGES = (0, -8, 8, -16, 16) around the sentinel line 500
        c = self.cand(None, fb_x=1498, sentinel_hover_y=500)
        self.assertEqual(
            c, [(1498, 500), (1498, 492), (1498, 508), (1498, 484), (1498, 516)])

    def test_offsets_outside_window_are_dropped(self):
        # sentinel near the bottom edge: +8 / +16 fall past ry1 and must be dropped
        c = self.cand(None, fb_x=1498, sentinel_hover_y=568, ry0=100, ry1=570)
        ys = [y for _, y in c]
        self.assertTrue(all(100 <= y <= 570 for y in ys))
        self.assertIn(568, ys)          # 0 offset kept
        self.assertNotIn(576, ys)       # +8 past bottom → dropped
        self.assertNotIn(584, ys)       # +16 past bottom → dropped

    def test_template_outside_window_is_dropped(self):
        c = self.cand((1346, 999), fb_x=1498, sentinel_hover_y=500, ry0=100, ry1=570)
        self.assertNotIn((1346, 999), c)
        self.assertEqual(c[0], (1498, 500))   # falls through to the sentinel column

    def test_template_equal_to_sentinel_is_not_duplicated(self):
        c = self.cand((1498, 500), fb_x=1498, sentinel_hover_y=500)
        self.assertEqual(c.count((1498, 500)), 1)

    def test_no_anchor_yields_no_candidates(self):
        # SAFETY: no template AND no sentinel in view → NO click points at all,
        # so the caller scrolls to reveal the real button instead of blind-clicking
        # near the bottom (which could hit Copilot's INPUT copy icon).
        self.assertEqual(self.cand(None, sentinel_hover_y=None), [])

    def test_template_only_when_no_sentinel(self):
        # A confident template match is still honored even with no sentinel, but
        # NO speculative column is added below it.
        c = self.cand((1346, 528), sentinel_hover_y=None)
        self.assertEqual(c, [(1346, 528)])


class SocBridgeTests(unittest.TestCase):
    """Exact local-agent reply channel: the chatbox drops each completed A5/A6/A7
    reply as a file so SOC reads it VERBATIM (no OCR), and the live bridge
    suppresses the lossy OCR route for the shared window."""

    def _route_app(self, bridge_last_seen):
        parked = []
        class _Root:
            def after(self, *a, **k):
                pass
        app = bare_app(
            _mode="module_block", _self_mod_gate=None, _log=lambda m: None,
            _waiting_reply=None, _waiting_body_hash=None, root=_Root(),
            _bridge_last_seen=bridge_last_seen,
        )
        app._update_ocr_hold_label = lambda: None
        app._cd_disk_ready = lambda lid: (False, "swap needed")
        app._cd_auto_swap = lambda aid, body, src: (parked.append((aid, src)) or True)
        return app, parked

    # ── _bridge_active window ─────────────────────────────────────────────────
    def test_bridge_active_only_within_trust_window(self):
        app = bare_app(_bridge_last_seen=time.time())
        self.assertTrue(app._bridge_active())
        app._bridge_last_seen = time.time() - soc.BRIDGE_TRUST_WINDOW - 1
        self.assertFalse(app._bridge_active())
        app._bridge_last_seen = 0.0
        self.assertFalse(app._bridge_active())     # never seen → OCR keeps working

    def test_bridge_owns_only_agent5_when_active(self):
        # While the bridge is live it owns the shared window: OCR (route + scroll
        # auto-hunt) must stay off agent5 — but never off the other windows, and
        # never off agent5 when the bridge has not proven live (OCR fallback).
        app = bare_app(_bridge_last_seen=time.time())
        self.assertTrue(app._bridge_owns_window("agent5"))
        self.assertFalse(app._bridge_owns_window("agent1"))
        self.assertFalse(app._bridge_owns_window("agent2"))
        app._bridge_last_seen = 0.0
        self.assertFalse(app._bridge_owns_window("agent5"))

    # ── _route_text arbitration ───────────────────────────────────────────────
    def test_live_bridge_suppresses_ocr_route_for_shared_window(self):
        # OCR route (from_bridge=False) for the shared window is the corrupt
        # duplicate while the bridge is live → dropped, nothing parked.
        app, parked = self._route_app(time.time())
        n = app._route_text("To Agent6\nhi\nend message now", "agent5")
        self.assertEqual(n, 0)
        self.assertEqual(parked, [])

    def test_bridge_route_is_never_suppressed(self):
        # The exact file route (from_bridge=True) always proceeds, digit intact.
        app, parked = self._route_app(time.time())
        app._route_text("To Agent7\nsolve 2+2\nend message now",
                        "agent5", from_bridge=True)
        self.assertEqual([aid for aid, _ in parked], ["agent7"])

    def test_ocr_route_works_when_bridge_absent(self):
        # Older chatbox / no bridge files ever → OCR route still functions.
        app, parked = self._route_app(0.0)
        app._route_text("To Agent6\nhi\nend message now", "agent5")
        self.assertEqual([aid for aid, _ in parked], ["agent6"])

    def test_redispatch_not_suppressed_even_when_bridge_live(self):
        # The CD-changer redispatch (source 'cd_changer') must always land.
        app, parked = self._route_app(time.time())
        app._route_text("To Agent6\nhi\nend message now", "cd_changer")
        self.assertEqual([aid for aid, _ in parked], ["agent6"])

    # ── _bridge_scan_once: stability gate + verbatim route + archive ──────────
    def test_scan_stability_gate_then_routes_verbatim_and_archives(self):
        routed = []
        with tempfile.TemporaryDirectory() as td:
            replies = Path(td) / "replies"
            replies.mkdir()
            processed = replies / "processed"
            app = bare_app(_log=lambda m: None, _bridge_seen={},
                           _bridge_last_seen=0.0)
            app._route_text = (lambda text, source_agent=None, from_bridge=False:
                               routed.append((text, source_agent, from_bridge)) or 1)
            f = replies / "1_cd3.md"
            f.write_text("To Agent7\nsolve 2+2\nend message now", encoding="utf-8")
            with patch.multiple(soc, SOC_BRIDGE_REPLIES=replies,
                                SOC_BRIDGE_PROCESSED=processed):
                app._bridge_scan_once()                 # 1st poll: unstable, no route
                self.assertEqual(routed, [])
                app._bridge_scan_once()                 # 2nd poll: stable → route
            self.assertEqual(len(routed), 1)
            text, src, from_bridge = routed[0]
            self.assertEqual(src, "agent5")
            self.assertTrue(from_bridge)
            self.assertIn("To Agent7", text)            # digit survived (no OCR "?")
            self.assertGreater(app._bridge_last_seen, 0.0)
            self.assertFalse(f.exists())                # moved out of the inbox
            self.assertTrue((processed / "1_cd3.md").exists())

    def test_marker_write_records_pid_then_clear_removes_it(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "soc_bridge"
            marker = d / "soc_active.json"
            app = bare_app(_log=lambda m: None, _bridge_marker_at=0.0)
            with patch.multiple(soc, SOC_BRIDGE_DIR=d, SOC_BRIDGE_MARKER=marker):
                app._bridge_write_marker()
                self.assertTrue(marker.exists())
                self.assertIn('"pid"', marker.read_text(encoding="utf-8"))
                self.assertGreater(app._bridge_marker_at, 0.0)
                app._bridge_clear_marker()
                self.assertFalse(marker.exists())


class TitleMatchTests(unittest.TestCase):
    """Window (re)resolution match used by auto-locate AND snap-to-grid: a stale
    handle is refreshed by finding the window whose title still matches."""

    def test_prefix_contained_either_direction(self):
        # Live title carries an extra suffix beyond the saved 30-char prefix.
        self.assertTrue(soc._title_match("Copilot", "Copilot"))
        self.assertTrue(soc._title_match(
            "Plan Baxters OS Linux mi",
            "Plan Baxters OS Linux migration - Visual Studio Code"))
        self.assertTrue(soc._title_match("GGUF Chatbox", "GGUF Chatbox"))

    def test_case_insensitive(self):
        self.assertTrue(soc._title_match("gguf chatbox", "GGUF Chatbox"))

    def test_non_match_and_empty(self):
        # The desktop shell must NOT match a real agent window (the mis-lock bug).
        self.assertFalse(soc._title_match("GGUF Chatbox", "Program Manager"))
        self.assertFalse(soc._title_match("Copilot", "Visual Studio Code"))
        self.assertFalse(soc._title_match("", "anything"))
        self.assertFalse(soc._title_match("Copilot", ""))
        self.assertFalse(soc._title_match("(not set)", "Copilot"))


class CdRedispatchDedupTests(unittest.TestCase):
    """A CD-changer redispatch (source 'cd_changer') is authoritative: it must
    deliver to the freshly-loaded disk even when a prior/premature send already
    seeded the dedup guards — e.g. a fail-open dispatch before the swap (chatbox
    server not up yet at startup), or a non-fresh SOC restart re-running the same
    test. Plain OCR re-reads stay deduped so nothing double-sends."""

    def _app(self):
        injected = []
        class _Root:
            def after(self, *a, **k):
                pass
        app = bare_app(
            _paused=False, _estop=False,
            _bypass_agent3=False, _bypass_agent5=False,
            _manual_hold={}, _waiting_reply=None, _waiting_since=0.0,
            _last_routed_body={}, _seen_hashes=OrderedDict(),
            _dedup_lock=threading.Lock(), _pending_trigger={},
            _last_routed_text={}, _last_route_time=0.0, _welfare_fired=False,
            # Replay reset handshake records every handoff as it routes.
            _last_sent_by={}, _last_handoff=None,
            _mode="module_block", _self_mod_gate=None,
            _agent1_expect_since=0.0, _bridge_last_seen=0.0,
            _inject_grace={}, root=_Root(), _log=lambda m: None,
        )
        app._write_transcript   = lambda *a, **k: None
        app._set_pending_routed = lambda *a, **k: None
        app._update_ocr_hold_label = lambda: None
        app._cd_disk_ready      = lambda lid: (True, "loaded")
        app._agent_tool_capable = lambda aid: True   # adaptive-guidance probe (not under test here)
        app._gpu_try_acquire    = lambda aid: True
        app._gpu_release        = lambda aid: None
        app._inject_to_agent    = lambda aid, body: injected.append((aid, body))
        return app, injected

    ENV = "To Agent5\ndo the thing\nend message now"

    def test_redispatch_delivers_despite_dedup_from_prior_send(self):
        app, injected = self._app()
        # A prior send (fail-open before the swap, or an old test) records the body.
        self.assertEqual(app._route_text(self.ENV, "agent1"), 1)
        self.assertEqual(len(injected), 1)
        app._waiting_reply = None                       # an OCR restart clears the hold
        # The CD-changer redispatch of the SAME body must still land.
        self.assertEqual(app._route_text(self.ENV, "cd_changer"), 1)
        self.assertEqual(len(injected), 2)              # delivered again to the loaded disk

    def test_plain_ocr_reread_still_deduped(self):
        app, injected = self._app()
        self.assertEqual(app._route_text(self.ENV, "agent1"), 1)
        app._waiting_reply = None
        # A non-cd_changer re-read of the same body stays deduped (no double-send).
        self.assertEqual(app._route_text(self.ENV, "agent1"), 0)
        self.assertEqual(len(injected), 1)


class FormatReminderTests(unittest.TestCase):
    """SOC's own format reminders get OCR'd back off the agent window, so they
    must NEVER be router-parseable, and must be recipient-AGNOSTIC (a hardcoded
    'Agent[2]' made A1 ping Agent2 with a relayed answer, 2026-07-15)."""

    REMINDERS = None  # filled in setUp

    def setUp(self):
        self.REMINDERS = (soc.IMPL_FORMAT_REMINDER_AGENT1,
                          soc.IMPL_FORMAT_REMINDER_AGENT2)

    def test_reminders_never_router_parseable(self):
        for tmpl in self.REMINDERS:
            self.assertIsNone(soc.TRIGGER_RE.search(tmpl), f"TRIGGER_RE: {tmpl!r}")
            self.assertIsNone(soc.SENTINEL_RE.search(tmpl), f"SENTINEL_RE: {tmpl!r}")
            self.assertIsNone(soc.INLINE_RE.search(tmpl), f"INLINE_RE: {tmpl!r}")

    def test_reminders_are_recipient_agnostic(self):
        for tmpl in self.REMINDERS:
            self.assertIn("Agent<number>", tmpl)             # placeholder, not a digit
            self.assertNotRegex(tmpl, r"(?i)agent\s*\[?\d")  # no hardcoded recipient


class WelfareDueTests(unittest.TestCase):
    """Auto-welfare gate: never fire on an uninitialized region timestamp (the
    '56-year idle' that misfired the phantom format-guide to A1) nor mid-swap."""

    def test_uninitialized_timestamp_never_fires(self):
        # last_change <= 0 => the window was never scanned (uncalibrated).
        self.assertFalse(soc._welfare_due(0.0, 1000.0, 999.0, 120.0, False))
        self.assertFalse(soc._welfare_due(-5.0, 1000.0, 999.0, 120.0, False))

    def test_swap_in_flight_suppresses(self):
        # A CD disk is loading — the relay is progressing, not stalled.
        self.assertFalse(soc._welfare_due(100.0, 1000.0, 999.0, 120.0, True))

    def test_fires_when_genuinely_idle(self):
        self.assertTrue(soc._welfare_due(100.0, 1000.0, 500.0, 120.0, False))

    def test_not_idle_or_not_quiet_enough(self):
        self.assertFalse(soc._welfare_due(990.0, 1000.0, 500.0, 120.0, False))  # 10s idle
        self.assertFalse(soc._welfare_due(100.0, 1000.0, 50.0, 120.0, False))   # 50s route gap


class AdaptiveGuidanceTests(unittest.TestCase):
    """Per-model adaptive head-guidance: a tool-trained model (tool markers in its
    chat template) holds the routing envelope and gets the lean form; a plain
    model also gets an explicit relay-fidelity clause (fixes 'solve 2+2' -> '3+3')."""

    # ── _model_profile (pure) ─────────────────────────────────────────────────
    def test_tool_template_is_strong(self):
        p = soc._model_profile(
            r"C:\m\Qwen3-9B.gguf",
            "{% for message %} ... assistant tool_calls ... {% endfor %}")
        self.assertEqual(p["tier"], "strong")
        self.assertTrue(p["tool_capable"])
        self.assertEqual(p["name"], "Qwen3-9B")

    def test_plain_template_is_weak(self):
        p = soc._model_profile(
            r"C:\m\gemma4-v2-Q8_0.gguf",
            "<start_of_turn>user\n{{ content }}<end_of_turn>\n<start_of_turn>model\n")
        self.assertEqual(p["tier"], "weak")
        self.assertFalse(p["tool_capable"])

    def test_override_wins_over_template(self):
        # Operator forces a tool model DOWN to weak (or vice-versa).
        p = soc._model_profile(r"C:\m\gemma4-v2.gguf", "tool_calls",
                               overrides={"gemma": "weak"})
        self.assertEqual(p["tier"], "weak")
        self.assertFalse(p["tool_capable"])

    def test_unknown_template_defaults_weak(self):
        p = soc._model_profile(r"C:\m\mystery.gguf", "")
        self.assertEqual(p["tier"], "weak")     # safe default: extra guidance
        self.assertFalse(p["tool_capable"])

    # ── _local_agent_header (pure) ────────────────────────────────────────────
    def test_header_weak_gets_relay_fidelity(self):
        h = soc._local_agent_header("6", tool_capable=False)
        self.assertIn("RELAY FIDELITY", h)
        self.assertIn("do NOT solve", h)
        self.assertIn("you are Agent 6", h)
        self.assertIn("end message now", h)
        self.assertTrue(h.endswith("\n\n"))

    def test_header_strong_is_lean(self):
        h = soc._local_agent_header("5", tool_capable=True)
        self.assertNotIn("RELAY FIDELITY", h)
        self.assertIn("you are Agent 5", h)
        self.assertIn("To Agent<number>", h)
        self.assertTrue(h.endswith("\n\n"))

    # ── caching + wiring ──────────────────────────────────────────────────────
    def test_profile_cached_reads_template_once(self):
        calls = []
        app = bare_app(_model_profiles_cache={}, _model_profile_overrides={},
                       _log=lambda m: None)
        app._model_chat_template = lambda: (calls.append(1) or "assistant tool_calls")
        p1 = app._model_profile_cached(r"C:\m\Qwen.gguf")
        p2 = app._model_profile_cached(r"C:\m\Qwen.gguf")
        self.assertEqual(p1["tier"], "strong")
        self.assertEqual(len(calls), 1)         # template read once, then cached
        self.assertIs(p1, p2)

    def test_agent_tool_capable_uses_disk_and_template(self):
        app = bare_app(_model_profiles_cache={}, _model_profile_overrides={},
                       _log=lambda m: None)
        app._cd_disk_paths = lambda aid: (r"C:\m\gemma4.gguf", None)
        app._model_chat_template = lambda: ""       # plain → weak
        self.assertFalse(app._agent_tool_capable("agent6"))
        app2 = bare_app(_model_profiles_cache={}, _model_profile_overrides={},
                        _log=lambda m: None)
        app2._cd_disk_paths = lambda aid: (r"C:\m\Qwen.gguf", None)
        app2._model_chat_template = lambda: "tool_calls"
        self.assertTrue(app2._agent_tool_capable("agent7"))


class AutoHuntCooldownTests(unittest.TestCase):
    """Sentinel-only auto-hunt must back off after a failed hunt — otherwise the
    scroll it does changes the OCR hash, defeats the tick dedup, and it re-fires
    every tick forever (infinite scroll-churn observed live 2026-07-15)."""

    def test_suppressed_within_cooldown(self):
        app = bare_app(_auto_hunt_cool={"agent5": time.time() + 10})
        self.assertTrue(app._auto_hunt_suppressed("agent5"))

    def test_not_suppressed_after_cooldown_expires(self):
        app = bare_app(_auto_hunt_cool={"agent5": time.time() - 1})
        self.assertFalse(app._auto_hunt_suppressed("agent5"))

    def test_not_suppressed_when_never_hunted(self):
        app = bare_app(_auto_hunt_cool={})
        self.assertFalse(app._auto_hunt_suppressed("agent3"))


class _FakePlatform:
    """Recording backend proving SOC call sites go through the seam (S8)."""

    def __init__(self):
        self.calls = []
        self._cursor = (11, 22)

    def find_windows(self):
        self.calls.append(("find_windows",))
        return [(101, "Agent 1 — Copilot"), (102, "Other Window")]

    def window_from_point(self, x, y):
        self.calls.append(("window_from_point", x, y))
        return 101, "Agent 1 — Copilot", "Chrome_WidgetWin_1", (10, 20, 810, 620)

    def focus_window(self, hwnd):
        self.calls.append(("focus_window", hwnd))
        return True

    def get_window_rect(self, hwnd):
        self.calls.append(("get_window_rect", hwnd))
        return (10, 20, 810, 620)

    def is_window(self, hwnd):
        return True

    def move_window(self, hwnd, x, y, w, h):
        self.calls.append(("move_window", hwnd, x, y, w, h))
        return True

    def cursor_pos(self):
        self.calls.append(("cursor_pos",))
        return self._cursor

    def set_cursor_pos(self, x, y):
        self.calls.append(("set_cursor_pos", x, y))

    def left_button_down(self):
        return False

    def virtual_screen(self):
        return (0, 0, 1920, 1080)

    def install_input_hook(self, mark_operator):
        self.calls.append(("install_input_hook",))
        return False

    def acquire_instance_lock(self, name):
        return True

    def hide_own_console(self):
        pass

    def set_app_id(self, app_id):
        pass


class PlatformSeamTests(unittest.TestCase):
    """S8: the OS seam. SOC must reach the desktop ONLY through PLATFORM, and
    the interface must be identical across backends (win32 / x11 / fakes)."""

    INTERFACE = [
        "find_windows", "window_from_point", "focus_window", "get_window_rect",
        "is_window", "move_window", "cursor_pos", "set_cursor_pos",
        "left_button_down", "virtual_screen", "install_input_hook",
        "acquire_instance_lock", "hide_own_console", "set_app_id",
    ]

    def test_backends_implement_the_full_interface(self):
        from platform_layer.platform_win32 import Win32Platform
        from platform_layer.platform_x11 import X11Platform
        for backend in (Win32Platform, X11Platform, _FakePlatform):
            for method in self.INTERFACE:
                self.assertTrue(callable(getattr(backend, method, None)),
                                f"{backend.__name__} missing {method}")

    def test_get_platform_picks_win32_on_windows(self):
        import platform_layer
        platform_layer.set_platform(None)          # reset the singleton
        try:
            backend = platform_layer.get_platform()
            self.assertEqual(backend.name,
                             "win32" if sys.platform == "win32" else "x11")
        finally:
            platform_layer.set_platform(None)

    def test_no_direct_win32_imports_outside_platform_layer(self):
        """S8 acceptance: zero direct win32 imports in soc_ultralight.py."""
        src = (Path(soc.__file__)).read_text(encoding="utf-8", errors="replace")
        for needle in ("import win32api", "import win32gui", "import win32con",
                       "ctypes.windll"):
            self.assertNotIn(needle, src,
                             f"direct OS call left in soc_ultralight.py: {needle}")

    def test_scroll_agent_down_actually_scrolls(self):
        """REGRESSION: pre-S8 _scroll_agent_down referenced win32api with NO
        import in scope — the silent NameError (swallowed by except) meant the
        scroll NEVER fired. Through the seam it must fire exactly once."""
        fake = _FakePlatform()
        scrolls = []
        cfg = type("Cfg", (), {"ocr_region": (0, 0, 200, 100),
                               "scroll_dn_xy": None})()
        app = bare_app(agents={"agent1": cfg})
        with patch.object(soc, "PLATFORM", fake), \
             patch.object(soc.pyautogui, "scroll",
                          lambda *a, **k: scrolls.append(a)):
            app._scroll_agent_down("agent1")
        self.assertEqual(len(scrolls), 1, "scroll did not fire")
        self.assertIn(("cursor_pos",), fake.calls)
        self.assertIn(("set_cursor_pos", 11, 22), fake.calls)   # cursor restored

    def test_scroll_agent_up_actually_scrolls(self):
        """Same regression, other direction."""
        fake = _FakePlatform()
        scrolls = []
        cfg = type("Cfg", (), {"ocr_region": (0, 0, 200, 100)})()
        app = bare_app(agents={"agent1": cfg})
        with patch.object(soc, "PLATFORM", fake), \
             patch.object(soc.pyautogui, "scroll",
                          lambda *a, **k: scrolls.append(a)):
            app._scroll_agent_up("agent1", n=2)
        self.assertEqual(len(scrolls), 1, "scroll did not fire")
        self.assertEqual(scrolls[0][0], 10)                      # n*5 clicks up

    def test_auto_locate_goes_through_find_windows(self):
        fake = _FakePlatform()
        cfg = type("Cfg", (), {"title": "Agent 1", "hwnd": None,
                               "lbl_window": None})()
        cfg.lbl_window = type("L", (), {"config": lambda self, **kw: None})()
        app = bare_app(agents={"agent1": cfg}, _log=lambda *a, **k: None)
        with patch.object(soc, "PLATFORM", fake):
            app._auto_locate_windows()
        self.assertIn(("find_windows",), fake.calls)
        self.assertEqual(cfg.hwnd, 101)     # matched by loose title


class ReplayResetHandshakeTests(unittest.TestCase):
    """_welfare_check replays the last routed handoff into its destination.

    Replaces the old 'where am I' questionnaire, which asked both agents to
    self-report their position. Replaying the last real turn is what actually
    restarts a stalled sequence.
    """

    def _app(self, handoff, **over):
        self.injected = []

        class _Root:
            def after(self, *a, **k):
                pass

        attrs = dict(
            _last_handoff=handoff, _last_sent_by={},
            _last_routed_body={"agent2": "stale"},
            _waiting_reply=None, _waiting_since=0.0, _waiting_body_hash=None,
            _welfare_fired=False, _last_route_time=0.0,
            root=_Root(), _log=lambda m: None,
        )
        attrs.update(over)
        app = bare_app(**attrs)
        app._dedup_clear = lambda h: None
        app._update_ocr_hold_label = lambda: None
        app._inject_to_agent = lambda aid, body: self.injected.append((aid, body))
        return app

    def _run(self, app):
        """Run _welfare_check with the inject thread made synchronous."""
        class _Now:
            def __init__(self, target=None, daemon=None, **k):
                self._t = target

            def start(self):
                self._t()

        with patch.object(soc.threading, "Thread", _Now):
            app._welfare_check()

    def test_replays_last_handoff_into_its_destination(self):
        app = self._app(("agent1", "agent2", "To Agent2\nblock A-1\nend message now"))
        self._run(app)
        self.assertEqual(self.injected,
                         [("agent2", "To Agent2\nblock A-1\nend message now")])

    def test_reestablishes_the_hold_on_the_destination(self):
        body = "To Agent2\nblock A-1\nend message now"
        app = self._app(("agent1", "agent2", body))
        self._run(app)
        # The old questionnaire CLEARED the hold; the replay must re-arm it so SOC
        # waits for the destination's fresh reply exactly as a normal route would.
        self.assertEqual(app._waiting_reply, "agent2")
        self.assertEqual(app._waiting_body_hash, soc.SOCUltralight._msg_hash(body))
        self.assertTrue(app._welfare_fired)      # don't auto-repeat until routing resumes

    def test_clears_dedup_so_the_replay_is_not_swallowed(self):
        app = self._app(("agent1", "agent2", "body"))
        self._run(app)
        self.assertEqual(app._last_routed_body, {})

    def test_no_prior_handoff_is_a_no_op(self):
        app = self._app(None)
        self._run(app)
        self.assertEqual(self.injected, [])
        self.assertTrue(app._welfare_fired)      # still suppress the auto-retry loop

    def test_empty_body_is_a_no_op(self):
        app = self._app(("agent1", "agent2", "   "))
        self._run(app)
        self.assertEqual(self.injected, [])
        self.assertTrue(app._welfare_fired)


class _RecordingRoot:
    """A stand-in for the Tk root that records what _build_window asks of it.

    Only the calls _build_window actually makes, so the test documents the real
    dependency surface the same way bare_app() does elsewhere in this file.
    """

    def __init__(self):
        self.iconbitmap_calls = []

    def iconbitmap(self, bitmap=None, default=None):
        self.iconbitmap_calls.append({"bitmap": bitmap, "default": default})

    def title(self, *a):            pass
    def attributes(self, *a):       pass
    def overrideredirect(self, *a): pass
    def configure(self, **k):       pass
    def resizable(self, *a):        pass
    def protocol(self, *a):         pass
    def geometry(self, *a):         pass
    def bind(self, *a):             pass
    def winfo_screenwidth(self):    return 1920


class MainWindowIconTests(unittest.TestCase):
    """SOC's main window must carry SOC's own mark.

    It never called iconbitmap at all, so the icon it showed was whatever the
    V-plugin's `iconbitmap(default=...)` had left as the process-wide default —
    the A4v vision mark if the plugin had loaded, the bare pythonw icon if it
    had not. Which one you got depended on plugin load order. Neither was
    deliberate, and assets/ob_icon.ico is the OUTBOX mark, not an app icon.
    """

    ICON = Path(soc.__file__).resolve().parent / "assets" / "soc-ultralight.ico"

    def test_the_icon_asset_ships(self):
        self.assertTrue(self.ICON.is_file(), f"missing app icon: {self.ICON}")
        # A real ICONDIR: reserved=0, type=1 (icon), and at least one image.
        head = self.ICON.read_bytes()[:6]
        self.assertEqual(head[:4], b"\x00\x00\x01\x00", "not a valid .ico header")
        self.assertGreater(int.from_bytes(head[4:6], "little"), 0, "no images in .ico")

    def test_build_window_sets_the_icon_on_the_main_window(self):
        app = bare_app(root=_RecordingRoot(), _quit=lambda: None,
                       _drag_start=lambda e: None, _drag_move=lambda e: None)
        soc.SOCUltralight._build_window(app)
        calls = app.root.iconbitmap_calls
        self.assertTrue(calls, "_build_window set no icon — the main window "
                               "inherits whatever a plugin left as the default")
        # The window-scoped call is the load-bearing one: measured on Windows,
        # only that form survives a LATER iconbitmap(default=...) from a plugin.
        scoped = [c for c in calls if c["bitmap"] and not c["default"]]
        self.assertTrue(scoped, "icon was only set as the process default, so "
                                "the V-plugin's default= call takes it back")
        for c in calls:
            self.assertEqual(Path(c["bitmap"] or c["default"]).name,
                             "soc-ultralight.ico")

    def test_a_missing_asset_does_not_stop_soc_from_opening(self):
        app = bare_app(root=_RecordingRoot(), _quit=lambda: None,
                       _drag_start=lambda e: None, _drag_move=lambda e: None)
        with patch.object(soc.Path, "exists", lambda self: False):
            soc.SOCUltralight._build_window(app)      # must not raise
        self.assertEqual(app.root.iconbitmap_calls, [])


if __name__ == "__main__":
    unittest.main()
