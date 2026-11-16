#!/usr/bin/env python
"""Google Flow(Nano Banana 2.1) batch generator driven over CDP into the dedicated Windows Chrome.

Input  : prompts.jsonl, one object per line:
         {"id", "prompt", "ratio"?, "count"?, "ref_title"?, "quality"?}   (quality: "1k"|"2k")
Output : <run>/images/<id>_<n>.jpg, <run>/manifest.json, <run>/logs/chunk_XX.txt

Transport is Playwright CDP (tools/flow_browser.py) against a dedicated Chrome profile that the
user has signed in to Google by hand. Work happens in a background tab through the DevTools
protocol only: the user's mouse, keyboard and foreground windows are never touched, no Flow
internal API is called and no other image model is ever used as a fallback.

Reliability contract:
- `--doctor` is read-only and needs no prompts: it checks CDP reachability, the dedicated
  profile's Google sign-in and the project page readiness (prompt box present).
- Before generation a read-only preflight runs the same check, so a broken transport stops the
  run before any prompt is submitted. Failures become a structured `fatal` in manifest.json.
- Every item is persisted as `running` before work starts and `submitted` before the submit
  click. `submission_unresolved` records earlier attempts that may have submitted without a
  final `done`; it survives restarts, --force runs and pre-click failures, so no path can
  automatically resubmit a possibly-submitted item. Only `--force` authorizes a resubmission,
  and versioned names (a_1.v2.jpg) keep earlier output from ever being overwritten.
"""
import argparse, datetime, json, os, random, re, sys, tempfile, time
from pathlib import Path

from flow_browser import CDP_URL, FlowBrowser, TransportError, chrome_cmdline, cdp_version



QUALITY = {"1k": "1K 원본 크기", "2k": "2K 업스케일링됨"}
RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
PREFLIGHT_RETRY_SLEEP = 3
READY_STATES = ("ready",)
# results the browser reports before the submit click: a rerun cannot duplicate anything
PRE_CLICK_REASONS = ("ref_not_found", "submit_disabled")
# fatals raised before anything can be submitted: a retry needs no --force
PRE_CLICK_FATALS = ("flow_not_ready", "settings_mismatch")
PREFLIGHT_FATAL = {"login_required": "login_required", "flow_not_ready": "flow_not_ready"}
TRANSIENT_RE = re.compile(r"fetch failed|ECONNRESET|ECONNREFUSED|ETIMEDOUT|socket hang up|Target closed|WebSocket|browser has been closed|cdp connect failed", re.I)


def prior_unresolved(entry):
    """True when an earlier attempt may have submitted without a final `done` result."""
    entry = entry or {}
    if entry.get("submission_unresolved"):
        return True
    status = entry.get("status")
    return status in ("running", "submitted") or (status in ("partial", "failed") and bool(entry.get("submission_started")))


def load_items(path, ratio, count):
    items = []
    # utf-8-sig: Windows PowerShell 5 writes a BOM by default
    for i, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        o = json.loads(line)
        o.setdefault("id", f"p{i:04d}")
        o["ratio"] = o.get("ratio", ratio)
        o["count"] = int(o.get("count", count))
        o["quality"] = o.get("quality") or None
        if (o["ratio"] not in RATIOS or not 1 <= o["count"] <= 4 or not o.get("prompt")
                or o["quality"] not in (None, *QUALITY)):
            raise SystemExit(f"invalid prompt line {i}: {line[:120]}")
        items.append(o)
    ids = [o["id"] for o in items]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate ids in prompts file")
    return items


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def existing_files(run_dir, item_id):
    """Images on disk for an id: current `<id>_<n>[.v<k>]` and legacy `<id>_<n>[_<k>]` names."""
    pat = re.compile(r"^" + re.escape(str(item_id)) + r"_\d+(?:\.v\d+|_\d+)?\.jpg$")
    try:
        names = sorted(n for n in os.listdir(Path(run_dir) / "images") if pat.match(n))
    except OSError:
        return []
    return [f"images/{n}" for n in names]


def image_path(run_dir, item_id, n):
    """Next free images/<id>_<n>[.v<k>].jpg so no earlier output is ever overwritten.

    The `.v<k>` suffix cannot collide with another id's `<id>_<n>.jpg`, unlike the legacy `_k`
    suffix (id "a" version `a_1_2.jpg` vs id "a_1" image `a_1_2.jpg`).
    """
    path = Path(run_dir) / "images" / f"{item_id}_{n}.jpg"
    k = 1
    while path.exists():
        k += 1
        path = Path(run_dir) / "images" / f"{item_id}_{n}.v{k}.jpg"
    return path, f"images/{path.name}"


