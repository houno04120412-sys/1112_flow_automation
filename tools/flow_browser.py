#!/usr/bin/env python
"""Dedicated-Chrome CDP transport for Google Flow batch generation (Aside-free).

The browser is a dedicated Windows Chrome profile the user has signed in to Google by hand;
never sign out, clear cookies or delete that profile directory. All UI steps go through the
DevTools protocol in a background tab: no OS mouse/keyboard, no window activation, no Flow
internal API, no other image model as a fallback. Selectors and Flow UI labels live only here.

Probed 2026-11-16 (Chrome 157, playwright 1.65.2, WSL2 mirrored networking):
- Playwright's Download.path()/save_as() return "canceled" over connect_over_cdp, so image
  bytes come from a per-run Windows staging folder (CDP Browser.setDownloadBehavior allowAndName)
  read over /mnt/c — the same file the "1K 원본 크기"/"2K 업스케일링됨" menu provides. Each
  saved file is bound to its click only when its CDP begin event provably belongs to that
  click (project-page frameId + expected Flow/Google host, arriving after the click). At most
  one download is ever in flight: an unresolvable download (no begin, not completed in time,
  canceled) stops the whole batch instead of letting another download click happen. Foreign
  or ambiguous downloads are never adopted or deleted.
- the cookie banner ("나중에") covers the bottom bar, so it is dismissed before clicking
  "설정 트리거"/"생성 시작" (tile hover/menu clicks are not covered by it).

CLI: flow_browser.py start  — launch the dedicated Chrome with the exact flags only when its
CDP endpoint is not already answering; flow_browser.py check — read-only reachability report.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CDP_URL = os.environ.get("FLOW_CDP_URL", "http://127.0.0.1:9222")
CHROME_EXE = "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
CHROME_ARGS = [
    "--remote-debugging-port=9222",
    "--remote-allow-origins=http://127.0.0.1:9222",
    r"--user-data-dir=C:\Users\houno\AppData\Local\flow-automation\chrome-profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # no window at launch (Chrome ignores `start /min` and takes focus, measured 2026-11-16);
    # the Flow tab is later opened in a new minimized, inactive window over CDP
    "--no-startup-window",
]
STAGING_ROOT_WIN = r"C:\Users\houno\AppData\Local\flow-automation\downloads"
STAGING_ROOT_WSL = Path("/mnt/c/Users/houno/AppData/Local/flow-automation/downloads")
# Google session cookie names, checked by NAME only; values are never read into output or logs
GOOGLE_SESSION_COOKIES = ("SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID")

HOME_URL = "https://flow.google.com/"
PROJECT_URL_RE = re.compile(r"^https://flow\.google\.com/project/[0-9a-f-]{36}")
NEW_PROJECT_NAME = "새 프로젝트"
TITLE_INPUT_NAME = "수정 가능한 텍스트"

TILE = 'img[alt="사용자 이미지를 표시하는 타일"]'
BOX_NAME = "무엇을 만들고 싶으신가요?"
SUBMIT_NAME = "생성 시작"
SETTINGS_NAME = "설정 트리거"
MODEL_LABEL = "Nano Banana 2.1"
LATER_NAME = "나중에"
MORE_OPTIONS = "옵션 더보기"
DOWNLOAD_MENU = "다운로드"
ADD_TO_PROMPT_MENU = "프롬프트에 추가"
CLEAR_NAME = "프롬프트 지우기"

PROBLEM_RE = re.compile(r"오류|실패|한도|제한|정책|위반|다시 시도|로그인|captcha|error|limit", re.I)
# "reCAPTCHA 로 보호됨" is a permanent footer badge, not a challenge
BADGE_RE = re.compile(r"reCAPTCHA 로 보호됨")
CLOSED_RE = re.compile(r"Target closed|browser has been closed|Connection closed|"
                       r"target page, context or browser has been closed", re.I)

MARK_JS = """([key, val, mark, s]) => {
  for (const i of document.querySelectorAll(s)) {
    const g = i.closest('[role=group]');
    if (!g) continue;
    if ((key === 'src' && i.src === val) || (key === 'title' && g.getAttribute('aria-label') === val)) {
      g.setAttribute('data-fr', mark);
      return g.getAttribute('aria-label') || '';
    }
  }
  return null;
}"""
TILE_SRCS_JS = "(s) => [...document.querySelectorAll(s)].map((i) => i.src)"
RADIO_CLICK_JS = """([ratio, count]) => {
  for (const r of document.querySelectorAll('[role=radio]')) {
    const t = r.textContent.trim();
    if (t.endsWith('이미지') || t.endsWith(ratio) || t === 'x' + count) r.click();
  }
}"""
CHECKED_JS = ("() => [...document.querySelectorAll('[role=radio]')]"
              ".filter((r) => r.getAttribute('aria-checked') === 'true').map((r) => r.textContent.trim())")
BODY_JS = "() => document.body.innerText"
# Flow promo/notice dialogs (e.g. "20 Flow 크레딧 받기", seen 2026-11-20) set aria-hidden on the
# whole app, so the prompt box and tile menus vanish from role queries. Only the dialog's own
# close (X) button is clicked: no offer is accepted or declined.
CLOSE_BACKDROPS_JS = """() => {
  const b = [...document.querySelectorAll('.cdk-overlay-backdrop-showing')];
  b.forEach((x) => x.click());
  return b.length;
}"""
CLOSE_DIALOGS_JS = """() => {
  const out = [];
  for (const d of document.querySelectorAll('mat-dialog-container,[role=dialog],[aria-modal=true]')) {
    if (!d.offsetParent && getComputedStyle(d).position !== 'fixed') continue;
    const b = [...d.querySelectorAll('button')].find((x) => x.innerText.trim() === 'close'
      || ['닫기', 'close', 'Close'].includes(x.getAttribute('aria-label') || ''));
    if (!b) continue;
    out.push(d.innerText.trim().split('\\n').filter(Boolean).slice(0, 3).join(' / ').slice(0, 120));
    b.click();
  }
  return out;
}"""


class TransportError(RuntimeError):
    """Connection-level failure (CDP unreachable, browser/page gone)."""


def _norm(s):
    return "".join(ch for ch in (s or "") if ch.isalnum()).lower()


def cdp_version(timeout=3.0):
    """Browser version dict from the CDP endpoint, or None when unreachable."""
    try:
        with urllib.request.urlopen(CDP_URL + "/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def chrome_cmdline():
    """The exact Windows command that starts the dedicated Chrome (for hints/docs)."""
    exe = "C:" + CHROME_EXE[len("/mnt/c"):].replace("/", "\\")
    parts = ['"%s"' % exe] + [a if " " not in a else "'%s'" % a for a in CHROME_ARGS]
    return " ".join(parts)


def start_chrome(wait_s=20):
    """Start the dedicated Chrome only when its CDP endpoint is not already answering."""
    if cdp_version():
        return "already_running"
    if not os.path.exists(CHROME_EXE):
        return "chrome_not_found"
    subprocess.Popen([CHROME_EXE] + CHROME_ARGS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if cdp_version():
            return "started"
        time.sleep(0.5)
    return "failed"


def classify_signin(url, has_prompt, has_session):
    """Readiness from positive evidence: a Google session (cookie names) plus the prompt box.

    Keyword scanning of page text is deliberately not used: signed-in pages can mention
    "로그인"/"Sign in", and a sign-in wall can still show a prompt box.
    """
    if "accounts.google.com" in (url or ""):
        return "login_required"
    if not has_session:
        return "login_required"
    return "ready" if has_prompt else "flow_not_ready"


def _url_host(url):
    m = re.search(r"(?:blob:)?https?://([^/\"']+)", url or "")
    return (m.group(1).lower() if m else "")


def expected_hosts(project_url):
    """Hosts a Flow download may come from: the project host and Flow/Google content hosts."""
    hosts = {"google.com", "googleusercontent.com"}
    h = _url_host(project_url)
    if h:
        hosts.add(h)
    return hosts


def begin_matches(url, frame_id, page_frames, allowed_hosts):
    """True only when a begin event provably comes from the driven project page.

    frame_id must be a frame of that page and the URL host an expected Flow/Google content
    host. A missing frame_id is never proof; foreign frames/tabs are excluded.
    """
    if not frame_id or not page_frames or frame_id not in page_frames:
        return False
    host = _url_host(url)
    return bool(host) and any(host == h or host.endswith("." + h) for h in allowed_hosts)


class DownloadUnresolved(RuntimeError):
    """The click's download cannot be resolved (no begin in time, not completed in time,
    or canceled). At most one download may be in flight: this stops the whole batch before
    another download click is ever issued."""

    def __init__(self, reason):
        super().__init__("download unresolved: %s" % reason)
        self.reason = reason


class DownloadTracker:
    """Binds each saved file to its click under the one-download-in-flight rule.

    Browser.setDownloadBehavior (allowAndName) names each download file by its guid and emits
    Browser.downloadWillBegin / Browser.downloadProgress. Only a begin that provably comes
    from the driven project page (begin_matches: page frameId + expected Flow/Google host) and
    arrives after the click counts as that click's download. If it does not begin within the
    begin timeout, does not complete within the download timeout, or is canceled, fetch raises
    DownloadUnresolved and the batch stops there. No other file is ever adopted or deleted;
    foreign frames/tabs are never touched and are reported as leftovers by the caller.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self._events = []
        self.foreign = []

    def feed(self, event):
        self._events.append(event)
        if event.get("kind") == "begin" and not event.get("verified"):
            self.foreign.append(event)

    def mark(self):
        """Record the click point: only begins arriving after it can be this click's."""
        return len(self._events)

    def fetch(self, mark, begin_timeout_s, dl_timeout_s, pump=time.sleep, step=0.25):
        """Bytes of this click's download; raises DownloadUnresolved when it cannot be resolved."""
        guid = None
        deadline = time.time() + begin_timeout_s
        while guid is None and time.time() < deadline:
            for e in self._events[mark:]:
                if e.get("kind") == "begin" and e.get("verified"):
                    guid = e.get("guid")
                    break
            if guid is None:
                pump(step)
        if guid is None:
            raise DownloadUnresolved("no_begin")
        deadline = time.time() + dl_timeout_s
        while time.time() < deadline:
            state = self._state_of(guid)
            if state == "completed":
                return self._take(guid)
            if state == "canceled":
                raise DownloadUnresolved("canceled")
            pump(step)
        raise DownloadUnresolved("timeout")

    def _state_of(self, guid):
        state = None
        for e in self._events:
            if e.get("kind") == "progress" and e.get("guid") == guid:
                state = e.get("state")
        return state

    def _take(self, guid):
        """Read and remove only the guid-named file of this download (allowAndName)."""
        p = self.directory / guid
        if not p.is_file():
            matches = sorted(q for q in self.directory.glob(guid + "*") if q.is_file())
            p = matches[0] if matches else p
        if not p.is_file():
            return None
        raw = p.read_bytes()
        if not raw.startswith(b"\xff\xd8"):
            return None  # not the JPEG the menu provides: leave the file where it is
        p.unlink()
        return raw


