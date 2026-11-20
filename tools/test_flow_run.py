#!/usr/bin/env python3
"""Regression tests for flow_run: transport preflight, fatal persistence and resume safety.

Run: python3 tools/test_flow_run.py
The browser boundary (flow_run.FlowBrowser) is mocked, so no browser, no Flow submission and no
image bytes beyond a tiny fake JPEG are involved.
"""
import contextlib, io, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_browser
import flow_run

READY_PROBE = {"status": "ready", "error": None,
               "attached": {"url": "https://flow.google.com/project/x", "has_prompt": True, "login": False}}
NOT_READY_PROBE = {"status": "flow_not_ready", "error": None,
                   "attached": {"url": "https://flow.google.com/project/x", "has_prompt": False, "login": False}}
LOGIN_PROBE = {"status": "login_required", "error": None,
               "attached": {"url": "https://accounts.google.com/x", "has_prompt": False, "login": True}}
CONNECT_FAIL_PROBE = {"status": "connect_failed", "error": "cdp connect failed: WebSocket error: ECONNREFUSED",
                      "attached": None}
JPEG = b"\xff\xd8fakejpeg"


class FakeBrowser:
    """Mocked browser boundary: scripted probe + per-item generate outcomes.

    A script step is {"result": ..., "images": [...], "fatal": ..., "clicked": bool, "raise": exc};
    "clicked" (default True) calls on_submit() first, like the real transport does right before
    the submit click.
    """

    def __init__(self, probe=None, script=(), leftovers=()):
        self.probe_result = READY_PROBE if probe is None else probe
        self.script = list(script)
        self.generated = []
        self.created = []
        self.probe_calls = 0
        self.staging_leftovers = list(leftovers)
        self.staging_dir = "C:\\fake\\staging"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def probe(self, project_url):
        self.probe_calls += 1
        return self.probe_result.pop(0) if isinstance(self.probe_result, list) else self.probe_result

    def create_project(self, title=None):
        url = "https://flow.google.com/project/new-%d" % (len(self.created) + 1)
        self.created.append(title)
        return {"url": url, "title": title, "title_set": True}

    def generate(self, project_url, item, quality_label, on_submit, gen_timeout_s, dl_timeout_s):
        self.project_url = project_url
        self.generated.append((dict(item), quality_label))
        step = self.script.pop(0)
        if step.get("clicked", True):
            on_submit()
        if step.get("raise"):
            raise step["raise"]
        return step.get("result"), step.get("images") or [], step.get("fatal")


def done_result(item_id="a"):
    return {"id": item_id, "status": "done", "reason": "", "gen_s": 1.0, "got": 1, "saved": 1,
            "verified": True, "titles": ["t"]}


def fail_result(item_id="a", reason="no_output", **kw):
    return {"id": item_id, "status": "failed", "reason": reason, "gen_s": 1.0, **kw}