def is_ambiguous(entry, files):
    """True when an automatic rerun could duplicate a generation or clobber earlier output.

    Any leftover running/submitted state counts. Post-submit failed/partial results and any
    image already on disk block too. `submission_unresolved` carries earlier submission evidence
    across attempts and survives restarts, --force runs and pre-click failures, so a pre-click
    failure (ref_not_found/submit_disabled) retries without --force only when nothing before it
    could have submitted.
    """
    entry = entry or {}
    status = entry.get("status")
    if status == "done":
        return False
    if files:
        return True
    if entry.get("submission_unresolved"):
        return True
    if status == "failed" and entry.get("reason") in PRE_CLICK_REASONS and not entry.get("submission_started"):
        return False
    return status in ("running", "submitted", "partial", "failed") or bool(entry.get("submission_started"))


def preflight(br, project_url):
    """Read-only gate before generation. One retry only on exact connection-level evidence."""
    attempts = []
    d = None
    for attempt in (0, 1):
        try:
            d = br.probe(project_url)
        except TransportError as e:
            d = {"status": "connect_failed", "error": str(e)[:300]}
        attempts.append({k: d.get(k) for k in ("status", "error")})
        if d["status"] in READY_STATES:
            return True, dict(d, attempts=attempts)
        if attempt == 0 and TRANSIENT_RE.search(d.get("error") or ""):
            time.sleep(PREFLIGHT_RETRY_SLEEP)
            continue
        return False, dict(d, attempts=attempts)
    return False, dict(d, attempts=attempts)




def doctor_hints(d):
    common = [
        "연결이 확인되지 않으면 생성은 시작하지 않는다. 대체 이미지 모델로 자동 전환하지 않고, Flow 내부 API도 쓰지 않는다.",
    ]
    start = ("전용 Chrome을 시작한다: python3 tools/flow_browser.py start "
             "(또는 Windows 명령 프롬프트에서: " + chrome_cmdline() + ")")
    s = d.get("status")
    if s in READY_STATES:
        return ["전송·프로젝트 페이지 준비 확인됨. 실행: python3 tools/flow_run.py prompts.jsonl --project-url <URL>"]
    if s == "cdp_unreachable":
        return [start, "시작 후 --doctor를 다시 실행한다."] + common
    if s == "connect_failed":
        return [start, "CDP 응답이 있는데 붙지 못하면 전용 Chrome을 재시작한다(프로필·로그인은 그대로 둔다)."] + common
    if s == "login_required":
        return ["전용 Chrome 프로필에서 사용자가 직접 Google 로그인을 진행한다(로그아웃·쿠키 삭제·프로필 삭제 금지).",
                "로그인 후 --doctor를 다시 실행한다."] + common
    if s == "flow_not_ready":
        return ["프로젝트 페이지가 준비되지 않았다(프롬프트 입력창 없음). 프로젝트 URL이 맞는지 확인하고 전용 Chrome에서 "
                "해당 탭을 연뒤 --doctor를 다시 실행한다."] + common
    return ["--doctor를 다시 실행해 상태를 확인한다."] + common


