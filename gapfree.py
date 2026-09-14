#!/usr/bin/env python3
"""gapfree: free, self-hosted contribution graph filler for GitHub.

One file, standard library only. Plans a realistic commit distribution
(the same seeded hash gapless.sh uses), backfills past dates, keeps
committing every day while it runs, and can open issues and pull requests
on top so the whole graph fills in, not only the commit squares.

    python3 gapfree.py serve          web UI + scheduler on http://localhost:7331
    python3 gapfree.py tick           one scheduler pass (for cron)
    python3 gapfree.py backfill 2024  push planned commits for every past day of a year
"""
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HOME = os.environ.get("GAPFREE_HOME") or os.path.expanduser("~/.gapfree")
CONFIG = os.path.join(HOME, "config.json")
REPO = os.path.join(HOME, "repo")
LOGFILE = os.path.join(HOME, "gapfree.log")
PORT = int(os.environ.get("GAPFREE_PORT", "7331"))
API = "https://api.github.com"

DEFAULTS = {
    "token": "",                 # blank: use `gh auth token`
    "repo": "",                  # owner/name; created private when missing
    "tz": "",                    # IANA zone; blank: detected from the machine
    "hours": [9, 16],            # commits land between these local hours
    "density": 50,               # percent of days that get commits
    "range": [1, 6],             # commits per active day, uniform
    "weekends": True,
    "issue_pct": 15,             # percent of active days that also open an issue
    "pr_pct": 20,                # percent of active days whose commits arrive as a merged PR
    "review": True,              # leave a review on each PR
    "messages": ["Small update", "Add daily entry", "Record progress", "Housekeeping",
                 "Update notes", "Tidy up log", "Update activity log"],
    "seeds": {},                 # year -> uint32, "Randomize" replaces it
    "overrides": {},             # date -> count, 0 keeps the day empty
    "issues": {},                # date -> issue number opened that day
    "forward": False,            # keep committing every day
    "forward_since": "",         # first day the scheduler is responsible for
}

CFG = {}
BUSY = ""
_cfg_lock = threading.RLock()
_work = threading.Lock()
_me = {}
_cal = {}


# ---------------------------------------------------------------- config

def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    if not cfg["tz"]:
        cfg["tz"] = detect_tz()
    return cfg