class FlowRunSafety(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.prompts = self.root / "p.jsonl"
        self.prompts.write_text('{"id":"a","prompt":"cat on a mat"}\n', encoding="utf-8")
        self.run_dir = self.root / "run"

    def tearDown(self):
        self._tmp.cleanup()

    def invoke(self, fake, *extra, project=("--project-url", "https://flow.google.com/project/x")):
        argv = ["flow_run.py", str(self.prompts), *project, "--project-dir", str(self.root),
                "--run", str(self.run_dir), "--gap", "0", "0", *extra]
        out = io.StringIO()

        def no_browser():
            raise AssertionError("browser must not be created")

        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(flow_run.os.environ, {"FLOW_PROJECT_URL": ""}), \
             mock.patch.object(flow_run, "FlowBrowser", (lambda: fake) if fake is not None else no_browser), \
             mock.patch.object(flow_run, "cdp_version", lambda timeout=3: {"Browser": "Chrome/fake"}), \
             mock.patch.object(flow_run.time, "sleep", lambda s: None), \
             contextlib.redirect_stdout(out):
            code = flow_run.main()
        return code, out.getvalue()

    def state(self):
        return json.loads((self.root / flow_run.STATE_NAME).read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- per-folder project
    def test_first_run_creates_project_and_remembers_it(self):
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake, project=())
        self.assertEqual(code, 0)
        self.assertEqual(fake.created, [self.root.name], "new project is titled after the work folder")
        url = "https://flow.google.com/project/new-1"
        self.assertEqual(fake.project_url, url)
        self.assertEqual(self.state()["url"], url)
        self.assertEqual(self.manifest()["project_url"], url)

    def test_saved_project_is_reused_without_creating(self):
        flow_run.save_state(self.root, "https://flow.google.com/project/saved", "t", "created")
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake, project=())
        self.assertEqual(code, 0)
        self.assertEqual(fake.created, [])
        self.assertEqual(fake.project_url, "https://flow.google.com/project/saved")

    def test_resumed_run_keeps_its_own_project(self):
        flow_run.save_state(self.root, "https://flow.google.com/project/saved", "t", "created")
        self.run_dir.mkdir()
        flow_run.write_json(self.run_dir / "manifest.json",
                            {"items": [], "project_url": "https://flow.google.com/project/old-run"})
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        self.invoke(fake, project=())
        self.assertEqual(fake.project_url, "https://flow.google.com/project/old-run",
                         "ref_title targets live in the run's project")

    def test_new_project_replaces_saved_and_keeps_history(self):
        flow_run.save_state(self.root, "https://flow.google.com/project/saved", "t", "created")
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake, "--new-project", project=())
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.created), 1)
        st = self.state()
        self.assertEqual(st["url"], "https://flow.google.com/project/new-1")
        self.assertEqual([p["url"] for p in st["previous"]], ["https://flow.google.com/project/saved"])

    def test_explicit_url_is_remembered_for_the_folder(self):
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        self.invoke(fake)
        self.assertEqual(self.state()["url"], "https://flow.google.com/project/x")

    def test_project_creation_failure_is_fatal_before_any_submission(self):
        fake = FakeBrowser()
        fake.create_project = mock.Mock(side_effect=flow_browser.TransportError("login_required: x"))
        code, _ = self.invoke(fake, project=())
        self.assertEqual(code, 2)
        self.assertEqual(fake.generated, [])
        self.assertEqual(self.manifest()["fatal"]["fatal"], "login_required")
        self.assertFalse((self.root / flow_run.STATE_NAME).exists())

    def test_work_dir_is_the_project_folder_containing_cwd(self):
        base = self.root / "pw"
        (base / "1120_ASCII" / "work" / "1120").mkdir(parents=True)
        self.assertEqual(flow_run.work_dir(base / "1120_ASCII" / "work" / "1120", base), (base / "1120_ASCII").resolve())
        self.assertEqual(flow_run.work_dir(self.root, base), self.root.resolve())

    def manifest(self):
        return json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def item(self, item_id="a"):
        return next(i for i in self.manifest()["items"] if i["id"] == item_id)

    def seed_submitted(self, item_id="a", files=True):
        (self.run_dir / "images").mkdir(parents=True, exist_ok=True)
        entry = {"id": item_id, "prompt": "cat on a mat", "ratio": "1:1", "count": 1, "status": "submitted",
                 "submission_started": True, "started": "2026-11-16T13:00:00", "files": []}
        if files:
            (self.run_dir / "images" / f"{item_id}_1.jpg").write_bytes(b"\xff\xd8partial")
            entry["files"] = [f"images/{item_id}_1.jpg"]
        flow_run.write_json(self.run_dir / "manifest.json", {"items": [entry]})

    # ---------------------------------------------------------------- preflight / transport
    def test_preflight_not_ready_stops_before_generation(self):
        fake = FakeBrowser(probe=NOT_READY_PROBE)
        code, out = self.invoke(fake)
        self.assertEqual(fake.generated, [], "no generation before a passing preflight")
        self.assertEqual(fake.probe_calls, 1, "non-transient probe failure is not retried")
        self.assertEqual(code, 2)
        fatal = self.manifest()["fatal"]
        self.assertEqual(fatal["fatal"], "flow_not_ready")
        self.assertEqual(fatal["stage"], "preflight")
        self.assertEqual(self.manifest()["items"], [], "failed preflight must not mark items as submitted")

    def test_preflight_login_required_is_fatal(self):
        fake = FakeBrowser(probe=LOGIN_PROBE)
        code, _ = self.invoke(fake)
        self.assertEqual(fake.generated, [])
        self.assertEqual(code, 2)
        self.assertEqual(self.manifest()["fatal"]["fatal"], "login_required")

    def test_transient_connect_error_is_retried_once(self):
        fake = FakeBrowser(probe=[CONNECT_FAIL_PROBE, READY_PROBE],
                           script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake)
        self.assertEqual(fake.probe_calls, 2)
        self.assertEqual(code, 0)
        self.assertEqual(self.item()["status"], "done")

    def test_doctor_ready(self):
        fake = FakeBrowser()
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["flow_run.py", "--doctor", "--project-url", "https://flow.google.com/project/x"]), \
             mock.patch.object(flow_run, "FlowBrowser", lambda: fake), \
             mock.patch.object(flow_run, "cdp_version", lambda timeout=3: {"Browser": "Chrome/157"}), \
             contextlib.redirect_stdout(out):
            code = flow_run.main()
        self.assertEqual(code, 0)
        self.assertIn("READY", out.getvalue())

    def test_doctor_cdp_unreachable_prints_exact_start_command(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["flow_run.py", "--doctor"]), \
             mock.patch.object(flow_run, "cdp_version", lambda timeout=3: None), \
             contextlib.redirect_stdout(out):
            code = flow_run.main()
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertIn("flow_browser.py start", text)
        self.assertIn("--remote-debugging-port=9222", text)
        self.assertIn(r"--user-data-dir=C:\Users\houno\AppData\Local\flow-automation\chrome-profile", text)

    # ---------------------------------------------------------------- resume safety
    def test_submitted_item_is_not_resubmitted_on_resume(self):
        self.seed_submitted()
        fake = FakeBrowser()
        code, out = self.invoke(fake)
        self.assertEqual(fake.generated, [], "nothing to do: ambiguous item blocks the run")
        self.assertEqual(code, 3)
        self.assertEqual(self.item()["status"], "submitted")
        self.assertIn('"ambiguous": ["a"]', out)

    def test_pending_item_runs_while_ambiguous_item_stays_blocked(self):
        self.seed_submitted()
        self.prompts.write_text('{"id":"a","prompt":"cat on a mat"}\n{"id":"b","prompt":"dog"}\n', encoding="utf-8")
        fake = FakeBrowser(script=[{"result": done_result("b"), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake)
        self.assertEqual([g[0]["id"] for g in fake.generated], ["b"])
        self.assertEqual(self.item("b")["status"], "done")
        self.assertEqual(self.item("a")["status"], "submitted")
        self.assertEqual(code, 3)

    def test_force_authorizes_resubmission(self):
        self.seed_submitted()
        original = (self.run_dir / "images" / "a_1.jpg").read_bytes()
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake, "--force")
        self.assertEqual(fake.generated[0][0]["id"], "a")
        self.assertEqual(self.item()["status"], "done")
        self.assertEqual((self.run_dir / "images" / "a_1.jpg").read_bytes(), original)
        self.assertEqual(self.item()["files"], ["images/a_1.v2.jpg"])
        self.assertEqual(self.item()["previous_files"], ["images/a_1.jpg"])
        self.assertEqual(code, 0)

    def test_post_submit_failure_blocks_automatic_rerun(self):
        fake = FakeBrowser(script=[{"result": fail_result(reason="no_output")}])
        code, _ = self.invoke(fake)
        self.assertEqual(code, 0, "a recorded per-item failure is not a fatal stop")
        self.assertEqual(self.item()["status"], "failed")
        self.assertEqual(self.item()["reason"], "no_output")
        self.assertTrue(self.item()["submission_started"])
        self.assertTrue(self.item()["submission_unresolved"])
        fake2 = FakeBrowser()
        code2, _ = self.invoke(fake2)
        self.assertEqual(fake2.generated, [], "post-SUB failure must not be resubmitted automatically")
        self.assertEqual(code2, 3)

    def test_partial_download_blocks_and_force_preserves_original(self):
        original = b"\xff\xd8first-generation"
        (self.run_dir / "images").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "images" / "a_1.jpg").write_bytes(original)
        flow_run.write_json(self.run_dir / "manifest.json",
                            {"items": [{"id": "a", "prompt": "cat on a mat", "status": "partial",
                                        "submission_started": True, "files": ["images/a_1.jpg"]}]})
        fake = FakeBrowser()
        code, _ = self.invoke(fake)
        self.assertEqual(fake.generated, [])
        self.assertEqual(code, 3)
        self.assertEqual((self.run_dir / "images" / "a_1.jpg").read_bytes(), original)
        fake2 = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code2, _ = self.invoke(fake2, "--force")
        self.assertEqual(code2, 0)
        self.assertEqual((self.run_dir / "images" / "a_1.jpg").read_bytes(), original,
                         "original bytes must survive an explicit --force")
        self.assertEqual(self.item()["files"], ["images/a_1.v2.jpg"])
        self.assertEqual(self.item()["previous_files"], ["images/a_1.jpg"])
        self.assertTrue(self.item()["submission_started"])

    def test_pre_click_failure_retries_without_force(self):
        fake = FakeBrowser(script=[{"result": fail_result(reason="submit_disabled"), "clicked": False}])
        self.invoke(fake)
        self.assertEqual(self.item()["reason"], "submit_disabled")
        self.assertFalse(self.item().get("submission_unresolved"))
        self.assertFalse(self.item().get("submission_started"))
        fake2 = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code2, _ = self.invoke(fake2)
        self.assertEqual(fake2.generated[0][0]["id"], "a")
        self.assertEqual(code2, 0)
        self.assertEqual(self.item()["status"], "done")

    def test_force_pre_click_failure_keeps_unresolved_history(self):
        """Residual defect 2026-11-16: run1 SUB+kill -> run2 --force submit_disabled -> run3 must
        not resubmit. Earlier submission evidence survives until a final done result."""
        fake1 = FakeBrowser(script=[{"raise": flow_run.TransportError("target closed"), "clicked": True}])
        code1, _ = self.invoke(fake1)
        self.assertEqual(code1, 2)
        self.assertEqual(self.manifest()["fatal"]["fatal"], "transport_error")
        self.assertEqual(self.item()["status"], "submitted")
        self.assertTrue(self.item()["submission_unresolved"])
        fake2 = FakeBrowser(script=[{"result": fail_result(reason="submit_disabled"), "clicked": False}])
        code2, _ = self.invoke(fake2, "--force")
        self.assertEqual(code2, 0)
        self.assertEqual(self.item()["reason"], "submit_disabled")
        self.assertTrue(self.item()["submission_unresolved"],
                        "earlier submission evidence must survive a --force pre-click failure")
        fake3 = FakeBrowser()
        code3, _ = self.invoke(fake3)
        self.assertEqual(fake3.generated, [])
        self.assertEqual(code3, 3)

    def test_settings_mismatch_pending_reset_keeps_history(self):
        self.seed_submitted()
        fake = FakeBrowser(script=[{"result": None, "clicked": False,
                                    "fatal": {"fatal": "settings_mismatch", "settings": {}, "want": "1:1|1"}}])
        code, _ = self.invoke(fake, "--force")
        self.assertEqual(code, 2)
        self.assertEqual(self.manifest()["fatal"]["fatal"], "settings_mismatch")
        entry = self.item()
        self.assertEqual(entry["status"], "pending")
        self.assertTrue(entry["submission_unresolved"],
                        "a pre-click fatal must not erase earlier submission evidence")
        fake2 = FakeBrowser()
        code2, _ = self.invoke(fake2)
        self.assertEqual(fake2.generated, [])
        self.assertEqual(code2, 3)

    def test_crash_before_click_still_blocks_resume(self):
        fake = FakeBrowser(script=[{"raise": flow_run.TransportError("target closed"), "clicked": False}])
        code, _ = self.invoke(fake)
        self.assertEqual(code, 2)
        self.assertEqual(self.item()["status"], "running")
        self.assertFalse(self.item().get("submission_started", False))
        fake2 = FakeBrowser()
        code2, _ = self.invoke(fake2)
        self.assertEqual(fake2.generated, [], "a killed attempt still blocks automatic rerun")
        self.assertEqual(code2, 3)

    def test_missing_result_is_a_fatal_stop(self):
        fake = FakeBrowser(script=[{"result": None, "clicked": True}])
        code, _ = self.invoke(fake)
        self.assertEqual(code, 2)
        self.assertEqual(self.manifest()["fatal"]["fatal"], "incomplete_item")
        self.assertEqual(self.item()["status"], "submitted")

    # ---------------------------------------------------------------- naming / CLI surface
    def test_versioned_names_do_not_cross_ids(self):
        (self.run_dir / "images").mkdir(parents=True)
        (self.run_dir / "images" / "a_1.jpg").write_bytes(b"x")
        (self.run_dir / "images" / "a_1.v2.jpg").write_bytes(b"x")
        self.assertEqual(flow_run.existing_files(self.run_dir, "a"), ["images/a_1.jpg", "images/a_1.v2.jpg"])
        self.assertEqual(flow_run.existing_files(self.run_dir, "a_1"), [],
                         "id a_1 must not see id a's versioned names")
        _p, rel = flow_run.image_path(self.run_dir, "a", 1)
        self.assertEqual(rel, "images/a_1.v3.jpg")
        _p, rel2 = flow_run.image_path(self.run_dir, "a_1", 2)
        self.assertEqual(rel2, "images/a_1_2.jpg")

    def test_item_quality_override_reaches_download_menu(self):
        self.prompts.write_text('{"id":"a","prompt":"p","quality":"2k"}\n', encoding="utf-8")
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])
        code, _ = self.invoke(fake)
        self.assertEqual(fake.generated[0][1], "2K 업스케일링됨")
        self.assertEqual(code, 0)

    def test_dry_run_needs_no_browser(self):
        code, out = self.invoke(None, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn('"dry_run": true', out)
        self.assertIn('"id": "a"', out)

    def test_chunk_zero_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            self.invoke(FakeBrowser(), "--chunk", "0")
        self.assertEqual(cm.exception.code, 2, "--chunk must be validated to >= 1")

    def test_log_write_failure_does_not_lose_the_item_result(self):
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}])

        def boom(*a, **k):
            raise OSError("log disk full")

        with mock.patch.object(flow_run, "log_item", boom):
            code, _ = self.invoke(fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.item()["status"], "done", "the result must survive a log-write failure")
        self.assertEqual(self.item()["files"], ["images/a_1.jpg"])

    def test_staging_leftovers_are_reported_not_lost(self):
        fake = FakeBrowser(script=[{"result": done_result(), "images": [(1, JPEG)]}],
                           leftovers=["foreign-guid"])
        code, out = self.invoke(fake)
        self.assertEqual(code, 0)
        self.assertIn("staging_leftovers", out)
        self.assertIn("foreign-guid", out)

    def test_unresolved_download_stops_batch_and_keeps_later_items_resumable(self):
        self.prompts.write_text('{"id":"a","prompt":"p1"}\n{"id":"b","prompt":"p2"}\n', encoding="utf-8")
        fake = FakeBrowser(script=[
            {"result": {"id": "a", "status": "failed", "reason": "download_unresolved: timeout"},
             "fatal": {"fatal": "download_unresolved", "reason": "timeout", "ids": ["a"],
                       "hint": "이 항목(a)을 Flow에서 결과가 있는지 먼저 확인한다."}}])
        code, _ = self.invoke(fake)
        self.assertEqual(code, 2)
        self.assertEqual([g[0]["id"] for g in fake.generated], ["a"],
                         "the batch stops before a second download click is ever issued")
        fatal = self.manifest()["fatal"]
        self.assertEqual(fatal["fatal"], "download_unresolved")
        self.assertIn("Flow에서", fatal["hint"], "the hint must tell the user to check that item in Flow")
        self.assertTrue(self.item("a")["submission_unresolved"], "the current item stays blocked on resume")
        # item b was never submitted: resumable without --force, while a stays blocked
        fake2 = FakeBrowser(script=[{"result": done_result("b"), "images": [(1, JPEG)]}])
        code2, _ = self.invoke(fake2)
        self.assertEqual([g[0]["id"] for g in fake2.generated], ["b"])
        self.assertEqual(code2, 3)
        self.assertEqual(self.item("a")["submission_unresolved"], True)