def doctor(args):
    """Read-only diagnosis: no prompts needed, no submission, no tab is foregrounded."""
    hints = []
    ver = cdp_version()
    print(f"cdp      : {CDP_URL} {ver.get('Browser') if ver else 'NOT REACHABLE'}")
    if not args.project_url:
        print("project  : --project-url/FLOW_PROJECT_URL 없음")
        hints.append("생성에 쓸 Flow 프로젝트 URL을 --project-url 또는 FLOW_PROJECT_URL로 넘긴다.")
    d = {"status": "cdp_unreachable", "error": "CDP endpoint not reachable"}
    if ver:
        try:
            with FlowBrowser() as br:
                d = br.probe(args.project_url)
        except TransportError as e:
            d = {"status": "connect_failed", "error": str(e)[:300]}
        a = d.get("attached")
        print(f"probe    : status={d['status']}")
        if a:
            print(f"page     : {a['url']} prompt={a['has_prompt']} login={a['login']}")
        elif d.get("error"):
            print(f"page     : FAILED {d['error']}")
    ready = d["status"] in READY_STATES
    if args.prompts:
        try:
            print(f"prompts  : {len(load_items(args.prompts, args.ratio, args.count))} items ok")
        except SystemExit as e:
            print(f"prompts  : INVALID {e}")
            hints.append('프롬프트 jsonl을 고친다(한 줄 = {"id","prompt","ratio","count"}).')
    if args.run:
        mpath = Path(args.run) / "manifest.json"
        if mpath.exists():
            m = json.loads(mpath.read_text(encoding="utf-8"))
            counts, blocked = {}, []
            for it in m.get("items", []):
                counts[it.get("status")] = counts.get(it.get("status"), 0) + 1
                if is_ambiguous(it, existing_files(args.run, it.get("id"))):
                    blocked.append(it.get("id"))
            print(f"run      : {counts} ambiguous={blocked}")
            if m.get("fatal"):
                f = m["fatal"]
                print(f"fatal    : {f.get('fatal')} stage={f.get('stage')} exit={f.get('exit_code')} "
                      f"{f.get('error') or f.get('reason') or ''}".rstrip())
                hints.append("manifest의 fatal 원인을 해결한 뒤 다시 실행한다. 중단·제출 상태 항목은 --force 없이는 다시 제출하지 않는다(--force는 done 항목도 재생성하며 기존 이미지는 .v2, .v3 이름으로 보존한다).")
            if blocked:
                hints.append(f"중단/제출 의심 항목 {blocked}: Flow에 결과가 있는지 먼저 확인한다. 재생성은 --force로만 승인하고, 기존 산출물은 덮어쓰지 않고 images/에 버전 이름으로 남긴다.")
    hints.extend(doctor_hints(d))
    print("status   : " + ("READY" if ready else "NOT READY"))
    if hints:
        print("next     :")
        for h in hints:
            print(" - " + h)
    print(json.dumps({"doctor": d, "ready": ready, "next": hints}, ensure_ascii=False))
    return 0 if ready else 1


def start_entry(prev, o):
    """Fresh attempt entry. Earlier submission evidence survives in `submission_unresolved`."""
    entry = dict(o, status="running", started=now_iso(), at=now_iso(),
                 submission_unresolved=prior_unresolved(prev))
    if prev and prev.get("previous_files"):
        entry["previous_files"] = prev["previous_files"]
    return entry


def reset_pending(prev, o):
    """Pre-click fatal: nothing was submitted this attempt, but prior evidence still survives."""
    entry = dict(o, status="pending", at=now_iso(), submission_unresolved=prior_unresolved(prev))
    if prev and prev.get("previous_files"):
        entry["previous_files"] = prev["previous_files"]
    return entry


def finish_entry(by_id, run_dir, item_id, result, new_files):
    prev_e = by_id[item_id]
    entry = dict(prev_e, **{k: v for k, v in result.items() if k != "id"})
    # a final done result consumes the unresolved history; anything else keeps the carried flag
    entry["submission_unresolved"] = False if result.get("status") == "done" else bool(prev_e.get("submission_unresolved"))
    known = set(prev_e.get("files", [])) | set(prev_e.get("previous_files", [])) | set(existing_files(run_dir, item_id))
    entry["files"] = new_files
    kept = [f for f in sorted(known) if f not in set(new_files)]
    if kept:
        entry["previous_files"] = kept
    else:
        entry.pop("previous_files", None)
    entry["at"] = now_iso()
    by_id[item_id] = entry