def _reexec_with_venv():
    """Playwright lives in the project .venv; re-exec there when the current interpreter lacks it."""
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    if venv_py.exists() and os.path.abspath(sys.executable) != os.path.abspath(venv_py):
        os.execv(str(venv_py), [str(venv_py)] + sys.argv)
    raise SystemExit("playwright가 없다. 설치: .venv/bin/pip install playwright==1.65.2")


class FlowBrowser:
    """One persistent CDP connection to the dedicated Chrome; UI steps per the 2026-11-13
    proven flow (settings radios, tile menu, submit, download menu)."""

    def __init__(self, cdp_url=CDP_URL):
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None
        self._page = None
        self._settings = None
        self._dl_cdp = None
        self._tracker = None
        self._staging_win = None
        self._staging_wsl = None
        self._proof = (set(), set())
        self._frame_cdp = None
        self.staging_leftovers = []
        self.staging_dir = ""
        self.closed_dialogs = []

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _reexec_with_venv()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise TransportError("cdp connect failed: %s" % e)
        self._minimize_windows()
        # per-run staging folder: concurrent runs never share download files
        run_tag = "run-%d-%d" % (os.getpid(), int(time.time()))
        self._staging_win = STAGING_ROOT_WIN + "\\" + run_tag
        self._staging_wsl = STAGING_ROOT_WSL / run_tag
        try:
            self._staging_wsl.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._tracker = DownloadTracker(self._staging_wsl)
        return self

    def __exit__(self, *exc):
        try:
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = None
            try:
                self.staging_leftovers = sorted(p.name for p in self._staging_wsl.iterdir() if p.is_file())
            except OSError:
                self.staging_leftovers = []
            self.staging_dir = str(self._staging_wsl)
            try:
                self._staging_wsl.rmdir()  # only when empty: unknown files are never deleted
            except OSError:
                pass
        return False

    def _minimize_windows(self):
        """Keep the dedicated Chrome off the user's screen. Minimizing over CDP does not change
        the foreground window (measured 2026-11-16), and rendering continues because of the
        anti-throttling launch flags. Best effort: a failure here never blocks the run."""
        try:
            cdp = self._browser.new_browser_cdp_session()
            done = set()
            for t in cdp.send("Target.getTargets")["targetInfos"]:
                if t.get("type") != "page":
                    continue
                try:
                    w = cdp.send("Browser.getWindowForTarget", {"targetId": t["targetId"]})
                except Exception:
                    continue  # browser UI pages (e.g. sign-in dialogs) have no window
                if w["windowId"] in done:
                    continue
                done.add(w["windowId"])
                if w["bounds"].get("windowState") != "minimized":
                    cdp.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {"windowState": "minimized"}})
        except Exception:
            pass

    # ------------------------------------------------------------- probe (read-only)
    def probe(self, project_url):
        """Readiness of the project page (prompt box), or of the home page (new-project button)
        when project_url is HOME_URL, i.e. no project has been created yet."""
        out = {"status": None, "error": None, "attached": None}
        try:
            page = self._ensure_page(project_url)
            if "flow.google.com" in page.url:
                try:  # let the app render so a promo dialog is in place before it is closed
                    page.wait_for_load_state("load", timeout=15000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
                self._close_dialogs(page)
            if project_url == HOME_URL:
                box = page.get_by_role("button", name=NEW_PROJECT_NAME)
            else:
                box = page.get_by_role("textbox", name=BOX_NAME)
            if box.count() == 0 and "flow.google.com" in page.url:
                try:  # a freshly opened tab is still loading the Flow app
                    box.first.wait_for(state="attached", timeout=30000)
                except Exception:
                    pass
            has_prompt = box.count() > 0
            has_session = self._has_google_session()
            status = classify_signin(page.url, has_prompt, has_session)
            out["attached"] = {"url": page.url, "has_prompt": has_prompt, "session": has_session,
                               "login": status == "login_required"}
            out["status"] = status
        except TransportError as e:
            out.update(status="connect_failed", error=str(e)[:300])
        except Exception as e:
            out.update(status="connect_failed", error=str(e)[:300])
        return out

    def _has_google_session(self):
        """Positive sign-in evidence: Google session cookie *names* in the browser context.
        Cookie values are never read into output, logs or errors."""
        try:
            ctx = self._browser.contexts[0] if self._browser and self._browser.contexts else None
            cookies = ctx.cookies() if ctx else []
        except Exception:
            return False
        return any(c.get("name") in GOOGLE_SESSION_COOKIES and str(c.get("domain", "")).endswith("google.com")
                   for c in cookies)

    def _ensure_page(self, project_url):
        """The project tab (reused when open), else a new background tab. Never foregrounded.

        A signed-out redirect to accounts.google.com is returned as-is so the probe reports
        login_required instead of hiding it behind a transport error.
        """
        if self._browser is None:
            raise TransportError("not connected")
        ctx = self._browser.contexts[0] if self._browser.contexts else None
        if ctx is None:
            raise TransportError("no browser context")
        pages = [p for p in ctx.pages if not p.url.startswith("chrome://")]
        if project_url == HOME_URL:
            # every Flow URL starts with the home URL: only the home page itself counts
            matches = lambda u: u.rstrip("/") == HOME_URL.rstrip("/")
        else:
            matches = lambda u: u.startswith(project_url)
        if project_url:
            exact = [p for p in pages if matches(p.url)]
            if exact:
                self._page = exact[0]
                return self._page
            before = {id(p) for p in ctx.pages}
            cdp = self._browser.new_browser_cdp_session()
            target = {"url": project_url, "background": True}
            if not pages:
                # no window yet: open a minimized one so nothing appears or takes focus
                target.update(newWindow=True, windowState="minimized")
            cdp.send("Target.createTarget", target)
            deadline = time.time() + 15
            grace = 0
            while time.time() < deadline:
                created = [p for p in ctx.pages if id(p) not in before]
                exact = [p for p in created if matches(p.url)]
                if exact:
                    self._page = exact[0]
                    return self._page
                accounts = [p for p in created if "accounts.google.com" in p.url]
                if accounts:
                    grace += 1
                    if grace >= 4:  # the redirect settled on the sign-in wall
                        self._page = accounts[0]
                        return self._page
                if created:
                    created[0].wait_for_timeout(500)
                else:
                    # the sync API only registers new pages while a Playwright call runs;
                    # time.sleep here would never see the tab that was just created
                    try:
                        ctx.wait_for_event("page", timeout=500)
                    except Exception:
                        pass
            raise TransportError("project tab did not open: %s" % project_url)
        flows = [p for p in pages if "flow.google.com" in p.url]
        if flows:
            self._page = flows[0]
            return self._page
        raise TransportError("no flow.google.com tab open")

    # ------------------------------------------------------------- project creation
    def create_project(self, title=None):
        """Create an empty Flow project from the home page in the background tab.

        Returns {"url", "title", "title_set"}. The title (the work folder name) is best effort:
        an unrenamed project still works, only its Flow list label stays the default date.
        Raises TransportError when the home page is not usable (signed out, UI changed).
        """
        page = self._ensure_page(HOME_URL)
        if "accounts.google.com" in page.url or not self._has_google_session():
            raise TransportError("login_required: Flow home is not signed in")
        self._dismiss_cookie(page)
        btn = page.get_by_role("button", name=NEW_PROJECT_NAME)
        try:
            btn.first.wait_for(state="visible", timeout=30000)
        except Exception:
            raise TransportError("new project button not found on %s" % page.url)
        btn.first.click(timeout=10000)
        deadline = time.time() + 30
        while not PROJECT_URL_RE.match(page.url) and time.time() < deadline:
            page.wait_for_timeout(500)
        m = PROJECT_URL_RE.match(page.url)
        if not m:
            raise TransportError("new project did not open (url %s)" % page.url)
        url = m.group(0)
        # a fresh load renders the prompt box the same way a reopened project does
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._page = page
        self._settings = None
        out = {"url": url, "title": title, "title_set": False}
        if title:
            out["title_set"] = self._rename_project(page, title)
        return out

    def _rename_project(self, page, title):
        try:
            self._dismiss_cookie(page)
            inp = page.get_by_role("textbox", name=TITLE_INPUT_NAME)
            if not inp.count():
                inp = page.locator('input[aria-label="%s"]' % TITLE_INPUT_NAME)
            inp.first.wait_for(state="visible", timeout=20000)
            inp.first.fill(title, timeout=10000)
            inp.first.press("Enter", timeout=5000)
            page.wait_for_timeout(1500)
            return inp.first.input_value(timeout=5000) == title
        except Exception:
            return False

    # ------------------------------------------------------------- generation
    def generate(self, project_url, item, quality_label, on_submit, gen_timeout_s, dl_timeout_s):
        """Run one prompt item end to end; returns (result, images, fatal).

        `on_submit` runs after the durable "submitted" write point and before the click, so a
        later pre-click failure keeps earlier submission history (`submission_unresolved`).
        """
        try:
            return self._generate(project_url, item, quality_label, on_submit, gen_timeout_s, dl_timeout_s)
        except TransportError:
            raise
        except Exception as e:
            if CLOSED_RE.search(str(e)):
                raise TransportError(str(e)[:300])
            return {"id": item["id"], "status": "failed", "reason": "error: " + str(e)[:200]}, [], None

    def _generate(self, project_url, item, quality_label, on_submit, gen_timeout_s, dl_timeout_s):
        page = self._ensure_page(project_url)
        self._dismiss_cookie(page)
        self._close_menus(page)
        fatal = self._ensure_settings(page, item)
        if fatal:
            return None, [], fatal
        t0 = time.time()
        self._clear_prompt(page)
        if item.get("ref_title"):
            mark = "ref-" + str(item["id"])
            found = self._mark(page, "title", item["ref_title"], mark)
            if not found or not self._open_tile_menu(page, mark):
                self._close_menus(page)
                return {"id": item["id"], "status": "failed", "reason": "ref_not_found"}, [], None
            page.get_by_role("menuitem", name=ADD_TO_PROMPT_MENU).click(timeout=10000)
            page.wait_for_timeout(1200)
        before = set(self._tile_srcs(page))
        box = page.get_by_role("textbox", name=BOX_NAME)
        box.click(timeout=10000)
        page.keyboard.insert_text(item["prompt"])
        page.wait_for_timeout(700)
        btn = page.get_by_role("button", name=SUBMIT_NAME)
        if btn.is_disabled():
            return {"id": item["id"], "status": "failed", "reason": "submit_disabled"}, [], None
        # durable "submitted" write happens here: a kill below may have submitted
        on_submit()
        btn.click(timeout=10000)
        deadline = t0 + gen_timeout_s
        fresh = []
        while time.time() < deadline:
            fresh = [s for s in self._tile_srcs(page) if s not in before]
            if len(fresh) >= item["count"]:
                break
            page.wait_for_timeout(2500)
        fresh = fresh[: item["count"]]
        got, gen_s = len(fresh), round(time.time() - t0, 1)
        if got == 0:
            problems = self._problem_lines(page)
            fatal = {"fatal": "problem_text", "problems": problems} if problems else None
            return {"id": item["id"], "status": "failed", "reason": "no_output", "gen_s": gen_s,
                    "problems": problems}, [], fatal
        page.wait_for_timeout(3000)
        titles = [self._mark(page, "src", s, "%s-%d" % (item["id"], k + 1)) or "" for k, s in enumerate(fresh)]
        # re-mark first tile and hover it so its prompt shows for the verified check
        self._mark(page, "src", fresh[0], "%s-1" % item["id"])
        page.locator('[data-fr="%s-1"] img' % item["id"]).hover(timeout=10000)
        page.wait_for_timeout(800)
        verified = _norm(item["prompt"][:40]) in _norm(self._body_text(page))
        images = []
        fatal = None
        for k in range(got):
            raw, unresolved = None, None
            try:
                mk = "%s-%d" % (item["id"], k + 1)
                # re-mark right before use: Flow re-renders tiles on hover and drops the marker
                if self._mark(page, "src", fresh[k], mk) is not None and self._open_tile_menu(page, mk):
                    page.get_by_role("menuitem", name=DOWNLOAD_MENU).hover(timeout=10000)
                    page.wait_for_timeout(900)
                    raw = self._download_via_staging(page, quality_label, dl_timeout_s, project_url)
            except DownloadUnresolved as e:
                unresolved = e
            except Exception:
                raw = None
            finally:
                self._close_menus(page)
            if raw:
                images.append((k + 1, raw))
                page.wait_for_timeout(800)
            if unresolved is not None:
                # at most one download in flight: the batch stops here, before any further click
                fatal = {"fatal": "download_unresolved", "reason": unresolved.reason, "ids": [item["id"]],
                         "hint": "이 항목(%s)을 Flow에서 결과가 있는지 먼저 확인한다. 확인 전까지 다른 다운로드 클릭은 하지 않으며, "
                                 "이 항목은 --force 없이는 재제출하지 않는다." % item["id"]}
                break
        saved = len(images)
        status = "done" if saved == item["count"] else ("partial" if saved else "failed")
        reason = "" if saved else "download_failed"
        if fatal:
            reason = "download_unresolved: " + fatal["reason"]
        return {"id": item["id"], "status": status, "reason": reason,
                "gen_s": gen_s, "got": got, "saved": saved, "verified": verified, "titles": titles}, images, fatal

    # ------------------------------------------------------------- UI steps
    def _dismiss_cookie(self, page):
        later = page.get_by_role("button", name=LATER_NAME, exact=True)
        try:
            if later.count() and later.first.is_visible():
                later.first.click(timeout=5000)
                page.wait_for_timeout(800)
        except Exception:
            pass
        self._close_dialogs(page)

    def _close_dialogs(self, page):
        """Close blocking Flow promo/notice dialogs by their X button; returns what was closed."""
        try:
            closed = page.evaluate(CLOSE_DIALOGS_JS)
            if closed:
                self.closed_dialogs.extend(closed)
                page.wait_for_timeout(800)
            return closed
        except Exception:
            return []

    def _clear_prompt(self, page):
        try:
            clear = page.locator('[aria-label="%s"]' % CLEAR_NAME)
            if clear.count() and clear.first.is_visible():
                clear.first.click(timeout=5000)
                page.wait_for_timeout(600)
        except Exception:
            pass

    def _ensure_settings(self, page, item):
        want = "%s|%s" % (item["ratio"], item["count"])
        if want == self._settings:
            return None
        info = self._apply_settings(page, item["ratio"], item["count"])
        ok = (MODEL_LABEL in info["trigger"] and "x%s" % item["count"] in info["trigger"]
              and any(c.endswith(item["ratio"]) for c in info["checked"]))
        if not ok:
            return {"fatal": "settings_mismatch", "settings": info, "want": want}
        self._settings = want
        return None

    def _apply_settings(self, page, ratio, count):
        page.get_by_role("button", name=SETTINGS_NAME).click(timeout=10000)
        page.wait_for_timeout(1000)
        page.evaluate(RADIO_CLICK_JS, [ratio, count])
        page.wait_for_timeout(800)
        checked = page.evaluate(CHECKED_JS)
        trigger = page.get_by_role("button", name=SETTINGS_NAME).inner_text()
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        return {"checked": checked, "trigger": trigger}

    def _tile_srcs(self, page):
        return page.evaluate(TILE_SRCS_JS, TILE)

    def _mark(self, page, key, val, mark):
        """Tag the tile's group with data-fr so it can be located; returns its title or None."""
        return page.evaluate(MARK_JS, [key, val, mark, TILE])

    def _open_tile_menu(self, page, mark):
        try:
            page.locator('[data-fr="%s"] img' % mark).hover(timeout=10000)
            page.wait_for_timeout(500)
            page.locator('[data-fr="%s"] button[aria-label="%s"]' % (mark, MORE_OPTIONS)).click(timeout=10000)
            page.wait_for_timeout(900)
            return True
        except Exception:
            return False

    def _close_menus(self, page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            # a menu that ignored Escape leaves a transparent backdrop over every button
            if page.evaluate(CLOSE_BACKDROPS_JS):
                page.wait_for_timeout(600)
        except Exception:
            pass

    def _download_via_staging(self, page, quality_label, dl_timeout_s, project_url):
        """Click the download menu item and return the bytes of exactly that download.

        A file is adopted only when its CDP begin event provably belongs to this click
        (project-page frameId + expected Flow/Google host, arriving after the click). A
        download that cannot be resolved raises DownloadUnresolved so the batch stops before
        another download click; a foreign or missing file is never adopted or deleted.
        """
        self._set_download_behavior()
        self._proof = (self._page_frames(page), expected_hosts(project_url))
        mark = self._tracker.mark()
        page.get_by_role("menuitem", name=quality_label).click(timeout=10000)
        return self._tracker.fetch(mark, 15, dl_timeout_s,
                                   pump=lambda s: page.wait_for_timeout(int(s * 1000)))

    def _on_download_begin(self, event):
        frames, hosts = self._proof
        self._tracker.feed({"kind": "begin", "guid": event.get("guid"), "url": event.get("url"),
                            "frameId": event.get("frameId"), "suggested": event.get("suggestedFilename"),
                            "verified": begin_matches(event.get("url"), event.get("frameId"), frames, hosts)})

    def _page_frames(self, page):
        """Frame ids of the driven page (main frame + children), for begin-event proof."""
        try:
            if self._frame_cdp is None or self._frame_cdp[0] is not page:
                self._frame_cdp = (page, page.context.new_cdp_session(page))
            tree = self._frame_cdp[1].send("Page.getFrameTree").get("frameTree") or {}
            frames = set()

            def walk(node):
                frame = node.get("frame") or {}
                if frame.get("id"):
                    frames.add(frame["id"])
                for child in node.get("childFrames") or []:
                    walk(child)

            walk(tree)
            return frames
        except Exception:
            return set()

    def _set_download_behavior(self):
        try:
            if self._dl_cdp is None:
                self._dl_cdp = self._browser.new_browser_cdp_session()
                self._dl_cdp.on("Browser.downloadWillBegin", self._on_download_begin)
                self._dl_cdp.on("Browser.downloadProgress",
                                lambda e: self._tracker.feed({"kind": "progress", "guid": e.get("guid"),
                                                              "state": e.get("state")}))
            self._dl_cdp.send("Browser.setDownloadBehavior",
                              {"behavior": "allowAndName", "downloadPath": self._staging_win,
                               "eventsEnabled": True})
        except Exception as e:
            raise TransportError("setDownloadBehavior failed: %s" % e)

    def _body_text(self, page):
        return page.evaluate(BODY_JS) or ""

    def _problem_lines(self, page):
        return [l for l in self._body_text(page).splitlines()
                if not BADGE_RE.search(l) and PROBLEM_RE.search(l)][:12]


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    cmd = argv[0] if argv else "check"
    if cmd == "start":
        r = start_chrome()
        print(json.dumps({"start": r, "cdp": CDP_URL,
                          "version": (cdp_version() or {}).get("Browser")}, ensure_ascii=False))
        if r in ("started", "already_running"):
            return 0
        print("시작하지 못했다(%s). 같은 프로필로 Chrome이 이미 실행 중이면 닫고 다시 시작한다:" % r)
        print(" " + chrome_cmdline())
        return 1
    if cmd == "check":
        v = cdp_version()
        print(json.dumps({"cdp": CDP_URL, "reachable": bool(v),
                          "version": (v or {}).get("Browser")}, ensure_ascii=False))
        return 0 if v else 1
    print("usage: flow_browser.py start|check")
    return 2


if __name__ == "__main__":
    sys.exit(main())