class DownloadBinding(unittest.TestCase):
    """One download in flight at a time: a click's download is bound by a verified
    frame/host begin arriving after the click; anything unresolvable raises
    DownloadUnresolved and stops the batch. R1 stays closed (foreign files never adopted
    or deleted); the R2 ordering defect is closed by the stop-the-batch rule."""

    def tracker(self):
        self.dir = Path(tempfile.mkdtemp())
        return flow_browser.DownloadTracker(self.dir)

    def test_normal_download_is_adopted_and_consumed(self):
        t = self.tracker()
        mark = t.mark()
        t.feed({"kind": "begin", "guid": "mine", "url": "blob:https://flow.google.com/x", "verified": True})
        t.feed({"kind": "progress", "guid": "mine", "state": "completed"})
        (self.dir / "mine").write_bytes(b"\xff\xd8mine")
        self.assertEqual(t.fetch(mark, 1, 1, pump=lambda s: None), b"\xff\xd8mine")
        self.assertFalse((self.dir / "mine").exists(), "the bound file is consumed")

    def test_r1_foreign_frame_begin_is_never_adopted_or_deleted(self):
        """A user's JPEG download in another tab beginning after our click: not adopted,
        not deleted, and the download is unresolvable for our click."""
        t = self.tracker()
        mark = t.mark()
        (self.dir / "foreign-guid").write_bytes(b"\xff\xd8user")
        t.feed({"kind": "begin", "guid": "foreign-guid", "url": "https://example.com/x.jpg",
                "frameId": "other-frame", "verified": False})   # foreign frame/tab
        t.feed({"kind": "progress", "guid": "foreign-guid", "state": "completed"})
        with self.assertRaises(flow_browser.DownloadUnresolved) as cm:
            t.fetch(mark, 0.3, 0.3, pump=lambda s: None)
        self.assertEqual(cm.exception.reason, "no_begin")
        self.assertTrue((self.dir / "foreign-guid").exists(), "never adopted, never deleted")
        self.assertEqual(len(t.foreign), 1)

    def test_two_begin_ordering_cannot_misadopt(self):
        """Click 1's begin is missing in its window → the batch stops before a second
        click, so click 1's late JPEG can never surface as click 2's image."""
        t = self.tracker()
        mark1 = t.mark()
        with self.assertRaises(flow_browser.DownloadUnresolved) as cm:
            t.fetch(mark1, 0.2, 0.2, pump=lambda s: None)
        self.assertEqual(cm.exception.reason, "no_begin", "the batch stops here")
        # click 1's late download arrives only after the batch already stopped; and if a
        # second click ever ran, its window starts after these events (mark), so the late
        # begin is not even a candidate
        (self.dir / "g1").write_bytes(b"\xff\xd8late")
        t.feed({"kind": "begin", "guid": "g1", "url": "blob:https://flow.google.com/x", "verified": True})
        t.feed({"kind": "progress", "guid": "g1", "state": "completed"})
        self.assertTrue((self.dir / "g1").exists(), "no adoption, no deletion, no second click")

    def test_slow_download_stops_batch(self):
        t = self.tracker()
        mark = t.mark()
        t.feed({"kind": "begin", "guid": "g1", "verified": True})
        t.feed({"kind": "progress", "guid": "g1", "state": "inProgress"})
        with self.assertRaises(flow_browser.DownloadUnresolved) as cm:
            t.fetch(mark, 1, 0.2, pump=lambda s: None)
        self.assertEqual(cm.exception.reason, "timeout")

    def test_canceled_download_stops_batch(self):
        t = self.tracker()
        mark = t.mark()
        t.feed({"kind": "begin", "guid": "g1", "verified": True})
        t.feed({"kind": "progress", "guid": "g1", "state": "canceled"})
        with self.assertRaises(flow_browser.DownloadUnresolved) as cm:
            t.fetch(mark, 1, 1, pump=lambda s: None)
        self.assertEqual(cm.exception.reason, "canceled")

    def test_missing_guid_file_is_failure_not_adoption(self):
        t = self.tracker()
        (self.dir / "other").write_bytes(b"\xff\xd8other")  # an unrelated completed download
        mark = t.mark()
        t.feed({"kind": "begin", "guid": "g1", "verified": True})
        t.feed({"kind": "progress", "guid": "g1", "state": "completed"})
        self.assertIsNone(t.fetch(mark, 1, 1, pump=lambda s: None),
                          "a missing guid file must not fall back to a different file")
        self.assertTrue((self.dir / "other").exists())