def log_item(run_dir, chunk, idx, obj):
    path = Path(run_dir) / "logs" / f"chunk_{idx // chunk + 1:02d}.txt"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def run(args):
    items = load_items(args.prompts, args.ratio, args.count)
    run_dir = Path(args.run or f"artifacts/runs/{datetime.datetime.now():%Y%m%d-%H%M%S}-{Path(args.prompts).stem}")
    (run_dir / "images").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    mpath = run_dir / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"items": []}
    by_id = {m["id"]: m for m in manifest["items"]}
    todo, blocked = [], []
    for o in items:
        entry = by_id.get(o["id"])
        if args.force:
            todo.append(o)
        elif entry and entry.get("status") == "done":
            continue
        elif is_ambiguous(entry, existing_files(run_dir, o["id"])):
            blocked.append(o["id"])
        else:
            todo.append(o)
    todo = todo[: args.max_items]
    print(json.dumps({"run": str(run_dir), "total": len(items), "todo": len(todo), "ambiguous": blocked}, ensure_ascii=False))
    if blocked:
        print(json.dumps({"blocked": blocked, "hint": "이전 실행이 제출까지 갔을 수 있거나 산출물이 이미 있어 다시 제출하지 않는다. 재생성은 --force로만 하며 기존 파일은 덮어쓰지 않고 .v2, .v3 이름으로 남긴다"}, ensure_ascii=False))
    if args.dry_run:
        plan = [{"id": o["id"], "ratio": o["ratio"], "count": o["count"],
                 "quality": o.get("quality") or args.quality, "ref_title": o.get("ref_title") or ""} for o in todo]
        print(json.dumps({"dry_run": True, "items": plan}, ensure_ascii=False, indent=1))
        return 0
    fatal, done_since_rest = None, 0
    if todo:
        try:
            with FlowBrowser() as br:
                ok, pr = preflight(br, args.project_url)
                print(json.dumps({"preflight": pr["status"], "attempts": pr.get("attempts"),
                                  "error": pr.get("error")}, ensure_ascii=False))
                if not ok:
                    fatal = {"fatal": PREFLIGHT_FATAL.get(pr["status"], "transport_unavailable"),
                             "stage": "preflight", "status": pr["status"], "error": pr.get("error"),
                             "attempts": pr.get("attempts"), "at": now_iso(),
                             "hint": "python3 tools/flow_run.py --doctor 로 진단하고, 원인이 풀리기 전에는 생성하지 않는다"}
                    manifest["fatal"] = fatal
                    write_json(mpath, manifest)
                    print(json.dumps({"fatal": fatal}, ensure_ascii=False))
                    return 2
                for idx, o in enumerate(todo):
                    prev = by_id.get(o["id"])
                    by_id[o["id"]] = start_entry(prev, o)
                    manifest["items"] = list(by_id.values())
                    manifest["fatal"] = None
                    write_json(mpath, manifest)

                    def on_submit(item_id=o["id"]):
                        e = by_id[item_id]
                        e.update(status="submitted", submission_started=True, submission_unresolved=True, at=now_iso())
                        manifest["items"] = list(by_id.values())
                        write_json(mpath, manifest)

                    quality = QUALITY[o.get("quality") or args.quality]
                    result, images, extra_fatal = None, [], None
                    try:
                        result, images, extra_fatal = br.generate(
                            args.project_url, o, quality, on_submit, args.gen_timeout, 30)
                    except TransportError as e:
                        fatal = {"fatal": "transport_error", "reason": str(e)[:300], "ids": [o["id"]], "at": now_iso()}
                    new_files = []
                    for n, raw in images:
                        path, rel = image_path(run_dir, o["id"], n)
                        path.write_bytes(raw)
                        new_files.append(rel)
                    if result:
                        finish_entry(by_id, run_dir, o["id"], result, sorted(new_files))
                        print(json.dumps({"id": o["id"], "status": result["status"],
                                          "files": len(by_id[o["id"]].get("files", [])),
                                          "gen_s": result.get("gen_s")}, ensure_ascii=False))
                    if extra_fatal and not fatal:
                        fatal = dict(extra_fatal, at=now_iso())
                    if result is None and not fatal:
                        fatal = {"fatal": "incomplete_item", "reason": "no_result", "ids": [o["id"]], "at": now_iso()}
                    if fatal and result is None and fatal.get("fatal") in PRE_CLICK_FATALS:
                        # proven before the submit click: nothing was submitted this attempt,
                        # but earlier submission evidence (if any) still survives
                        by_id[o["id"]] = reset_pending(prev, o)
                    manifest["items"] = list(by_id.values())
                    manifest["fatal"] = fatal
                    write_json(mpath, manifest)
                    try:
                        log_item(run_dir, args.chunk, idx,
                                 {"id": o["id"], "status": by_id[o["id"]].get("status"),
                                  "result": result, "fatal": fatal})
                    except OSError:
                        pass  # a log-write failure must not abort a persisted item
                    if fatal:
                        break
                    done_since_rest += 1
                    if idx + 1 < len(todo):
                        if done_since_rest >= args.rest_every:
                            time.sleep(random.uniform(*args.rest))
                            done_since_rest = 0
                        else:
                            time.sleep(random.uniform(*args.gap))
        except TransportError as e:
            fatal = {"fatal": "transport_unavailable", "stage": "connect", "error": str(e)[:300], "at": now_iso(),
                     "hint": "python3 tools/flow_run.py --doctor 로 진단한다"}
            manifest["fatal"] = fatal
            write_json(mpath, manifest)
            print(json.dumps({"fatal": fatal}, ensure_ascii=False))
            return 2
        left = getattr(br, "staging_leftovers", [])
        if left:
            # files the tool did not start (or could not attribute) stay in staging: reported, not lost
            print(json.dumps({"staging_leftovers": left, "staging_dir": getattr(br, "staging_dir", ""),
                              "hint": "도구가 시작하지 않은 다운로드라 지우지 않았다"}, ensure_ascii=False))
    st = [m.get("status") for m in manifest["items"]]
    summary = {"done": st.count("done"), "partial": st.count("partial"), "failed": st.count("failed"),
               "submitted": st.count("submitted"), "blocked": blocked, "fatal": fatal}
    print(json.dumps(summary, ensure_ascii=False))
    return 2 if fatal else (3 if blocked else 0)