def save(cfg):
    os.makedirs(HOME, 0o700, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def detect_tz():
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return os.environ.get("TZ") or "UTC"


def zone(cfg):
    return ZoneInfo(cfg["tz"])


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs(HOME, 0o700, exist_ok=True)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- the plan

def fnv(date, seed, tag):
    """Seeded FNV-1a of 'date:tag' mapped to [0, 1). Same arithmetic as gapless.sh."""
    r = (0x811C9DC5 ^ seed) & 0xFFFFFFFF
    for c in f"{date}:{tag}".encode():
        r = ((r ^ c) * 0x01000193) & 0xFFFFFFFF
    return (r % 1_000_000) / 1_000_000


def seed(cfg, year):
    s = cfg["seeds"].get(str(year))
    if s is None:
        s = cfg["seeds"][str(year)] = int.from_bytes(os.urandom(4), "big")
        save(cfg)
    return s


def planned(cfg, date):
    """Commits the sliders plan for a date; 0 means leave it empty."""
    if date in cfg["overrides"]:
        return int(cfg["overrides"][date])
    d = dt.date.fromisoformat(date)
    s = seed(cfg, d.year)
    weekend = d.weekday() >= 5
    if weekend and not cfg["weekends"]:
        return 0
    p = cfg["density"] / 100
    if fnv(date, s, "in") >= (0.4 * p if weekend else p):  # weekends run at 40% of the density
        return 0
    lo, hi = cfg["range"]
    return lo + int(fnv(date, s, "ct") * (hi - lo + 1))


def minutes(cfg, date, n):
    """Minute of day for each of n commits: random start in the hours window, one constant 3 to 12 min gap."""
    s = seed(cfg, date[:4])
    a, b = cfg["hours"][0] * 60, cfg["hours"][1] * 60 - 1
    gap = 3 + int(fnv(date, s, "gp") * 10)
    start = a + int(fnv(date, s, "st") * (b - a))
    start = max(a, min(start, b - (n - 1) * gap))
    return [min(start + i * gap, b) for i in range(n)]


def flavor(cfg, date):
    s = seed(cfg, date[:4])
    return {"issue": fnv(date, s, "is") * 100 < cfg["issue_pct"],
            "pr": fnv(date, s, "pr") * 100 < cfg["pr_pct"]}


def year_days(year):
    d = dt.date(year, 1, 1)
    while d.year == year:
        yield d.isoformat()
        d += dt.timedelta(1)


# ---------------------------------------------------------------- github

class GHError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def token(cfg):
    if cfg["token"]:
        return cfg["token"]
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    except OSError:
        r = None
    if r is None or r.returncode or not r.stdout.strip():
        raise RuntimeError("No GitHub token. Paste one in Settings, or run `gh auth login`.")
    return r.stdout.strip()


def gh(cfg, path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": "Bearer " + token(cfg), "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "gapfree"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r) if r.status != 204 else None
    except urllib.error.HTTPError as e:
        raise GHError(e.code, f"GitHub {e.code} on {path}: {e.read().decode()[:300]}") from None


def me(cfg):
    """Login and the noreply address GitHub credits commits to."""
    if not _me:
        u = gh(cfg, "/user")
        _me.update(login=u["login"], email=f'{u["id"]}+{u["login"]}@users.noreply.github.com')
    return _me


def calendar(cfg, year):
    """GitHub's own per-day contribution counts for a year, cached 10 minutes."""
    hit = _cal.get(year)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    q = ("query($f:DateTime!,$t:DateTime!){viewer{contributionsCollection(from:$f,to:$t)"
         "{contributionCalendar{weeks{contributionDays{date contributionCount}}}}}}")
    r = gh(cfg, "/graphql", {"query": q, "variables": {"f": f"{year}-01-01T00:00:00Z",
                                                        "t": f"{year}-12-31T23:59:59Z"}})
    if r.get("errors"):
        raise RuntimeError(r["errors"][0]["message"])
    weeks = r["data"]["viewer"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    days = {d["date"]: d["contributionCount"] for w in weeks for d in w["contributionDays"]}
    _cal[year] = (time.time(), days)
    return days


# ---------------------------------------------------------------- git

def git(*args, env=None, check=True):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip()[-300:]}")
    return r.stdout


def ident(cfg, when=None):
    m = me(cfg)
    env = dict(os.environ, GIT_AUTHOR_NAME=m["login"], GIT_AUTHOR_EMAIL=m["email"],
               GIT_COMMITTER_NAME=m["login"], GIT_COMMITTER_EMAIL=m["email"])
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return env


def ensure_repo(cfg):
    """Local clone at REPO on origin/main; creates the private repo on GitHub when missing."""
    if "/" not in cfg["repo"]:
        raise RuntimeError("Set the repo as owner/name in Settings.")
    name = cfg["repo"].split("/", 1)[1]
    try:
        gh(cfg, f"/repos/{cfg['repo']}")
    except GHError as e:
        if e.code != 404:
            raise
        gh(cfg, "/user/repos", {"name": name, "private": True, "description": "Daily activity log"})
        log(f"created private repo {cfg['repo']}")
    url = f"https://x-access-token:{token(cfg)}@github.com/{cfg['repo']}.git"
    # ponytail: the token sits in .git/config inside a 0700 dir, same as gh's own plain-text store on Linux
    if not os.path.isdir(os.path.join(REPO, ".git")):
        os.makedirs(REPO, 0o700, exist_ok=True)
        git("init", "-q")
        git("symbolic-ref", "HEAD", "refs/heads/main")
        git("remote", "add", "origin", url)
    git("remote", "set-url", "origin", url)
    if git("ls-remote", "--heads", "origin", "main").strip():
        git("fetch", "-q", "origin", "main")
        git("checkout", "-q", "-B", "main", "origin/main")
    elif not git("rev-parse", "-q", "--verify", "HEAD", check=False).strip():
        with open(os.path.join(REPO, "README.md"), "w") as f:
            f.write(f"# {name}\n\nA running log of daily activity.\n")
        git("add", "-A")
        git("commit", "-q", "-m", "Start activity log", env=ident(cfg))
        git("push", "-q", "-u", "origin", "main")


def ours():
    """date -> commits already in the local clone, bucketed by each commit's own zone."""
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return {}
    out = {}
    for line in git("log", "--format=%aI", "main", check=False).split():
        out[line[:10]] = out.get(line[:10], 0) + 1
    return out


def commit(cfg, date, minute, i):
    path = os.path.join(REPO, "log", date[:4], date[5:] + ".md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new:
            f.write(f"# {date}\n\n")
        f.write(f"- {minute // 60:02d}:{minute % 60:02d} {cfg['tz']}\n")
    when = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(minute // 60, minute % 60),
                               zone(cfg)).isoformat()
    msgs = cfg["messages"] or DEFAULTS["messages"]
    msg = msgs[int(fnv(date, seed(cfg, date[:4]), f"m{i}") * len(msgs))]
    git("add", "-A")
    git("commit", "-q", "-m", msg, env=ident(cfg, when))


def push_main():
    git("push", "-q", "origin", "main")


def open_pr(cfg, date, branch, body):
    try:
        return gh(cfg, f"/repos/{cfg['repo']}/pulls",
                  {"title": f"Add {date} entries", "head": branch, "base": "main", "body": body})
    except GHError as e:
        if e.code != 422:
            raise
        owner = cfg["repo"].split("/")[0]
        prs = gh(cfg, f"/repos/{cfg['repo']}/pulls?state=open&head={owner}:{branch}")
        if not prs:
            raise
        return prs[0]


def pending(cfg, date, done, now):
    """True when a commit (or the day's issue) is due right now."""
    want = planned(cfg, date)
    if done >= want:
        return False
    if date < now.date().isoformat():
        return True
    ts, fl, m = minutes(cfg, date, want), flavor(cfg, date), now.hour * 60 + now.minute
    if fl["issue"] and date not in cfg["issues"] and ts[0] <= m:
        return True
    return (ts[-1] if fl["pr"] else ts[done]) <= m


def sync_day(cfg, date, mine, now=None, push=True):
    """Create the commits still missing for a date. Past dates land at once; today's wait for
    their planned minute, and the issue / PR extras happen live so GitHub dates them today."""
    want = planned(cfg, date)
    done = mine.get(date, 0)
    if done >= want:
        return 0
    ts = minutes(cfg, date, want)
    repo = cfg["repo"]
    if now is None or date != now.date().isoformat():
        for i in range(done, want):
            commit(cfg, date, ts[i], i)
        mine[date] = want
        if push:
            push_main()
        log(f"{date}: {want - done} commits")
        return want - done
    m = now.hour * 60 + now.minute
    fl = flavor(cfg, date)
    if fl["issue"] and date not in cfg["issues"] and ts[0] <= m:
        n = gh(cfg, f"/repos/{repo}/issues",
               {"title": f"Entries for {date}", "body": "Tracking today's log entries."})["number"]
        cfg["issues"][date] = n
        save(cfg)
        log(f"{date}: opened issue #{n}")
    issue = cfg["issues"].get(date)
    if fl["pr"]:
        if ts[-1] > m:
            return 0
        push_main()
        for i in range(done, want):
            commit(cfg, date, ts[i], i)
        branch = f"log/{date}"
        git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
        pr = open_pr(cfg, date, branch, (f"Closes #{issue}\n\n" if issue else "") + "Daily log entries.")
        log(f"{date}: opened PR #{pr['number']}")
        if cfg["review"]:
            try:
                gh(cfg, f"/repos/{repo}/pulls/{pr['number']}/reviews", {"event": "COMMENT", "body": "Looks good."})
                log(f"{date}: reviewed PR #{pr['number']}")
            except GHError as e:
                log(f"{date}: review skipped: {e}")
        for attempt in range(5):
            try:
                gh(cfg, f"/repos/{repo}/pulls/{pr['number']}/merge", {"merge_method": "merge"}, "PUT")
                break
            except GHError:
                if attempt == 4:
                    raise
                time.sleep(3)
        gh(cfg, f"/repos/{repo}/git/refs/heads/{branch}", method="DELETE")
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        mine[date] = want
        log(f"{date}: merged PR #{pr['number']} with {want - done} commits")
        return want - done
    due = [i for i in range(done, want) if ts[i] <= m]
    if not due:
        return 0
    for i in due:
        commit(cfg, date, ts[i], i)
    mine[date] = done + len(due)
    push_main()
    if issue and mine[date] == want:
        gh(cfg, f"/repos/{repo}/issues/{issue}", {"state": "closed"}, "PATCH")
        log(f"{date}: closed issue #{issue}")
    log(f"{date}: {len(due)} commits")
    return len(due)


def tick(cfg):
    """One scheduler pass: catch up every day since forward mode started, then today."""
    if not cfg["forward"]:
        return 0
    now = dt.datetime.now(zone(cfg))
    today = now.date()
    start = dt.date.fromisoformat(cfg["forward_since"] or today.isoformat())
    days = [(start + dt.timedelta(i)).isoformat() for i in range((today - start).days + 1)]
    mine = ours()
    if not any(pending(cfg, d, mine.get(d, 0), now) for d in days):
        return 0
    with _work:
        ensure_repo(cfg)
        mine = ours()
        n = sum(sync_day(cfg, d, mine, push=False) for d in days[:-1])
        if n:
            push_main()
        n += sync_day(cfg, days[-1], mine, now)
        _cal.clear()
        return n


def backfill(cfg, year):
    """Push planned commits for every past day of a year that has no real activity yet."""
    with _work:
        ensure_repo(cfg)
        mine = ours()
        today = dt.datetime.now(zone(cfg)).date().isoformat()
        real = calendar(cfg, year)
        n = 0
        for d in year_days(year):
            if d >= today:
                break
            if real.get(d, 0) - mine.get(d, 0) > 0 and d not in cfg["overrides"]:
                continue
            n += sync_day(cfg, d, mine, push=False)
        if n:
            push_main()
        _cal.clear()
        log(f"backfill {year}: {n} commits pushed")
        return n


# ---------------------------------------------------------------- web

def state(cfg, year):
    today = dt.datetime.now(zone(cfg)).date().isoformat()
    mine = ours()
    err = ""
    try:
        cal = calendar(cfg, year)
        login = me(cfg)["login"]
    except Exception as e:
        cal, login, err = {}, "", str(e)
    days, past = {}, [0, 0]
    for d in year_days(year):
        o = mine.get(d, 0)
        r = max(0, cal.get(d, 0) - o)
        want = planned(cfg, d)
        p = 0 if (d < today and r > 0 and d not in cfg["overrides"]) else max(0, want - o)
        fl = flavor(cfg, d) if p and d >= today else {}
        if p and d < today:
            past[0] += 1
            past[1] += p
        days[d] = [r, o, p, bool(fl.get("issue")), bool(fl.get("pr"))]
    public = {k: v for k, v in cfg.items() if k != "token"}
    public["token_set"] = bool(cfg["token"])
    return {"year": year, "today": today, "days": days, "login": login, "error": err, "busy": BUSY,
            "total": sum(v for k, v in cal.items() if k.startswith(str(year))),
            "planned_days": sum(1 for v in days.values() if v[2]),
            "planned_commits": sum(v[2] for v in days.values()),
            "past_days": past[0], "past_commits": past[1], "settings": public}


def tail_log(n=60):
    try:
        with open(LOGFILE) as f:
            return {"log": "".join(f.readlines()[-n:])}
    except FileNotFoundError:
        return {"log": ""}


def run_bg(name, fn):
    global BUSY
    if BUSY:
        raise RuntimeError(f"already running: {BUSY}")

    def go():
        global BUSY
        BUSY = name
        try:
            fn()
        except Exception as e:
            log(f"{name} failed: {e}")
        finally:
            BUSY = ""
    threading.Thread(target=go, daemon=True).start()


def set_settings(body):
    with _cfg_lock:
        for k in ("repo", "tz", "hours", "density", "range", "weekends", "issue_pct", "pr_pct",
                  "review", "messages"):
            if k in body:
                CFG[k] = body[k]
        if body.get("token"):
            CFG["token"] = body["token"].strip()
        CFG["repo"] = CFG["repo"].strip().removeprefix("https://github.com/").strip("/")
        ZoneInfo(CFG["tz"])
        lo, hi = sorted(int(x) for x in CFG["range"])
        CFG["range"] = [max(1, lo), max(1, hi)]
        h0, h1 = int(CFG["hours"][0]), int(CFG["hours"][1])
        CFG["hours"] = [max(0, min(23, h0)), max(h0 + 1, min(24, h1))]
        CFG["density"] = max(0, min(100, int(CFG["density"])))
        CFG["issue_pct"] = max(0, min(100, int(CFG["issue_pct"])))
        CFG["pr_pct"] = max(0, min(100, int(CFG["pr_pct"])))
        _me.clear()
        _cal.clear()
        save(CFG)


def set_override(body):
    with _cfg_lock:
        if body.get("count") is None:
            CFG["overrides"].pop(body["date"], None)
        else:
            CFG["overrides"][body["date"]] = max(0, int(body["count"]))
        save(CFG)


def set_forward(body):
    with _cfg_lock:
        CFG["forward"] = bool(body.get("on"))
        if CFG["forward"] and not CFG["forward_since"]:
            CFG["forward_since"] = dt.datetime.now(zone(CFG)).date().isoformat()
        if not CFG["forward"]:
            CFG["forward_since"] = ""
        save(CFG)
        log("daily commits " + ("on" if CFG["forward"] else "off"))


def randomize(body):
    with _cfg_lock:
        CFG["seeds"][str(body["year"])] = int.from_bytes(os.urandom(4), "big")
        save(CFG)


ACTIONS = {
    "/api/settings": set_settings,
    "/api/override": set_override,
    "/api/forward": set_forward,
    "/api/randomize": randomize,
    "/api/refresh": lambda b: _cal.clear(),
    "/api/backfill": lambda b: run_bg(f"backfill {b['year']}", lambda: backfill(CFG, int(b["year"]))),
    "/api/tick": lambda b: run_bg("tick", lambda: tick(CFG)),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = dict(urllib.parse.parse_qsl(qs))
        try:
            if path == "/":
                return self.send(200, HTML.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self.send(200, state(CFG, int(q.get("year") or dt.date.today().year)))
            if path == "/api/log":
                return self.send(200, tail_log())
        except Exception as e:
            return self.send(500, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        fn = ACTIONS.get(self.path)
        if not fn:
            return self.send(404, {"error": "not found"})
        try:
            self.send(200, fn(body) or {"ok": True})
        except Exception as e:
            self.send(500, {"error": str(e)})


def scheduler():
    while True:
        try:
            if CFG["forward"] and not BUSY:
                tick(CFG)
        except Exception as e:
            log(f"tick failed: {e}")
        time.sleep(60)


def serve():
    threading.Thread(target=scheduler, daemon=True).start()
    bind = os.environ.get("GAPFREE_BIND", "127.0.0.1")
    srv = ThreadingHTTPServer((bind, PORT), Handler)
    log(f"gapfree on http://{bind}:{PORT}")
    srv.serve_forever()


HTML = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>gapfree</title>
<style>
:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--bd:#30363d;--c0:#161b22;--c1:#0e4429;--c2:#006d32;--c3:#26a641;--c4:#39d353;--err:#f85149}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 2px}h1 small{color:var(--mut);font-weight:normal;font-size:13px;margin-left:8px}
.row{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;margin:10px 0}
label{display:inline-flex;gap:6px;align-items:center;color:var(--mut);font-size:12px;white-space:nowrap}
input[type=range]{width:100px;margin:0}
input[type=number]{width:54px}
input,textarea,button{background:var(--c0);color:var(--fg);border:1px solid var(--bd);border-radius:4px;font:inherit;padding:4px 8px}
button{cursor:pointer}button.pri{background:#238636;border-color:#238636}button:disabled{opacity:.5;cursor:default}
.graph{overflow-x:auto;padding:6px 0}
svg{display:block;font-size:10px;fill:var(--mut)}
rect{rx:2;cursor:pointer}
.ours{stroke:#e6edf3;stroke-width:.7}.plan{stroke:var(--c4);stroke-width:.8;stroke-dasharray:1.5 1}
.today{stroke:#fff;stroke-width:1.3}
.legend{display:flex;flex-wrap:wrap;gap:14px;color:var(--mut);font-size:12px;align-items:center}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
details{margin:12px 0;border-top:1px solid var(--bd);padding-top:8px}summary{cursor:pointer;color:var(--mut)}
pre{background:var(--c0);border:1px solid var(--bd);padding:8px;max-height:220px;overflow:auto;font-size:12px;margin:8px 0 0}
.err{color:var(--err)}.mut{color:var(--mut)}
</style>
<main>
<h1>gapfree <small id="who"></small></h1>
<div class="row"><span id="summary" class="mut"></span><span id="busy" class="err"></span></div>
<div class="row">
 <button id="prev">&lsaquo;</button><b id="year"></b><button id="next">&rsaquo;</button>
 <label>Density <input type="range" id="density" min="0" max="100"> <span id="densityv"></span></label>
 <label>Commits/day <input type="range" id="lo" min="1" max="20"> <input type="range" id="hi" min="1" max="20"> <span id="rangev"></span></label>
 <label><input type="checkbox" id="weekends"> Include weekends</label>
</div>
<div class="row">
 <label>Issues <input type="range" id="issue_pct" min="0" max="100"> <span id="issuev"></span> of active days</label>
 <label>Pull requests <input type="range" id="pr_pct" min="0" max="100"> <span id="prv"></span> of active days</label>
 <label><input type="checkbox" id="review"> Review each PR</label>
</div>
<div class="row">
 <button id="randomize">Randomize</button>
 <button id="backfill" class="pri">Backfill past dates</button>
 <label><input type="checkbox" id="forward"> Keep committing every day</label>
 <button id="tick">Run now</button>
 <button id="refresh">Refresh</button>
</div>
<div class="graph"><svg id="g"></svg></div>
<div class="legend"><span>Click a square to set its count.</span>
 <span><span class="sw" style="background:var(--c3)"></span>your activity</span>
 <span><span class="sw" style="background:var(--c3);outline:1px solid #e6edf3"></span>from the activity repo</span>
 <span><span class="sw" style="border:1px dashed var(--c4)"></span>planned</span>
 <span>Less <span class="sw" style="background:var(--c0)"></span><span class="sw" style="background:var(--c1)"></span><span class="sw" style="background:var(--c2)"></span><span class="sw" style="background:var(--c3)"></span><span class="sw" style="background:var(--c4)"></span>More</span>
</div>
<details id="settings"><summary>Settings</summary>
 <div class="row">
  <label>Repo <input id="repo" placeholder="owner/activity-log" size="26"></label>
  <label>Token <input id="token" type="password" placeholder="blank = gh auth token" size="26"></label>
  <label>Timezone <input id="tz" size="18"></label>
  <label>Hours <input type="number" id="h0" min="0" max="23"> to <input type="number" id="h1" min="1" max="24"></label>
 </div>
 <div class="row"><label>Commit messages, one per line<br><textarea id="messages" rows="4" cols="34"></textarea></label>
 <button id="save">Save settings</button></div>
</details>
<details open><summary>Log</summary><pre id="log"></pre></details>
</main>
<script>
const $ = id => document.getElementById(id);
let Y = new Date().getFullYear(), S = null, timer = null;
const api = (p, b) => fetch(p, b ? {method: 'POST', body: JSON.stringify(b)} : {}).then(async r => {
  const j = await r.json(); if (!r.ok) throw new Error(j.error); return j; });
const level = n => n <= 0 ? 0 : n <= 2 ? 1 : n <= 4 ? 2 : n <= 7 ? 3 : 4;
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

async function load() { S = await api('/api/state?year=' + Y); render(); loadLog(); }
async function loadLog() { $('log').textContent = (await api('/api/log')).log; $('log').scrollTop = 1e9; }

function render() {
  const s = S.settings;
  $('year').textContent = Y;
  $('who').textContent = (S.login ? '@' + S.login : '') + (s.repo ? ' · ' + s.repo : ' · set a repo in Settings');
  $('summary').textContent = `${S.total} contributions in ${Y} · ${S.planned_days} planned days, ${S.planned_commits} commits to add`;
  $('busy').textContent = S.busy ? 'working: ' + S.busy : S.error;
  $('density').value = s.density; $('densityv').textContent = s.density + '%';
  $('lo').value = s.range[0]; $('hi').value = s.range[1]; $('rangev').textContent = s.range[0] + '–' + s.range[1];
  $('weekends').checked = s.weekends;
  $('issue_pct').value = s.issue_pct; $('issuev').textContent = s.issue_pct + '%';
  $('pr_pct').value = s.pr_pct; $('prv').textContent = s.pr_pct + '%';
  $('review').checked = s.review; $('forward').checked = s.forward;
  if (!$('settings').open || !document.activeElement.closest('#settings')) {
    $('repo').value = s.repo; $('tz').value = s.tz; $('h0').value = s.hours[0]; $('h1').value = s.hours[1];
    $('messages').value = s.messages.join('\n');
  }
  $('backfill').disabled = !!S.busy || !S.past_commits; $('tick').disabled = !!S.busy;
  $('backfill').textContent = S.past_commits ? `Backfill ${S.past_days} past days (${S.past_commits} commits)` : 'Nothing to backfill';
  const first = new Date(Y, 0, 1).getDay(), cell = 11, step = 14;
  let out = '', months = [];
  Object.entries(S.days).forEach(([d, [r, o, p, is, pr]], i) => {
    const idx = i + first, col = Math.floor(idx / 7), row = idx % 7;
    if (d.endsWith('-01')) months.push([col, MON[+d.slice(5, 7) - 1]]);
    const have = r + o, shown = have || p;
    const cls = (r > 0 ? 'real' : o > 0 ? 'ours' : p > 0 ? 'plan' : '') + (d === S.today ? ' today' : '');
    const fill = have ? `var(--c${level(have)})` : p ? `color-mix(in srgb, var(--c${level(p)}) 40%, var(--c0))` : 'var(--c0)';
    const parts = [];
    if (r) parts.push(r + ' yours'); if (o) parts.push(o + ' in the repo'); if (p) parts.push(p + ' planned');
    const tip = d + ': ' + (parts.join(' + ') || 'nothing') + (is ? ' · issue' : '') + (pr ? ' · PR' : '');
    out += `<rect x="${30 + col * step}" y="${18 + row * step}" width="${cell}" height="${cell}" fill="${fill}" class="${cls}" data-d="${d}"><title>${tip}</title></rect>`;
  });
  months.forEach(([c, m]) => out += `<text x="${30 + c * step}" y="10">${m}</text>`);
  ['Mon', 'Wed', 'Fri'].forEach((n, i) => out += `<text x="0" y="${18 + (1 + i * 2) * step + 9}">${n}</text>`);
  const cols = Math.ceil((Object.keys(S.days).length + first) / 7);
  $('g').setAttribute('width', 30 + cols * step); $('g').setAttribute('height', 18 + 7 * step);
  $('g').innerHTML = out;
}

function settings() {
  const lo = +$('lo').value, hi = +$('hi').value;
  return {density: +$('density').value, range: [Math.min(lo, hi), Math.max(lo, hi)], weekends: $('weekends').checked,
    issue_pct: +$('issue_pct').value, pr_pct: +$('pr_pct').value, review: $('review').checked,
    repo: $('repo').value, tz: $('tz').value, hours: [+$('h0').value, +$('h1').value],
    messages: $('messages').value.split('\n').map(x => x.trim()).filter(Boolean), token: $('token').value};
}
const fail = e => { $('busy').textContent = e.message; };
const saveSettings = () => api('/api/settings', settings()).then(() => { $('token').value = ''; load(); }).catch(fail);
['density', 'lo', 'hi', 'issue_pct', 'pr_pct'].forEach(id => $(id).oninput = () => {
  $('densityv').textContent = $('density').value + '%';
  $('rangev').textContent = Math.min($('lo').value, $('hi').value) + '–' + Math.max($('lo').value, $('hi').value);
  $('issuev').textContent = $('issue_pct').value + '%'; $('prv').textContent = $('pr_pct').value + '%';
  clearTimeout(timer); timer = setTimeout(saveSettings, 150);
});
['weekends', 'review'].forEach(id => $(id).onchange = saveSettings);
$('save').onclick = saveSettings;
$('prev').onclick = () => { Y--; load(); };
$('next').onclick = () => { Y++; load(); };
$('randomize').onclick = () => api('/api/randomize', {year: Y}).then(load).catch(fail);
$('refresh').onclick = () => api('/api/refresh', {}).then(load).catch(fail);
$('forward').onchange = () => api('/api/forward', {on: $('forward').checked}).then(load).catch(fail);
$('tick').onclick = () => api('/api/tick', {}).then(load).catch(fail);
$('backfill').onclick = () => {
  if (!confirm(`Push ${S.past_commits} commits over ${S.past_days} past days of ${Y} to ${S.settings.repo}?`)) return;
  api('/api/backfill', {year: Y}).then(load).catch(fail);
};
$('g').onclick = e => {
  const d = e.target.dataset.d; if (!d) return;
  const v = prompt(`Commits gapfree should add on ${d} (0 = keep empty, blank = automatic)`, S.days[d][2] || '');
  if (v === null) return;
  api('/api/override', {date: d, count: v.trim() === '' ? null : +v}).then(load).catch(fail);
};
load();
setInterval(() => { if (!document.activeElement.closest('#settings')) load(); }, 5000);
</script>
"""

if __name__ == "__main__":
    CFG = load()
    cmd = sys.argv[1:] or ["serve"]
    if cmd[0] == "serve":
        serve()
    elif cmd[0] == "tick":
        print(tick(CFG), "commits")
    elif cmd[0] == "backfill" and len(cmd) > 1:
        print(backfill(CFG, int(cmd[1])), "commits")
    else:
        print(__doc__)
