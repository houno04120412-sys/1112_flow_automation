#!/usr/bin/env python
"""Google Flow(Nano Banana 2.1) batch generator driven through the logged-in Aside browser.

Input  : prompts.jsonl, one object per line: {"id", "prompt", "ratio"?, "count"?, "ref_title"?}
Output : <run>/images/<id>_<n>.jpg, <run>/manifest.json, <run>/logs/chunk_XX.txt

Each chunk is one `aside repl` call (the REPL opens a fresh background tab per call, closes it
and discards its session folder afterwards), so downloaded bytes travel back over stdout as base64.
Clicks and typing go through the browser's DevTools protocol, not the OS mouse/keyboard, and the
tab is never made the window's active tab, so the user's screen and input are left alone.
Selectors and Flow UI labels live only in JS_TEMPLATE.
"""
import argparse, base64, datetime, json, os, random, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

JS_TEMPLATE = r"""
const CFG = __CFG__;
const TILE = 'img[alt="사용자 이미지를 표시하는 타일"]';
const emit = (o) => console.log('RES|' + JSON.stringify(o));
// aside repl kills a call at 120 s, so every wait is bounded by the remaining call budget
const T0 = Date.now(); const left = () => CFG.budget_s * 1000 - (Date.now() - T0);
const pg = await openTab(CFG.project_url);
let ready = false;
for (let i = 0; i < 40 && !ready; i++) { await sleep(1000); ready = await pg.evaluate(() => !!document.querySelector('[role=textbox],textarea,[contenteditable=true]')); }
if (!ready || !pg.url().includes('flow.google.com')) { emit({fatal: 'flow_not_ready', url: pg.url()}); }
else {
// REPL sessions start without stored consent, and the cookie banner covers the submit button
const later = pg.getByRole('button', {name: '나중에', exact: true});
try { if (await later.first().isVisible()) { await later.first().click({timeout: 5000}); await sleep(800); } } catch (e) {}
const tileSrcs = async () => await pg.evaluate((s) => [...document.querySelectorAll(s)].map((i) => i.src), TILE);
const tree = async () => (await snapshot(pg)).tree;
// New tiles are identified by image src (titles arrive late and DOM order is not reliable);
// the tile's group gets a data-fr marker so it can be located for hover/menu actions.
const markBy = async (key, val, mark) => await pg.evaluate(([key, val, mark, s]) => {
  for (const i of document.querySelectorAll(s)) { const g = i.closest('[role=group]'); if (!g) continue;
    if ((key === 'src' && i.src === val) || (key === 'title' && g.getAttribute('aria-label') === val)) { g.setAttribute('data-fr', mark); return g.getAttribute('aria-label') || ''; } }
  return null; }, [key, val, mark, TILE]);
// "reCAPTCHA 로 보호됨" is a permanent footer badge, not a challenge
const problemLines = (t) => t.split('\n').filter((l) => !/reCAPTCHA 로 보호됨/.test(l) && /오류|실패|한도|제한|정책|위반|다시 시도|로그인|captcha|error|limit/i.test(l)).slice(0, 12);
let dl = null; pg.on('download', (d) => { dl = d; });

async function applySettings(ratio, count) {
  await pg.getByRole('button', {name: '설정 트리거'}).click(); await sleep(1000);
  await pg.evaluate(([ratio, count]) => { for (const r of document.querySelectorAll('[role=radio]')) { const t = r.textContent.trim(); if (t.endsWith('이미지') || t.endsWith(ratio) || t === 'x' + count) r.click(); } }, [ratio, count]);
  await sleep(800);
  const st = await pg.evaluate(() => [...document.querySelectorAll('[role=radio]')].filter((r) => r.getAttribute('aria-checked') === 'true').map((r) => r.textContent.trim()));
  const trig = ((await tree()).match(/설정 트리거"[^:\n]*: "(.*)"/) || [, ''])[1];
  await pg.keyboard.press('Escape'); await sleep(600);
  return {checked: st, trigger: trig};
}

async function openTileMenu(mark) {
  await pg.locator(`[data-fr="${mark}"] img`).hover(); await sleep(500);
  await pg.locator(`[data-fr="${mark}"] button[aria-label="옵션 더보기"]`).click(); await sleep(900);
  return true;
}
const closeMenus = async () => { await pg.keyboard.press('Escape'); await sleep(300); await pg.keyboard.press('Escape'); await sleep(400); };

let current = '';
for (const it of CFG.items) {
  const want = it.ratio + '|' + it.count;
  if (want !== current) {
    const s = await applySettings(it.ratio, it.count);
    const ok = s.trigger.includes(CFG.model_label) && s.trigger.includes('x' + it.count) && s.checked.some((c) => c.endsWith(it.ratio));
    if (!ok) { emit({fatal: 'settings_mismatch', settings: s, want}); break; }
    current = want;
  }
  try {
  const t0 = Date.now();
  let t = await tree();
  const clr = t.match(/프롬프트 지우기" \[ref=(\w+)\]/);
  if (clr) { await pg.locator(clr[1]).click(); await sleep(600); t = await tree(); }
  if (it.ref_title) {
    const found = await markBy('title', it.ref_title, 'ref-' + it.id);
    if (found === null || !(await openTileMenu('ref-' + it.id))) { emit({id: it.id, status: 'failed', reason: 'ref_not_found'}); await closeMenus(); continue; }
    await pg.getByRole('menuitem', {name: '프롬프트에 추가'}).click(); await sleep(1200);
  }
  const before = new Set(await tileSrcs());
  await pg.getByRole('textbox', {name: '무엇을 만들고 싶으신가요?'}).click();
  await pg.keyboard.insertText(it.prompt); await sleep(700);
  const btn = pg.getByRole('button', {name: '생성 시작'});
  if (await btn.isDisabled()) { emit({id: it.id, status: 'failed', reason: 'submit_disabled'}); continue; }
  await btn.click();
  let fresh = [];
  while (Date.now() - t0 < CFG.gen_timeout_s * 1000 && left() > 20000) { await sleep(2500); fresh = (await tileSrcs()).filter((s) => !before.has(s)); if (fresh.length >= it.count) break; }
  fresh = fresh.slice(0, it.count);
  const got = fresh.length;
  const genS = (Date.now() - t0) / 1000;
  if (got === 0) {
    const pl = problemLines(await tree());
    emit({id: it.id, status: 'failed', reason: 'no_output', gen_s: genS, problems: pl});
    if (pl.length) { emit({fatal: 'problem_text', problems: pl}); break; }
    continue;
  }
  await sleep(3000);
  const titles = [];
  for (let k = 0; k < got; k++) titles.push(await markBy('src', fresh[k], it.id + '-' + (k + 1)));
  await markBy('src', fresh[0], it.id + '-1');
  await pg.locator(`[data-fr="${it.id}-1"] img`).hover(); await sleep(800);
  const norm = (s) => s.replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase();
  const verified = norm(await tree()).includes(norm(it.prompt).slice(0, 40));
  let saved = 0;
  for (let k = 0; k < got; k++) {
    if (left() < 12000) break;
    dl = null;
    // re-mark right before use: Flow re-renders tiles on hover and drops the marker
    const mk = it.id + '-' + (k + 1);
    if ((await markBy('src', fresh[k], mk)) === null || !(await openTileMenu(mk))) { await closeMenus(); continue; }
    await pg.getByRole('menuitem', {name: '다운로드'}).hover(); await sleep(900);
    await pg.getByRole('menuitem', {name: CFG.quality_label}).click();
    for (let i = 0; i < CFG.dl_timeout_s * 2 && !dl; i++) await sleep(500);
    if (!dl) { await closeMenus(); continue; }
    const b = Buffer.from(await fs.readFile(await dl.path()));
    console.log('IMG|' + it.id + '|' + (k + 1) + '|' + b.toString('base64'));
    saved++; await sleep(800);
  }
  emit({id: it.id, status: saved === it.count ? 'done' : (saved ? 'partial' : 'failed'), reason: saved ? '' : 'download_failed', got, saved, gen_s: genS, verified, titles});
  } catch (e) { emit({id: it.id, status: 'failed', reason: 'error: ' + String(e && e.message || e).slice(0, 200)}); await closeMenus(); }
  if (it !== CFG.items[CFG.items.length - 1]) await sleep((CFG.gap_s[0] + Math.random() * (CFG.gap_s[1] - CFG.gap_s[0])) * 1000);
}
}
"""