class BeginProof(unittest.TestCase):
    """begin_matches: only project-page frames with Flow/Google hosts are provable."""

    def test_frame_and_host_proof(self):
        frames = {"f1"}
        hosts = flow_browser.expected_hosts("https://flow.google.com/project/x")
        self.assertTrue(flow_browser.begin_matches("blob:https://flow.google.com/uuid", "f1", frames, hosts))
        self.assertTrue(flow_browser.begin_matches("https://lh3.googleusercontent.com/a.jpg", "f1", frames, hosts))
        self.assertFalse(flow_browser.begin_matches("blob:https://flow.google.com/uuid", "other", frames, hosts),
                         "foreign frame")
        self.assertFalse(flow_browser.begin_matches("https://example.com/a.jpg", "f1", frames, hosts),
                         "foreign host")
        self.assertFalse(flow_browser.begin_matches("blob:https://flow.google.com/uuid", None, frames, hosts),
                         "a missing frameId is never proof")


class SignInClassify(unittest.TestCase):
    """Sign-in is decided by positive evidence (Google session cookie names + prompt box),
    never by page-text keywords. Regression tests for the 2026-11-16 review defect."""

    def test_signed_out_redirect_is_login_required(self):
        self.assertEqual(flow_browser.classify_signin("https://accounts.google.com/ServiceLogin?x=1", True, True),
                         "login_required")

    def test_signed_in_page_is_ready_even_with_login_words_on_screen(self):
        self.assertEqual(flow_browser.classify_signin("https://flow.google.com/project/x", True, True), "ready")

    def test_no_session_cookies_is_login_required_even_with_prompt_box(self):
        self.assertEqual(flow_browser.classify_signin("https://flow.google.com/project/x", True, False),
                         "login_required")

    def test_session_without_prompt_box_is_flow_not_ready(self):
        self.assertEqual(flow_browser.classify_signin("https://flow.google.com/project/x", False, True),
                         "flow_not_ready")


class FlowBrowserUnits(unittest.TestCase):
    """Transport helpers that need no browser."""

    def test_chrome_cmdline_carries_the_exact_flags(self):
        c = flow_browser.chrome_cmdline()
        for frag in ("--remote-debugging-port=9222",
                     "--remote-allow-origins=http://127.0.0.1:9222",
                     r"--user-data-dir=C:\Users\houno\AppData\Local\flow-automation\chrome-profile",
                     "--no-first-run", "--no-default-browser-check",
                     "--disable-background-timer-throttling",
                     "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding"):
            self.assertIn(frag, c)
        self.assertIn("chrome.exe", c)


if __name__ == "__main__":
    unittest.main(verbosity=2)