def self_test():
    ok = True
    d = Path(tempfile.mkdtemp())
    pf = d / "p.jsonl"
    pf.write_text('{"id":"a","prompt":"x \\"q\\" 한글"}\n\n{"prompt":"y","ratio":"9:16","count":2,"quality":"2k"}\n', encoding="utf-8")
    items = load_items(pf, "1:1", 1)
    ok &= [(o["id"], o["ratio"], o["count"], o["quality"]) for o in items] == [("a", "1:1", 1, None), ("p0003", "9:16", 2, "2k")]
    # versioned names: <id>_<n>.v<k>.jpg cannot collide with another id's <id>_<n>.jpg
    (d / "images").mkdir(exist_ok=True)
    (d / "images" / "a_1.jpg").write_bytes(b"\xff\xd8")
    ok &= image_path(d, "a", 1)[1] == "images/a_1.v2.jpg" and existing_files(d, "a") == ["images/a_1.jpg"]
    (d / "images" / "a_1.v2.jpg").write_bytes(b"\xff\xd8")
    ok &= existing_files(d, "a") == ["images/a_1.jpg", "images/a_1.v2.jpg"] and existing_files(d, "a_1") == []
    # ambiguity: earlier submission evidence survives pre-click failures
    ok &= is_ambiguous({"status": "running"}, []) and is_ambiguous({"status": "submitted"}, [])
    ok &= is_ambiguous({"status": "partial"}, []) and is_ambiguous({"status": "failed", "reason": "no_output"}, [])
    ok &= is_ambiguous(None, ["images/a_1.jpg"]) and not is_ambiguous({"status": "done"}, ["images/a_1.jpg"])
    ok &= not is_ambiguous({"status": "failed", "reason": "submit_disabled"}, [])
    ok &= is_ambiguous({"status": "failed", "reason": "submit_disabled", "submission_unresolved": True}, [])
    ok &= is_ambiguous({"status": "failed", "reason": "ref_not_found", "submission_started": True}, [])
    ok &= not is_ambiguous(None, []) and not is_ambiguous({"status": "pending"}, [])
    ok &= prior_unresolved({"status": "submitted"}) and prior_unresolved({"status": "running"})
    ok &= prior_unresolved({"status": "failed", "reason": "no_output", "submission_started": True})
    ok &= not prior_unresolved({"status": "done"}) and not prior_unresolved({"status": "failed", "reason": "submit_disabled"})
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def at_least_one(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 한다")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="exit: 0 ok, 2 fatal stop (see manifest.json fatal), "
                                        "3 ids left interrupted/submitted and not rerun (--force authorizes)")
    ap.add_argument("prompts", nargs="?", help="prompts.jsonl")
    ap.add_argument("--project-url", default=os.environ.get("FLOW_PROJECT_URL"), help="Flow project URL (or env FLOW_PROJECT_URL)")
    ap.add_argument("--run", help="run dir; reuse an existing one to resume (done ids are skipped)")
    ap.add_argument("--ratio", default="1:1", choices=RATIOS)
    ap.add_argument("--count", type=int, default=1, choices=[1, 2, 3, 4], help="images per prompt (x1-x4)")
    ap.add_argument("--quality", default="1k", choices=list(QUALITY))
    ap.add_argument("--chunk", type=at_least_one, default=1, help="items per log file (kept for compatibility; one persistent browser connection per run)")
    ap.add_argument("--gap", type=float, nargs=2, default=[5, 12], metavar=("MIN", "MAX"), help="random seconds between prompts")
    ap.add_argument("--rest-every", type=int, default=30, help="rest after this many prompts")
    ap.add_argument("--rest", type=float, nargs=2, default=[60, 180], metavar=("MIN", "MAX"))
    ap.add_argument("--gen-timeout", type=int, default=80)
    ap.add_argument("--max-items", type=int, default=280, help="cap per invocation (daily budget)")
    ap.add_argument("--force", action="store_true", help="rerun done ids, and items an earlier call left interrupted/submitted")
    ap.add_argument("--dry-run", action="store_true", help="print the item plan and exit (no browser)")
    ap.add_argument("--doctor", action="store_true", help="read-only transport/readiness diagnosis; no prompts needed, no submission")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.doctor:
        return doctor(a)
    if not a.prompts or not a.project_url:
        ap.error("prompts and --project-url (or FLOW_PROJECT_URL) are required")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