QUALITY = {"1k": "1K 원본 크기", "2k": "2K 업스케일링됨"}
RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
CALL_BUDGET_S = 105  # aside repl hard-kills a call at 120 s (measured 2026-11-13)


def build_js(cfg):
    return JS_TEMPLATE.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))


def parse_output(text):
    """Split aside stdout into result records, images and leftover log lines."""
    results, images, log = [], [], []
    for line in text.splitlines():
        if line.startswith("RES|"):
            results.append(json.loads(line[4:]))
        elif line.startswith("IMG|"):
            _, item_id, n, b64 = line.split("|", 3)
            images.append((item_id, int(n), base64.b64decode(b64.strip())))
        else:
            log.append(line)
    return results, images, "\n".join(log)


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
        if o["ratio"] not in RATIOS or not 1 <= o["count"] <= 4 or not o.get("prompt"):
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


def run(args):
    items = load_items(args.prompts, args.ratio, args.count)
    run_dir = Path(args.run or f"artifacts/runs/{datetime.datetime.now():%Y%m%d-%H%M%S}-{Path(args.prompts).stem}")
    (run_dir / "images").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    mpath = run_dir / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"items": []}
    by_id = {m["id"]: m for m in manifest["items"]}
    todo = [o for o in items if args.force or by_id.get(o["id"], {}).get("status") != "done"][: args.max_items]
    print(json.dumps({"run": str(run_dir), "total": len(items), "todo": len(todo)}, ensure_ascii=False))
    base_cfg = {"project_url": args.project_url, "model_label": "Nano Banana 2.1", "quality_label": QUALITY[args.quality],
                "gen_timeout_s": args.gen_timeout, "dl_timeout_s": 30, "gap_s": args.gap, "budget_s": CALL_BUDGET_S}
    if args.dry_run:
        print(build_js(dict(base_cfg, items=todo[: args.chunk])))
        return 0
    aside = shutil.which("aside") or sys.exit("aside CLI not found")
    done_since_rest, fatal = 0, None
    for c in range(0, len(todo), args.chunk):
        chunk = todo[c: c + args.chunk]
        js = build_js(dict(base_cfg, items=chunk))
        if c:
            time.sleep(random.uniform(*args.gap))
        limit = 150
        try:
            p = subprocess.run([aside, "repl", "--file", "-"], input=js, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=limit)
            out = p.stdout + "\n" + p.stderr
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            out += "\nTIMEOUT"
        results, images, log = parse_output(out)
        (run_dir / "logs" / f"chunk_{c // args.chunk + 1:02d}.txt").write_text(log, encoding="utf-8")
        files = {}
        for item_id, n, raw in images:
            rel = f"images/{item_id}_{n}.jpg"
            (run_dir / rel).write_bytes(raw)
            files.setdefault(item_id, []).append(rel)
        src = {o["id"]: o for o in chunk}
        for r in results:
            if "fatal" in r:
                fatal = r
                continue
            entry = dict(src[r["id"]], **{k: v for k, v in r.items() if k != "id"})
            entry["files"] = sorted(files.get(r["id"], []))
            entry["at"] = datetime.datetime.now().isoformat(timespec="seconds")
            by_id[r["id"]] = entry
            print(json.dumps({"id": r["id"], "status": r["status"], "files": len(entry["files"]), "gen_s": r.get("gen_s")}, ensure_ascii=False))
        manifest["items"] = list(by_id.values())
        write_json(mpath, manifest)
        if fatal or "TIMEOUT" in out or not results:
            fatal = fatal or {"fatal": "aside_call_failed", "tail": log[-800:]}
            break
        done_since_rest += len(chunk)
        if c + args.chunk < len(todo) and done_since_rest >= args.rest_every:
            time.sleep(random.uniform(*args.rest))
            done_since_rest = 0
    st = [m.get("status") for m in manifest["items"]]
    summary = {"done": st.count("done"), "partial": st.count("partial"), "failed": st.count("failed"), "fatal": fatal}
    print(json.dumps(summary, ensure_ascii=False))
    return 2 if fatal else 0


