#!/usr/bin/env python
"""Post-process a Flow image-generation run directory (no network, deterministic).

  check   <run>                 validate images -> <run>/check.json, PASS/FAIL (exit 0/1)
  montage <run> [--ids a,b]     numbered contact sheets -> <run>/montage_NN.jpg
  fit     <run> --size WxH      center-crop + LANCZOS downscale -> <run>/final_WxH/
  --self-test                   synthetic end-to-end test, prints PASS/FAIL
"""
import argparse, contextlib, hashlib, io, json, math, os, random, re, shutil, sys, tempfile
from PIL import Image, ImageDraw, ImageFont, ImageStat

FIT_SIZES = ("1080x1920", "1080x1080", "1920x1080")


def load_manifest(run):
    with open(os.path.join(run, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def entries(run, ids=None):
    """(item_id, label, rel_path, abs_path) for every manifest file, in manifest order."""
    out = []
    for item in load_manifest(run).get("items", []):
        if ids is not None and str(item.get("id")) not in ids:
            continue
        for rel in item.get("files") or []:
            label = os.path.splitext(os.path.basename(rel))[0]
            out.append((str(item.get("id")), label, rel, os.path.join(run, *rel.replace("\\", "/").split("/"))))
    return out


def dhash(img, n=16):
    # 256-bit: with 64 bits, distinct scenes sharing one reference image fell to distance 4,
    # overlapping re-encoded copies (measured 2026-11-13; 256-bit: distinct >= 21, copies <= 4)
    g = img.convert("L").resize((n + 1, n), Image.LANCZOS)
    px = g.tobytes()
    v = 0
    for r in range(n):
        for c in range(n):
            v = (v << 1) | (px[r * (n + 1) + c] > px[r * (n + 1) + c + 1])
    return v


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- check
def cmd_check(a):
    run, files, reasons, hashes = a.run, [], [], []
    for item_id, label, rel, path in entries(run):
        r = {"id": item_id, "file": rel, "ok": True, "flags": []}
        if not os.path.isfile(path):
            r.update(ok=False, flags=["missing"])
            reasons.append(f"missing: {rel}")
        else:
            try:
                with Image.open(path) as im:
                    im.load()
                    w, h = im.size
                    sd = ImageStat.Stat(im.convert("L")).stddev[0]
                    hv = dhash(im)
                r.update(width=w, height=h, stddev=round(sd, 2), dhash=f"{hv:064x}")
                hashes.append((rel, hv, r))
                if min(w, h) < a.min_side:
                    r["flags"].append("too_small")
                    reasons.append(f"too_small: {rel} min_side={min(w, h)} < {a.min_side}")
                if sd < a.blank_stddev:
                    r["flags"].append("blank")
                    reasons.append(f"blank: {rel} stddev={sd:.2f} < {a.blank_stddev}")
            except Exception as e:  # corrupt / unreadable image is a check result, not a crash
                r["flags"].append("unreadable")
                reasons.append(f"unreadable: {rel} {type(e).__name__}: {e}")
        files.append(r)
    dups = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            d = bin(hashes[i][1] ^ hashes[j][1]).count("1")
            if d <= a.dup_distance:
                dups.append({"a": hashes[i][0], "b": hashes[j][0], "distance": d})
                for k in (i, j):
                    if "duplicate" not in hashes[k][2]["flags"]:
                        hashes[k][2]["flags"].append("duplicate")
                reasons.append(f"duplicate: {hashes[i][0]} ~ {hashes[j][0]} hamming={d} <= {a.dup_distance}")
    for r in files:
        r["ok"] = not r["flags"]
    not_done = [str(i.get("id")) for i in load_manifest(run).get("items", []) if i.get("status") != "done"]
    if not files:
        reasons.append("no files in manifest")
    result = "PASS" if not reasons else "FAIL"
    summary = {"result": result, "files": len(files), "failed_files": sum(not r["ok"] for r in files),
               "duplicates": len(dups), "items_not_done": not_done,
               "params": {"min_side": a.min_side, "blank_stddev": a.blank_stddev, "dup_distance": a.dup_distance},
               "reasons": reasons}
    write_json(os.path.join(run, "check.json"), {"summary": summary, "files": files, "duplicates": dups})
    print(result)
    for x in reasons:
        print("  " + x)
    if not_done:
        print("  note: items not done (not a check failure): " + ", ".join(not_done))
    return 0 if result == "PASS" else 1


# ---------------------------------------------------------------- montage
def font(size):
    for name in ("arialbd.ttf", "arial.ttf", "malgunbd.ttf", "DejaVuSans-Bold.ttf"):
        with contextlib.suppress(OSError):
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def cmd_montage(a):
    run, cols, rows, cell, pad, band = a.run, a.cols, a.rows, a.cell, 8, 30
    ids = set(s.strip() for s in a.ids.split(",") if s.strip()) if a.ids else None
    for old in os.listdir(run):
        if re.fullmatch(r"montage_\d{2,}\.jpg", old):
            os.remove(os.path.join(run, old))
    items, skipped = [], []
    for item_id, label, rel, path in entries(run, ids):
        (items if os.path.isfile(path) else skipped).append((label, rel, path))
    fnt, per, mapping = font(22), cols * rows, {}
    for s in range(math.ceil(len(items) / per)):
        chunk = items[s * per:(s + 1) * per]
        nrows = math.ceil(len(chunk) / cols)
        sheet = Image.new("RGB", (cols * cell + (cols + 1) * pad, nrows * (cell + band) + (nrows + 1) * pad), (40, 40, 40))
        draw, cells = ImageDraw.Draw(sheet), []
        for k, (label, rel, path) in enumerate(chunk):
            x, y = pad + (k % cols) * (cell + pad), pad + (k // cols) * (cell + band + pad)
            num = s * per + k + 1
            try:
                with Image.open(path) as im:
                    im.draft("RGB", (cell, cell))
                    im = im.convert("RGB")
                    im.thumbnail((cell, cell), Image.LANCZOS)
                    sheet.paste(im, (x + (cell - im.width) // 2, y + band + (cell - im.height) // 2))
            except Exception as e:
                skipped.append((label, rel, f"{type(e).__name__}"))
            draw.rectangle([x, y, x + cell - 1, y + band - 1], fill=(255, 255, 255))
            draw.text((x + 6, y + band // 2), f"{num}. {label}", fill=(0, 0, 0), font=fnt, anchor="lm")
            cells.append({"n": num, "label": label, "file": rel})
        out = os.path.join(run, f"montage_{s + 1:02d}.jpg")
        sheet.save(out, "JPEG", quality=85)
        mapping[out] = cells
        print(out)
    print(json.dumps({"sheets": mapping, "skipped": [x[1] for x in skipped]}, ensure_ascii=False, indent=1))
    return 0 if items else 1


# ---------------------------------------------------------------- fit
def cmd_fit(a):
    m = re.fullmatch(r"(\d+)x(\d+)", a.size)
    tw, th = int(m.group(1)), int(m.group(2))
    outdir = os.path.join(a.run, f"final_{tw}x{th}")
    os.makedirs(outdir, exist_ok=True)
    done, fails = [], []
    for item_id, label, rel, path in entries(a.run):
        if not os.path.isfile(path):
            fails.append(f"missing: {rel}")
            continue
        with Image.open(path) as im:
            w, h = im.size
            cw, ch = (w, round(w * th / tw)) if w * th <= h * tw else (round(h * tw / th), h)
            if cw < tw or ch < th:
                fails.append(f"too_small_for_{tw}x{th}: {rel} {w}x{h} (crop {cw}x{ch})")
                continue
            l, t = (w - cw) // 2, (h - ch) // 2
            out = im.convert("RGB").resize((tw, th), Image.LANCZOS, box=(l, t, l + cw, t + ch))
        dst = os.path.join(outdir, label + ".jpg")
        out.save(dst, "JPEG", quality=95, subsampling=0)
        done.append(dst)
    print("PASS" if not fails else "FAIL", f"{len(done)} written to {outdir}")
    for x in fails:
        print("  " + x)
    return 0 if not fails and done else 1


# ---------------------------------------------------------------- self-test
def self_test():
    rnd = random.Random(1234)

    def art(w, h):
        im = Image.new("RGB", (w, h), tuple(rnd.randrange(256) for _ in range(3)))
        d = ImageDraw.Draw(im)
        for _ in range(25):
            x0, y0 = rnd.randrange(w), rnd.randrange(h)
            d.rectangle([x0, y0, x0 + rnd.randrange(w // 2), y0 + rnd.randrange(h // 2)],
                        fill=tuple(rnd.randrange(256) for _ in range(3)))
        return im

    def run_cmd(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def sha(p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    tmp = tempfile.mkdtemp(prefix="review selftest 한글 ")
    errs = []
    try:
        run = os.path.join(tmp, "런 01")
        os.makedirs(os.path.join(run, "images"))
        specs = {"a_1": art(2048, 2048), "b_1": art(2752, 1536), "blank_1": Image.new("RGB", (1024, 1024), (128, 128, 128)),
                 "small_1": art(500, 500)}
        for k, im in specs.items():
            im.save(os.path.join(run, "images", k + ".jpg"), quality=92)
        specs["a_1"].save(os.path.join(run, "images", "a_2.jpg"), quality=60)  # near-duplicate of a_1
        for n in range(12):
            art(1024, 1024).save(os.path.join(run, "images", f"c_{n + 1}.jpg"), quality=90)
        items = [{"id": "a", "prompt": "고양이 a", "ratio": "1:1", "count": 2, "status": "done", "files": ["images/a_1.jpg", "images/a_2.jpg"]},
                 {"id": "b", "prompt": "b", "ratio": "16:9", "count": 1, "status": "done", "files": ["images/b_1.jpg"]},
                 {"id": "blank", "prompt": "x", "ratio": "1:1", "count": 1, "status": "done", "files": ["images/blank_1.jpg"]},
                 {"id": "small", "prompt": "x", "ratio": "1:1", "count": 1, "status": "done", "files": ["images/small_1.jpg"]},
                 {"id": "gone", "prompt": "x", "ratio": "1:1", "count": 1, "status": "done", "files": ["images/gone_1.jpg"]},
                 {"id": "c", "prompt": "x", "ratio": "1:1", "count": 12, "status": "done", "files": [f"images/c_{n + 1}.jpg" for n in range(12)]}]
        write_json(os.path.join(run, "manifest.json"), {"items": items, "extra": {"opaque": True}})
        before = {p: sha(os.path.join(run, "images", p)) for p in os.listdir(os.path.join(run, "images"))}

        rc, out = run_cmd(["check", run])
        with open(os.path.join(run, "check.json"), encoding="utf-8") as f:
            cj = json.load(f)
        flags = {r["file"]: set(r["flags"]) for r in cj["files"]}
        exp = {"images/a_1.jpg": {"duplicate"}, "images/a_2.jpg": {"duplicate"}, "images/b_1.jpg": set(),
               "images/blank_1.jpg": {"blank"}, "images/small_1.jpg": {"too_small"}, "images/gone_1.jpg": {"missing"}}
        exp.update({f"images/c_{n + 1}.jpg": set() for n in range(12)})
        if rc != 1 or not out.startswith("FAIL") or flags != exp:
            errs.append(f"check bad: rc={rc} flags={flags}")

        clean = os.path.join(tmp, "clean")
        os.makedirs(os.path.join(clean, "images"))
        shutil.copy(os.path.join(run, "images", "b_1.jpg"), os.path.join(clean, "images", "b_1.jpg"))
        write_json(os.path.join(clean, "manifest.json"), {"items": [items[1]]})
        rc, out = run_cmd(["check", clean])
        if rc != 0 or not out.startswith("PASS"):
            errs.append(f"clean check bad: rc={rc} {out}")

        open(os.path.join(run, "montage_09.jpg"), "wb").close()  # stale own output must vanish
        rc, out = run_cmd(["montage", run])
        sheets = sorted(f for f in os.listdir(run) if f.startswith("montage_"))
        if rc != 0 or sheets != ["montage_01.jpg", "montage_02.jpg"]:
            errs.append(f"montage bad: rc={rc} sheets={sheets}")
        for s in sheets:
            with Image.open(os.path.join(run, s)) as im:
                if im.width > 1600:
                    errs.append(f"montage too wide: {s} {im.size}")
        rc, out = run_cmd(["montage", run, "--ids", "b,c"])
        sheets = sorted(f for f in os.listdir(run) if f.startswith("montage_"))
        if rc != 0 or sheets != ["montage_01.jpg"] or '"label": "b_1"' not in out or "a_1" in out:
            errs.append(f"montage --ids bad: rc={rc} sheets={sheets}")

        for size in FIT_SIZES:
            rc, out = run_cmd(["fit", run, "--size", size])
            d = os.path.join(run, f"final_{size}")
            got = sorted(os.listdir(d))
            # 1K sources are refused everywhere; 2752x1536 crops to 864 wide for 9:16, so b_1 is refused there
            want = ["a_1.jpg", "a_2.jpg"] + ([] if size == "1080x1920" else ["b_1.jpg"])
            if rc != 1 or got != want or "too_small_for" not in out or "missing: images/gone_1.jpg" not in out:
                errs.append(f"fit {size} bad: rc={rc} got={got}")
            for g in got:
                with Image.open(os.path.join(d, g)) as im:
                    if f"{im.width}x{im.height}" != size:
                        errs.append(f"fit {size} wrong size {g} {im.size}")
        rc, out = run_cmd(["fit", clean, "--size", "1920x1080"])
        if rc != 0 or not out.startswith("PASS"):
            errs.append(f"clean fit bad: {out}")

        after = {p: sha(os.path.join(run, "images", p)) for p in os.listdir(os.path.join(run, "images"))}
        if before != after:
            errs.append("originals modified")
    except Exception as e:
        errs.append(f"exception: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS" if not errs else "FAIL")
    for e in errs:
        print("  " + e)
    return 0 if not errs else 1


# ---------------------------------------------------------------- cli
def main(argv=None):
    p = argparse.ArgumentParser(prog="review.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--self-test", action="store_true", help="run synthetic end-to-end test")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="validate images, write check.json")
    c.add_argument("run")
    c.add_argument("--min-side", type=int, default=700, help="minimum short side px (default 700)")
    c.add_argument("--blank-stddev", type=float, default=8.0, help="grayscale stddev below this = blank (default 8.0)")
    c.add_argument("--dup-distance", type=int, default=10, help="256-bit dHash Hamming distance <= this = duplicate (default 10)")
    m = sub.add_parser("montage", help="numbered contact sheets")
    m.add_argument("run")
    m.add_argument("--ids", help="comma-separated item ids to include")
    m.add_argument("--cols", type=int, default=4)
    m.add_argument("--rows", type=int, default=4)
    m.add_argument("--cell", type=int, default=384, help="cell long side px (default 384)")
    f = sub.add_parser("fit", help="center-crop + downscale to exact size")
    f.add_argument("run")
    f.add_argument("--size", required=True, choices=FIT_SIZES)
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.cmd:
        p.print_help()
        return 2
    return {"check": cmd_check, "montage": cmd_montage, "fit": cmd_fit}[a.cmd](a)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
