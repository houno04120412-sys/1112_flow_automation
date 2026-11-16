#!/usr/bin/env python3
"""Install the flow-image skill and the flow-run / flow-review launchers.

Skill copies: ~/.agents/skills (Codex/opencode), ~/.claude/skills (Claude Code),
<MIXDOG_DATA_DIR or ~/.mixdog/data>/skills (Mixdog). Launchers go to ~/.agents/bin and
point at this checkout; Pillow is installed into this checkout's .venv.
Re-run after `git pull` to update.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL = "flow-image"
REPO = Path(__file__).resolve().parent
SRC = REPO / "skills" / SKILL
VENV_PACKAGES = ["pillow==12.0.0", "playwright==1.65.2"]


def targets():
    home = Path.home()
    mixdog = Path(os.environ["MIXDOG_DATA_DIR"]) if os.environ.get("MIXDOG_DATA_DIR") else home / ".mixdog" / "data"
    found = [home / ".agents" / "skills"]
    if (home / ".claude").is_dir():
        found.append(home / ".claude" / "skills")
    if mixdog.is_dir():
        found.append(mixdog / "skills")
    return found


def launchers():
    venv_py = REPO / ".venv" / "bin" / "python"
    return {
        "flow-run": f'#!/usr/bin/env sh\nexec "{venv_py}" "{REPO}/tools/flow_run.py" "$@"\n',
        "flow-review": f'#!/usr/bin/env sh\nexec "{venv_py}" "{REPO}/tools/review.py" "$@"\n',
        "flow-browser": f'#!/usr/bin/env sh\nexec "{venv_py}" "{REPO}/tools/flow_browser.py" "$@"\n',
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print target paths only")
    a = ap.parse_args()
    bin_dir = Path.home() / ".agents" / "bin"
    for root in targets():
        dest = root / SKILL
        print(dest)
        if not a.dry_run:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for name, body in launchers().items():
        path = bin_dir / name
        print(path)
        if not a.dry_run:
            bin_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
    if not a.dry_run:
        venv = REPO / ".venv"
        if not (venv / "bin" / "python").exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(venv / "bin" / "pip"), "install", "-q", *VENV_PACKAGES], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