def self_test():
    ok = True
    d = Path(tempfile.mkdtemp())
    pf = d / "p.jsonl"
    pf.write_text('{"id":"a","prompt":"x \\"q\\" 한글"}\n\n{"prompt":"y","ratio":"9:16","count":2}\n', encoding="utf-8")
    items = load_items(pf, "1:1", 1)
    ok &= [(o["id"], o["ratio"], o["count"]) for o in items] == [("a", "1:1", 1), ("p0003", "9:16", 2)]
    js = build_js({"project_url": "u", "model_label": "m", "quality_label": "q", "gen_timeout_s": 1, "dl_timeout_s": 1, "gap_s": [1, 2], "items": items})
    mod = d / "t.mjs"
    mod.write_text("async function openTab(){} async function sleep(){} async function snapshot(){} const fs={};\n" + js, encoding="utf-8")
    node = shutil.which("node")
    if node:
        ok &= subprocess.run([node, "--check", str(mod)], capture_output=True).returncode == 0
    raw = b"\xff\xd8jpegbytes"
    text = "noise\nRES|" + json.dumps({"id": "a", "status": "done"}) + "\nIMG|a|1|" + base64.b64encode(raw).decode() + "\n"
    res, imgs, log = parse_output(text)
    ok &= res == [{"id": "a", "status": "done"}] and imgs == [("a", 1, raw)] and log == "noise"
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompts", nargs="?", help="prompts.jsonl")
    ap.add_argument("--project-url", default=os.environ.get("FLOW_PROJECT_URL"), help="Flow project URL (or env FLOW_PROJECT_URL)")
    ap.add_argument("--run", help="run dir; reuse an existing one to resume (done ids are skipped)")
    ap.add_argument("--ratio", default="1:1", choices=RATIOS)
    ap.add_argument("--count", type=int, default=1, choices=[1, 2, 3, 4], help="images per prompt (x1-x4)")
    ap.add_argument("--quality", default="1k", choices=list(QUALITY))
    ap.add_argument("--chunk", type=int, default=1, help="prompts per aside call (a call must finish within 120 s)")
    ap.add_argument("--gap", type=float, nargs=2, default=[5, 12], metavar=("MIN", "MAX"), help="random seconds between prompts")
    ap.add_argument("--rest-every", type=int, default=30, help="rest after this many prompts")
    ap.add_argument("--rest", type=float, nargs=2, default=[60, 180], metavar=("MIN", "MAX"))
    ap.add_argument("--gen-timeout", type=int, default=80)
    ap.add_argument("--max-items", type=int, default=280, help="cap per invocation (daily budget)")
    ap.add_argument("--force", action="store_true", help="regenerate ids already done")
    ap.add_argument("--dry-run", action="store_true", help="print the first chunk's JS and exit")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.prompts or not a.project_url:
        ap.error("prompts and --project-url (or FLOW_PROJECT_URL) are required")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
