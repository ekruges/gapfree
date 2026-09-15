#!/usr/bin/env python3
"""gapfree: free, self-hosted contribution graph filler for GitHub.

One file, standard library only. Plans a realistic mix of commits, pull
requests, issues and reviews (the same seeded hash gapless.sh uses for the
commit squares), backfills past dates, and keeps going every day while it
runs. This should not be a paid service.

    python3 gapfree.py setup                        log in with the GitHub CLI and pick the repo
    python3 gapfree.py serve                        web UI + scheduler on http://localhost:7331
    python3 gapfree.py tick                         one scheduler pass (for cron)
    python3 gapfree.py balance 2026                 create the PRs, issues and reviews the mix implies for a year, now
    python3 gapfree.py backfill 2024-01-01 2024-12-31 [--before-creation]
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

__version__ = "0.4.0"

HOME = os.environ.get("GAPFREE_HOME") or os.path.expanduser("~/.gapfree")
CONFIG = os.path.join(HOME, "config.json")
REPO = os.path.join(HOME, "repo")
LOGFILE = os.path.join(HOME, "gapfree.log")
PORT = int(os.environ.get("GAPFREE_PORT", "7331"))
SERVICE = os.environ.get("GAPFREE_SERVICE", "")
API = "https://api.github.com"

DEFAULTS = {
    "token": "",                 # blank: use `gh auth token`
    "repo": "",                  # owner/name; created private when missing
    "tz": "",                    # IANA zone; blank: detected from the machine
    "hours": [9, 16],            # commits land between these local hours
    "density": 50,               # percent of days that get commits
    "range": [1, 6],             # commits per active day, uniform
    "weekends": True,
    "mix": {"commits": 70, "prs": 12, "issues": 10, "reviews": 8},  # shares of all contributions
    "mix_auto": False,           # draw a fresh balanced shape from each year's seed instead
    "messages": ["Small update", "Add daily entry", "Record progress", "Housekeeping",
                 "Update notes", "Tidy up log", "Update activity log"],
    "seeds": {},                 # year -> uint32, "Randomize" replaces it
    "overrides": {},             # date -> commit count, 0 keeps the day empty
    "progress": {},              # date -> issue and PR numbers created that day
    "forward": False,            # keep committing every day
    "forward_since": "",         # first day the scheduler is responsible for
    "topup": False,              # backfill may add commits to days gapfree itself filled earlier
    "balance_rate": 300,         # objects an hour while balancing a year in bulk
}
PR_TITLES = ["Add {date} entries", "Log updates for {date}", "Daily entries, {date}", "Update log for {date}"]
ISSUE_TITLES = ["Entries for {date}", "Track {date} notes", "Log housekeeping, {date}", "Notes for {date}"]
REVIEWS = ["Looks good.", "LGTM.", "Read through, nothing to change.", "Fine by me.", "Checked the entries, all good."]
CFG = {}
BUSY = ""
STOP = False
LAST_TICK = ""
_cfg_lock = threading.RLock()
_work = threading.Lock()
_me = {}
_cal = {}


# ---------------------------------------------------------------- config

def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    if not cfg["tz"]:
        cfg["tz"] = detect_tz()
    cfg["mix"] = clean_mix(cfg["mix"])
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


def clean_mix(m):
    """Whole percentages summing to 100; reviews never exceed PRs (GitHub counts one review per PR)."""
    m = {k: max(0, int(m.get(k, 0))) for k in ("commits", "prs", "issues", "reviews")}
    m["reviews"] = min(m["reviews"], m["prs"])
    m["commits"] = max(10, m["commits"])
    tot = sum(m.values())
    m = {k: round(v * 100 / tot) for k, v in m.items()}
    m["commits"] += 100 - sum(m.values())
    return m


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
        if cfg is CFG:  # only the live config is persisted, never a scratch copy
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


def sround(x, u):
    """Round x to a whole number, rounding up with probability equal to its fraction."""
    return int(x) + (u < x - int(x))


def day_plan(cfg, date):
    """Everything planned for one date, or None: commit minutes, which commit slices travel through a
    PR (and which of those get a review), and when issues open. Counts follow the mix per commit."""
    c = planned(cfg, date)
    if c <= 0:
        return None
    s = seed(cfg, date[:4])
    mix = mix_for(cfg, date[:4])
    per = 1 / max(1, mix["commits"])
    ts = minutes(cfg, date, c)
    p = min(c, sround(c * mix["prs"] * per, fnv(date, s, "np")))
    r = min(c, sround(c * mix["reviews"] * per, fnv(date, s, "nr")))
    p = max(p, r)  # a review needs its own PR
    i = sround(c * mix["issues"] * per, fnv(date, s, "ni"))
    free = list(range(c))
    starts = sorted(free.pop(int(fnv(date, s, f"ps{k}") * len(free))) for k in range(p))
    reviewed = set(sorted(range(p), key=lambda k: fnv(date, s, f"ro{k}"))[:r])
    prs = []
    for k, st in enumerate(starts):
        limit = (starts[k + 1] if k + 1 < p else c) - st
        prs.append({"start": st, "end": st + 1 + int(fnv(date, s, f"pl{k}") * min(3, limit)), "review": k in reviewed})
    a, b = cfg["hours"][0] * 60, cfg["hours"][1] * 60 - 1
    issues = sorted(a + int(fnv(date, s, f"im{k}") * (b - a)) for k in range(i))
    return {"commits": ts, "prs": prs, "issues": issues}


def mix_for(cfg, year):
    """The sliders' mix, or a balanced semi-random shape drawn from the year's seed when auto mix is on."""
    if not cfg.get("mix_auto"):
        return cfg["mix"]
    s = seed(cfg, year)

    def u(tag, a, b):
        return a + fnv(str(year), s, tag) * (b - a)
    m = {"commits": u("mc", 45, 75), "prs": u("mp", 10, 25), "issues": u("mi", 5, 15)}
    m["reviews"] = m["prs"] * u("mr", 0.3, 0.9)  # reviews as a share of the PRs, like someone who reviews some of what they open
    return clean_mix(m)


def counts(plan):
    return {"commits": len(plan["commits"]), "prs": len(plan["prs"]), "issues": len(plan["issues"]),
            "reviews": sum(g["review"] for g in plan["prs"])}


def year_days(year):
    d = dt.date(year, 1, 1)
    while d.year == year:
        yield d.isoformat()
        d += dt.timedelta(1)


def hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


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


def gql(cfg, query, variables):
    r = gh(cfg, "/graphql", {"query": query, "variables": variables})
    if r.get("errors"):
        raise RuntimeError(r["errors"][0]["message"])
    return r["data"]


def repo_counts(cfg, year):
    """This account's PRs, issues and reviewed PRs in the activity repo for a year, cached 10 minutes."""
    hit = _cal.get(("counts", year))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    login = me(cfg)["login"]
    base = f"repo:{cfg['repo']} created:{year}-01-01..{year}-12-31 "

    def count(extra):
        return gql(cfg, "query($q:String!){search(type:ISSUE,query:$q){issueCount}}", {"q": base + extra})["search"]["issueCount"]
    out = {"prs": count(f"is:pr author:{login}"), "issues": count(f"is:issue author:{login}"),
           "reviews": count(f"is:pr reviewed-by:{login}")}
    _cal[("counts", year)] = (time.time(), out)
    return out


def me(cfg):
    """Login, avatar and the noreply address GitHub credits commits to."""
    if not _me:
        u = gh(cfg, "/user")
        _me.update(login=u["login"], avatar=u.get("avatar_url", ""), created=u.get("created_at", "")[:10],
                   email=f'{u["id"]}+{u["login"]}@users.noreply.github.com')
    return _me


def calendar(cfg, year):
    """GitHub's own per-day counts and per-type totals for a year, cached 10 minutes."""
    hit = _cal.get(year)
    if hit and time.time() - hit[0] < 600:
        return hit[1], hit[2]
    q = ("query($f:DateTime!,$t:DateTime!){viewer{contributionsCollection(from:$f,to:$t){"
         "totalCommitContributions totalPullRequestContributions totalIssueContributions "
         "totalPullRequestReviewContributions restrictedContributionsCount "
         "contributionCalendar{weeks{contributionDays{date contributionCount}}}}}}")
    r = gh(cfg, "/graphql", {"query": q, "variables": {"f": f"{year}-01-01T00:00:00Z",
                                                        "t": f"{year}-12-31T23:59:59Z"}})
    if r.get("errors"):
        raise RuntimeError(r["errors"][0]["message"])
    cc = r["data"]["viewer"]["contributionsCollection"]
    days = {d["date"]: d["contributionCount"] for w in cc["contributionCalendar"]["weeks"]
            for d in w["contributionDays"]}
    totals = {"commits": cc["totalCommitContributions"], "prs": cc["totalPullRequestContributions"],
              "issues": cc["totalIssueContributions"], "reviews": cc["totalPullRequestReviewContributions"],
              "private": cc["restrictedContributionsCount"]}
    _cal[year] = (time.time(), days, totals)
    return days, totals


def list_repos(cfg):
    out = []
    for page in (1, 2, 3):
        rs = gh(cfg, f"/user/repos?per_page=100&sort=pushed&affiliation=owner&page={page}")
        out += [{"name": r["full_name"], "private": r["private"]} for r in rs]
        if len(rs) < 100:
            break
    return out


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


def repo_ready():
    return os.path.isdir(os.path.join(REPO, ".git"))


def ensure_repo(cfg):
    """Local clone at REPO on origin/main; creates the private repo on GitHub when missing."""
    if "/" not in cfg["repo"]:
        raise RuntimeError("Pick a repository first.")
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
    if repo_ready() and git("remote", "get-url", "origin", check=False).strip().rsplit("@", 1)[-1] != f"github.com/{cfg['repo']}.git":
        subprocess.run(["rm", "-rf", REPO])  # repo changed in settings, start a fresh clone
    if not repo_ready():
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


def ours(cfg):
    """date -> commits by this account already in the local clone, bucketed by each commit's own zone.
    Other people's commits in a shared repo must not count as the day's progress."""
    if not repo_ready():
        return {}
    try:
        mine = me(cfg)["email"]
    except Exception:
        mine = None
    out = {}
    for line in git("log", "--format=%ae %aI", "main", check=False).splitlines():
        email, _, when = line.partition(" ")
        if mine is None or email == mine:
            out[when[:10]] = out.get(when[:10], 0) + 1
    return out


def commit(cfg, date, minute, i):
    path = os.path.join(REPO, "log", date[:4], date[5:] + ".md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new:
            f.write(f"# {date}\n\n")
        f.write(f"- {hhmm(minute)} {cfg['tz']}\n")
    when = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(minute // 60, minute % 60),
                               zone(cfg)).isoformat()
    msgs = cfg["messages"] or DEFAULTS["messages"]
    msg = msgs[int(fnv(date, seed(cfg, date[:4]), f"m{i}") * len(msgs))]
    git("add", "-A")
    git("commit", "-q", "-m", msg, env=ident(cfg, when))


def push_main():
    git("push", "-q", "origin", "main")


def open_pr(cfg, title, branch, body):
    try:
        return gh(cfg, f"/repos/{cfg['repo']}/pulls", {"title": title, "head": branch, "base": "main", "body": body})
    except GHError as e:
        if e.code != 422:
            raise
        owner = cfg["repo"].split("/")[0]
        prs = gh(cfg, f"/repos/{cfg['repo']}/pulls?state=open&head={owner}:{branch}")
        if not prs:
            raise
        return prs[0]


def merge_pr(cfg, date, g, prog):
    """Push the group's commits as a branch, open a PR (reviewed when planned), rebase-merge it.
    Rebase keeps the author dates, so the commits still count on the day they are stamped with."""
    repo, s = cfg["repo"], seed(cfg, date[:4])
    branch = f"log/{date}-{g['start']}"
    git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
    issue = next((n for n in prog["issues"].values() if n not in prog.setdefault("closing", [])), None)
    title = PR_TITLES[int(fnv(date, s, f"pt{g['start']}") * len(PR_TITLES))].format(date=date)
    pr = open_pr(cfg, title, branch, (f"Closes #{issue}\n\n" if issue else "") + "Daily log entries.")
    log(f"{date}: opened PR #{pr['number']}")
    if g["review"]:
        body = REVIEWS[int(fnv(date, s, f"rb{g['start']}") * len(REVIEWS))]
        try:
            gh(cfg, f"/repos/{repo}/pulls/{pr['number']}/reviews", {"event": "COMMENT", "body": body})
            prog["reviews"] = prog.get("reviews", 0) + 1
            log(f"{date}: reviewed PR #{pr['number']}")
        except GHError as e:
            log(f"{date}: review skipped: {e}")
    for attempt in range(5):
        try:
            gh(cfg, f"/repos/{repo}/pulls/{pr['number']}/merge", {"merge_method": "rebase"}, "PUT")
            break
        except GHError:
            if attempt == 4:
                raise
            time.sleep(3)
    try:
        gh(cfg, f"/repos/{repo}/git/refs/heads/{branch}", method="DELETE")
    except GHError:
        pass
    git("fetch", "-q", "origin", "main")
    git("reset", "-q", "--hard", "origin/main")
    prog["prs"][str(g["start"])] = pr["number"]
    if issue:
        prog["closing"].append(issue)
    save(cfg)
    log(f"{date}: merged PR #{pr['number']} with {g['end'] - g['start']} commits")


def group_at(plan, i):
    return next((g for g in plan["prs"] if g["start"] <= i < g["end"]), None)


def sync_past(cfg, date, mine):
    """A past date lands all its missing commits at once. No PRs or issues: GitHub dates those at creation."""
    plan = day_plan(cfg, date)
    want = len(plan["commits"]) if plan else 0
    done = mine.get(date, 0)
    for i in range(done, want):
        commit(cfg, date, plan["commits"][i], i)
    if want > done:
        mine[date] = want
        log(f"{date}: {want - done} commits")
    return max(0, want - done)


def due_today(cfg, date, done, now):
    """True when something in today's plan is ready to happen."""
    plan = day_plan(cfg, date)
    if not plan:
        return False
    m = now.hour * 60 + now.minute
    prog = cfg["progress"].get(date, {})
    ts = plan["commits"]
    if done < len(ts):
        g = group_at(plan, done)
        if (ts[g["end"] - 1] if g else ts[done]) <= m:
            return True
    if any(mi <= m for k, mi in enumerate(plan["issues"]) if str(k) not in prog.get("issues", {})):
        return True
    return bool(plan["issues"]) and done >= len(ts) and len(prog.get("issues", {})) == len(plan["issues"]) \
        and not prog.get("closed")


def sync_today(cfg, date, mine, now):
    """Run today's plan up to the current minute: open due issues, land due commits (direct, or through
    a PR when the slice's last commit is due), and close the issues once the day is complete."""
    plan = day_plan(cfg, date)
    if not plan:
        return 0
    ts, repo, s = plan["commits"], cfg["repo"], seed(cfg, date[:4])
    m = now.hour * 60 + now.minute
    prog = cfg["progress"].setdefault(date, {"issues": {}, "prs": {}, "reviews": 0})
    for k, mi in enumerate(plan["issues"]):
        if mi <= m and str(k) not in prog["issues"]:
            title = ISSUE_TITLES[int(fnv(date, s, f"it{k}") * len(ISSUE_TITLES))].format(date=date)
            num = gh(cfg, f"/repos/{repo}/issues", {"title": title, "body": "Tracking today's log entries."})["number"]
            prog["issues"][str(k)] = num
            save(cfg)
            log(f"{date}: opened issue #{num}")
    i, n, dirty = mine.get(date, 0), 0, False
    while i < len(ts):
        g = group_at(plan, i)
        if g:
            if ts[g["end"] - 1] > m:
                break
            if dirty:
                push_main()
                dirty = False
            for j in range(i, g["end"]):
                commit(cfg, date, ts[j], j)
            merge_pr(cfg, date, g, prog)
            n += g["end"] - i
            i = g["end"]
        else:
            if ts[i] > m:
                break
            commit(cfg, date, ts[i], i)
            dirty = True
            n += 1
            i += 1
    if dirty:
        push_main()
    mine[date] = i
    if n:
        log(f"{date}: {n} commits")
    if i >= len(ts) and plan["issues"] and len(prog["issues"]) == len(plan["issues"]) and not prog.get("closed"):
        for num in prog["issues"].values():
            try:
                gh(cfg, f"/repos/{repo}/issues/{num}", {"state": "closed"}, "PATCH")
            except GHError as e:
                log(f"{date}: closing #{num} failed: {e}")
        prog["closed"] = True
        save(cfg)
        log(f"{date}: closed {len(prog['issues'])} issues")
    return n


def tick(cfg):
    """One scheduler pass: catch up every day since forward mode started, then run today."""
    global LAST_TICK
    if not cfg["forward"]:
        return 0
    now = dt.datetime.now(zone(cfg))
    LAST_TICK = now.strftime("%H:%M")
    today = now.date()
    start = dt.date.fromisoformat(cfg["forward_since"] or today.isoformat())
    days = [(start + dt.timedelta(k)).isoformat() for k in range((today - start).days + 1)]
    mine = ours(cfg)

    def behind(d):
        plan = day_plan(cfg, d)
        return plan and mine.get(d, 0) < len(plan["commits"])
    if not any(behind(d) for d in days[:-1]) and not due_today(cfg, days[-1], mine.get(days[-1], 0), now):
        return 0
    with _work:
        ensure_repo(cfg)
        mine = ours(cfg)
        n = sum(sync_past(cfg, d, mine) for d in days[:-1])
        if n:
            push_main()
        n += sync_today(cfg, days[-1], mine, now)
        _cal.clear()
        return n


def backfill(cfg, start, end, before_creation=False):
    """Push planned commits for every past day in a range that has no activity yet (yours or the repo's).
    Never reaches back before the account existed unless explicitly told to."""
    created = me(cfg)["created"]
    if start < created and not before_creation:
        raise RuntimeError(f"{start} is before your account was created ({created}). Confirm to fill anyway.")
    with _work:
        ensure_repo(cfg)
        mine = ours(cfg)
        today = dt.datetime.now(zone(cfg)).date().isoformat()
        cals, n = {}, 0
        d = dt.date.fromisoformat(start)
        while d.isoformat() <= end and d.isoformat() < today:
            ds = d.isoformat()
            if d.year not in cals:
                cals[d.year] = calendar(cfg, d.year)[0]
            taken = cals[d.year].get(ds, 0) - (mine.get(ds, 0) if cfg["topup"] else 0) > 0
            if not (taken and ds not in cfg["overrides"]):  # any activity that day: skip
                n += sync_past(cfg, ds, mine)
            d += dt.timedelta(1)
        if n:
            push_main()
        _cal.clear()
        log(f"backfill {start} to {end}: {n} commits pushed")
        return n


def balance_needed(cfg, year):
    """What the year holds now, what the mix implies for that many commits, and the gap."""
    cal, totals = calendar(cfg, year)
    mine = ours(cfg)
    rc = repo_counts(cfg, year)
    have = {"commits": totals["commits"] + sum(v for d, v in mine.items() if d.startswith(str(year))),
            "prs": totals["prs"] + rc["prs"], "issues": totals["issues"] + rc["issues"],
            "reviews": totals["reviews"] + rc["reviews"]}
    mix = mix_for(cfg, year)
    want = {k: round(have["commits"] * mix[k] / max(1, mix["commits"])) for k in ("prs", "issues", "reviews")}
    want["reviews"] = min(want["reviews"], want["prs"])
    return {"have": have, "want": want, "need": {k: max(0, want[k] - have[k]) for k in want}}


def balance_run(cfg, year):
    """Create the missing PRs, issues and reviews for a year, paced. GitHub dates them today, there is
    no other way. PR commits are authored by nobody so the commit count stays where the mix expects it."""
    global STOP
    STOP = False
    with _work:
        ensure_repo(cfg)
        need = balance_needed(cfg, year)["need"]
        log(f"balance {year}: +{need['prs']} PRs, +{need['issues']} issues, +{need['reviews']} reviews, "
            f"about {sum(need.values()) / max(60, cfg['balance_rate']):.1f} h at {cfg['balance_rate']} an hour")
        repo, s = cfg["repo"], seed(cfg, year)
        nobody = dict(os.environ, GIT_AUTHOR_NAME="gapfree", GIT_AUTHOR_EMAIL="gapfree@users.noreply.github.com",
                      GIT_COMMITTER_NAME="gapfree", GIT_COMMITTER_EMAIL="gapfree@users.noreply.github.com")
        done, k = {"prs": 0, "issues": 0, "reviews": 0}, 0
        while not STOP and any(done[x] < need[x] for x in need):
            try:
                if done["issues"] < need["issues"]:
                    title = ISSUE_TITLES[int(fnv(str(k), s, "bt") * len(ISSUE_TITLES))].format(date=f"entry {k + 1}")
                    n = gh(cfg, f"/repos/{repo}/issues", {"title": title, "body": "Tracking a log entry."})["number"]
                    gh(cfg, f"/repos/{repo}/issues/{n}", {"state": "closed"}, "PATCH")
                    done["issues"] += 1
                if done["prs"] < need["prs"]:
                    git("fetch", "-q", "origin", "main")
                    git("reset", "-q", "--hard", "origin/main")
                    path = os.path.join(REPO, "log", "notes.md")
                    with open(path, "a") as f:
                        f.write(f"- note {k + 1}\n")
                    git("add", "-A")
                    git("commit", "-q", "-m", "Update notes", env=nobody)
                    branch = f"notes/{k + 1}-{int(time.time())}"
                    git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
                    title = PR_TITLES[int(fnv(str(k), s, "bp") * len(PR_TITLES))].format(date=f"note {k + 1}")
                    pr = open_pr(cfg, title, branch, "Notes.")
                    if done["reviews"] < need["reviews"]:
                        gh(cfg, f"/repos/{repo}/pulls/{pr['number']}/reviews",
                           {"event": "COMMENT", "body": REVIEWS[int(fnv(str(k), s, "br") * len(REVIEWS))]})
                        done["reviews"] += 1
                    for attempt in range(5):
                        try:
                            gh(cfg, f"/repos/{repo}/pulls/{pr['number']}/merge", {"merge_method": "rebase"}, "PUT")
                            break
                        except GHError as e:
                            if attempt == 4 or e.code in (403, 429):
                                raise
                            time.sleep(3)
                    try:
                        gh(cfg, f"/repos/{repo}/git/refs/heads/{branch}", method="DELETE")
                    except GHError:
                        pass
                    done["prs"] += 1
                k += 1
                if k % 25 == 0:
                    log(f"balance {year}: {done['prs']}/{need['prs']} PRs, {done['issues']}/{need['issues']} issues, "
                        f"{done['reviews']}/{need['reviews']} reviews")
                time.sleep(3600 / max(60, cfg["balance_rate"]))
            except GHError as e:
                if e.code in (403, 429):
                    log(f"GitHub is rate limiting, balance pauses 15 min: {e}")
                    time.sleep(900)
                else:
                    log(f"balance: {e}")
                    time.sleep(5)
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        _cal.clear()
        log(f"balance {year} {'stopped' if STOP else 'done'}: {done['prs']} PRs, {done['issues']} issues, {done['reviews']} reviews")


# ---------------------------------------------------------------- setup

def gh_users():
    """Accounts the GitHub CLI is logged into on this machine."""
    r = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"], capture_output=True, text=True)
    return re.findall(r"account (\S+)", r.stdout + r.stderr)


def gh_login():
    """Interactive CLI login; the device code works on a headless box. Returns the login it added."""
    before = set(gh_users())
    subprocess.run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web",
                    "--scopes", "repo"], check=True)
    new = [u for u in gh_users() if u not in before]
    return new[0] if new else (gh_users() or [""])[0]


def gh_token(user):
    r = subprocess.run(["gh", "auth", "token", "--hostname", "github.com", "--user", user], capture_output=True, text=True)
    return r.stdout.strip()


def setup(cfg):
    """Walk through accounts and repo in the terminal, then hand the result to the running service."""
    print(f"gapfree {__version__} setup\n")
    if not shutil.which("gh"):
        print("The GitHub CLI is missing. Install it from https://cli.github.com and run this again.")
        return
    users = gh_users()
    main = users[0] if users else ""
    if not main or input(f"Main account: keep @{main}? [Y/n] ").strip().lower() in ("n", "no"):
        print("Log in to the account whose graph gets filled.")
        main = gh_login()
    cfg["token"] = gh_token(main)
    print(f"Main account: @{main}\n")
    if not cfg["repo"]:
        cfg["repo"] = norm_repo(cfg, input("\nRepo for the log [activity-log]: ").strip() or "activity-log")
    save(cfg)
    _me.clear()
    ensure_repo(cfg)
    print(f"\nRepo: {cfg['repo']} ({sum(ours(cfg).values())} commits)")
    for cmd in (["systemctl", "restart", "gapfree"], ["systemctl", "--user", "restart", "gapfree"],
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/sh.gapfree"]):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            print("Service restarted.")
            break
    print(f"Done. Open http://localhost:{PORT} and press Backfill or switch on the daily run.")


# ---------------------------------------------------------------- web

def state(cfg, year):
    now = dt.datetime.now(zone(cfg))
    today = now.date().isoformat()
    mine = ours(cfg)
    err = login = avatar = created = ""
    cal, totals, bal = {}, {}, {}
    try:
        u = me(cfg)
        login, avatar, created = u["login"], u["avatar"], u["created"]
        cal, totals = calendar(cfg, year)
        bal = balance_needed(cfg, year)
    except Exception as e:
        err = str(e)
    days = {}
    mix = {"commits": 0, "prs": 0, "issues": 0, "reviews": 0}
    for d in year_days(year):
        o = mine.get(d, 0)
        r = max(0, cal.get(d, 0) - o)
        plan = day_plan(cfg, d)
        want = len(plan["commits"]) if plan else 0
        taken = r > 0 or (o > 0 and not cfg["topup"])
        p = 0 if (d < today and taken and d not in cfg["overrides"]) else max(0, want - o)
        live = plan and p and d >= today
        prog = cfg["progress"].get(d, {})
        days[d] = [r, o, p, len(plan["prs"]) if live else 0, len(plan["issues"]) if live else 0]
        mix["commits"] += o + p
        if live:
            for k, v in counts(plan).items():
                if k != "commits":
                    mix[k] += v
        mix["prs"] += len(prog.get("prs", {}))
        mix["issues"] += len(prog.get("issues", {}))
        mix["reviews"] += prog.get("reviews", 0)
    for k in mix:
        mix[k] += totals.get(k, 0)
    upcoming, d = [], now.date()
    while len(upcoming) < 8 and (d - now.date()).days < 120:
        plan = day_plan(cfg, d.isoformat())
        if plan:
            upcoming.append(dict(counts(plan), date=d.isoformat(), first=hhmm(plan["commits"][0]),
                                 last=hhmm(plan["commits"][-1])))
        d += dt.timedelta(1)
    public = {k: v for k, v in cfg.items() if k not in ("token", "progress")}
    public["token_set"] = bool(cfg["token"])
    return {"year": year, "today": today, "days": days, "login": login, "avatar": avatar, "created": created, "error": err,
            "version": __version__, "balance": bal,
            "busy": BUSY, "service": SERVICE, "last_tick": LAST_TICK,
            "repo_ready": repo_ready() and bool(cfg["repo"]), "repo_commits": sum(mine.values()),
            "total": sum(v for k, v in cal.items() if k.startswith(str(year))),
            "planned_days": sum(1 for v in days.values() if v[2]),
            "planned_commits": sum(v[2] for v in days.values()),
            "mix": mix, "mix_now": mix_for(cfg, year), "public": totals, "upcoming": upcoming, "settings": public}


def range_info(cfg, start, end):
    """Planned commits inside a past date range, for the backfill button label."""
    mine = ours(cfg)
    today = dt.datetime.now(zone(cfg)).date().isoformat()
    cals, days, commits = {}, 0, 0
    d = dt.date.fromisoformat(start)
    while d.isoformat() <= end and d.isoformat() < today:
        ds = d.isoformat()
        if d.year not in cals:
            try:
                cals[d.year] = calendar(cfg, d.year)[0]
            except Exception:
                cals[d.year] = {}
        plan = day_plan(cfg, ds)
        want = len(plan["commits"]) if plan else 0
        taken = cals[d.year].get(ds, 0) - (mine.get(ds, 0) if cfg["topup"] else 0) > 0
        if want > mine.get(ds, 0) and not (taken and ds not in cfg["overrides"]):
            days += 1
            commits += want - mine.get(ds, 0)
        d += dt.timedelta(1)
    return {"days": days, "commits": commits}


def tail_log(n=80):
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


def norm_repo(cfg, r):
    r = r.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
    if r and "/" not in r:
        r = me(cfg)["login"] + "/" + r
    return r


def set_settings(body):
    with _cfg_lock:
        for k in ("repo", "tz", "hours", "density", "range", "weekends", "mix", "mix_auto", "messages", "topup", "balance_rate"):
            if k in body:
                CFG[k] = body[k]
        if body.get("token"):
            CFG["token"] = body["token"].strip()
            _me.clear()
        CFG["repo"] = norm_repo(CFG, CFG["repo"])
        ZoneInfo(CFG["tz"])
        lo, hi = sorted(int(x) for x in CFG["range"])
        CFG["range"] = [max(1, lo), max(1, hi)]
        h0, h1 = int(CFG["hours"][0]), int(CFG["hours"][1])
        CFG["hours"] = [max(0, min(23, h0)), max(h0 + 1, min(24, h1))]
        CFG["density"] = max(0, min(100, int(CFG["density"])))
        CFG["mix"] = clean_mix(CFG["mix"])
        CFG["mix_auto"] = bool(CFG["mix_auto"])
        _cal.clear()
        save(CFG)


def publish(body):
    with _cfg_lock:
        CFG["repo"] = norm_repo(CFG, body.get("repo", ""))
        save(CFG)
    with _work:
        ensure_repo(CFG)
    _cal.clear()
    return {"repo": CFG["repo"], "commits": sum(ours(CFG).values())}


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
        log("daily run " + ("on" if CFG["forward"] else "off"))


def randomize(body):
    with _cfg_lock:
        CFG["seeds"][str(body["year"])] = int.from_bytes(os.urandom(4), "big")
        save(CFG)


ACTIONS = {
    "/api/settings": set_settings,
    "/api/publish": publish,
    "/api/override": set_override,
    "/api/forward": set_forward,
    "/api/randomize": randomize,
    "/api/refresh": lambda b: _cal.clear(),
    "/api/backfill": lambda b: (backfill(CFG, b["from"], b["to"], b.get("before_creation")) if b.get("check") else
                                run_bg(f"backfill {b['from']} to {b['to']}", lambda: backfill(CFG, b["from"], b["to"], b.get("before_creation")))),
    "/api/tick": lambda b: run_bg("tick", lambda: tick(CFG)),
    "/api/balance": lambda b: run_bg(f"balance {b['year']}", lambda: balance_run(CFG, int(b["year"]))),
    "/api/stop": lambda b: globals().__setitem__("STOP", True),
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
            if path == "/api/range":
                return self.send(200, range_info(CFG, q["from"], q["to"]))
            if path == "/api/repos":
                return self.send(200, list_repos(CFG))
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
    log(f"gapfree on http://{bind}:{PORT}" + (f" ({SERVICE} service)" if SERVICE else ""))
    srv.serve_forever()


HTML = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>gapfree</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--mut:#8b949e;--bd:#30363d;--c0:#1c2128;--c1:#0e4429;--c2:#006d32;--c3:#26a641;--c4:#39d353;--acc:#238636;--blue:#58a6ff;--err:#f85149;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1040px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
header img{width:44px;height:44px;border-radius:50%;border:1px solid var(--bd);background:var(--card)}
header h1{font-size:20px;margin:0;line-height:1.2}header h1 small{color:var(--mut);font-weight:normal;font-size:12px;margin-left:6px}
header .sub{color:var(--mut);font-size:13px}
.pills{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.pill{border:1px solid var(--bd);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--mut)}
.pill.ok{border-color:var(--acc);color:var(--c4)}.pill.warn{border-color:var(--warn);color:var(--warn)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:14px 16px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 4px}
.card>p.mut,.card .lead{margin:0 0 10px}
.head{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
.stats{margin-left:auto;display:flex;gap:16px;color:var(--mut);font-size:13px}.stats b{color:var(--fg)}
.row{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;margin:8px 0}
label{display:inline-flex;gap:6px;align-items:center;color:var(--mut);font-size:12px;white-space:nowrap}
input[type=range]{width:110px;margin:0;accent-color:var(--c3)}
input[type=number]{width:56px}
input,select,textarea,button{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px;font:inherit;padding:5px 9px}
button{cursor:pointer;background:#21262d}button:hover{border-color:#8b949e}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button.pri:hover{background:#2ea043}
button:disabled{opacity:.5;cursor:default}
.yr{display:inline-flex;align-items:center;gap:6px}.yr b{min-width:40px;text-align:center}
.graph{overflow-x:auto;padding:8px 0 4px}
svg.grid{display:block;font-size:10px;fill:var(--mut)}
.grid rect{rx:2;cursor:pointer}
.grid rect.pre{opacity:.35}
.graph{user-select:none;touch-action:none}
.grid rect{cursor:crosshair}
#selg rect{fill:rgba(88,166,255,.38);stroke:var(--blue);stroke-width:1;pointer-events:none;rx:2}
#dragtip{position:fixed;background:#1f2937;border:1px solid var(--blue);border-radius:6px;padding:4px 8px;font-size:12px;pointer-events:none;display:none;z-index:9}
#setup.inner{border-bottom:1px solid var(--bd);padding-bottom:10px;margin-bottom:12px}
.ours{stroke:#e6edf3;stroke-width:.7}.plan{stroke:var(--c4);stroke-width:.8;stroke-dasharray:1.5 1}
.today{stroke:#fff;stroke-width:1.3}
.legend{display:flex;flex-wrap:wrap;gap:14px;color:var(--mut);font-size:12px;align-items:center}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
.cols{display:grid;grid-template-columns:1fr 300px;gap:16px}
@media(max-width:700px){.cols{grid-template-columns:1fr}}
.mix label.sl{display:grid;grid-template-columns:96px 1fr 44px;gap:10px;align-items:center;margin:6px 0;white-space:nowrap}
.mix label.sl input[type=range]{width:100%}.mix b{color:var(--fg);text-align:right;font-variant-numeric:tabular-nums}
svg.radar text{font-size:11px;fill:var(--mut)}
.tabs{display:flex;gap:0;margin:6px 0 10px;border-bottom:1px solid var(--bd)}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;padding:6px 12px;color:var(--mut)}
.tabs button.on{color:var(--fg);border-bottom-color:#f78166}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:10px;max-height:220px;overflow:auto;font-size:12px;margin:8px 0 0}
pre.sample{max-height:none;color:var(--mut)}
table{border-collapse:collapse;font-size:13px;margin-top:6px}td,th{padding:4px 12px 4px 0;text-align:left;color:var(--mut)}th{font-weight:500;font-size:12px}td.n{text-align:right;font-variant-numeric:tabular-nums;color:var(--fg)}
.err{color:var(--err)}.mut{color:var(--mut)}.ok{color:var(--c4)}
details.card summary{cursor:pointer;color:var(--mut);font-size:15px}
.hint{font-size:12px;color:var(--mut)}code{background:var(--bg);border:1px solid var(--bd);border-radius:4px;padding:1px 5px;font-size:12px}
</style>
<main>
<header>
 <img id="avatar" alt="">
 <div><h1>gapfree <small id="ver"></small></h1><div class="sub" id="who">connecting</div></div>
 <div class="pills"><span class="pill" id="pill-repo">no repo</span><span class="pill" id="pill-run">daily run off</span></div>
</header>

<section class="card" id="setup">
 <h2>Repository</h2>
 <p class="mut">Everything lands in one private repo you own. Publish a fresh sample or pick one you already have.</p>
 <div class="tabs"><button data-tab="new" class="on">New private repo</button><button data-tab="existing">Existing repo</button></div>
 <div id="tab-new">
  <div class="row"><label>Name <input id="newname" value="activity-log" size="22"></label><span class="mut">becomes <b id="newfull"></b>, private</span></div>
  <pre class="sample" id="samplepre"></pre>
  <div class="row"><button class="pri" id="publish">Publish</button><span id="setupmsg"></span></div>
 </div>
 <div id="tab-existing" hidden>
  <div class="row"><select id="repos"><option>loading</option></select><button class="pri" id="use">Use this repo</button><span id="setupmsg2"></span></div>
  <p class="hint">gapfree only appends to log/ and opens issues and pull requests in it. Nothing else in the repo is touched.</p>
 </div>
</section>

<section class="card" id="graphcard">
 <div class="head"><h2>Contribution graph</h2><div class="stats"><span><b id="total">0</b> contributions in <span id="yearlbl"></span></span><span><b id="plannedc">0</b> planned on <b id="plannedd">0</b> days</span></div></div>
 <div class="row">
  <span class="yr"><button id="prev">&lsaquo;</button><b id="year"></b><button id="next">&rsaquo;</button></span>
  <label>Density <input type="range" id="density" min="0" max="100"> <span id="densityv"></span></label>
  <label>Commits/day <input type="range" id="lo" min="1" max="20"> <input type="range" id="hi" min="1" max="20"> <span id="rangev"></span></label>
  <label><input type="checkbox" id="weekends"> Weekends</label>
  <button id="randomize">Randomize year</button>
 </div>
 <div class="graph"><svg class="grid" id="g"></svg></div>
 <div class="legend"><span>Click a square to pin its count.</span>
  <span><span class="sw" style="background:var(--c3)"></span>your activity</span>
  <span><span class="sw" style="background:var(--c3);outline:1px solid #e6edf3"></span>in the activity repo</span>
  <span><span class="sw" style="border:1px dashed var(--c4)"></span>planned</span>
  <span>Less <span class="sw" style="background:var(--c0)"></span><span class="sw" style="background:var(--c1)"></span><span class="sw" style="background:var(--c2)"></span><span class="sw" style="background:var(--c3)"></span><span class="sw" style="background:var(--c4)"></span>More</span>
 </div>
 <div class="row" id="selrow">
  <span id="seltext" class="mut">Drag across the squares to pick a period to fill, or use a preset.</span><button id="clearsel" hidden>Clear</button>
  <button data-preset="year">This year</button><button data-preset="last">Last year</button><button data-preset="12">Last 12 months</button>
  <button class="pri" id="backfill">Backfill</button>
  <label><input type="checkbox" id="topup"> Top up days gapfree already filled</label>
 </div>
 <p class="hint">Backfill pushes the planned commits for every still-empty past day in the selection, dated to their planned minutes. Days with anything on them already are skipped, unless "top up" is on: then days that only hold gapfree's own commits are raised to the current plan, which is how you make old days keep up with a busy real one. Issues and PRs cannot be backdated, so past days get commits only.</p>
</section>

<section class="card cols">
 <div class="mix">
  <h2>Activity mix</h2>
  <p class="mut lead">Shares of your contributions, the way the activity overview on your profile shows them. Move one and the others give way. A review needs a pull request, so reviews never exceed PRs.</p>
  <label class="sl">Commits <input type="range" id="mix-commits" min="10" max="100"> <b id="mixv-commits"></b></label>
  <label class="sl">Pull requests <input type="range" id="mix-prs" min="0" max="90"> <b id="mixv-prs"></b></label>
  <label class="sl">Issues <input type="range" id="mix-issues" min="0" max="90"> <b id="mixv-issues"></b></label>
  <label class="sl">Code review <input type="range" id="mix-reviews" min="0" max="90"> <b id="mixv-reviews"></b></label>
  <div class="row"><button id="shuffle">Shuffle a balanced shape</button><label><input type="checkbox" id="mixauto"> New shape every year, drawn from that year's seed</label></div>
  <p class="hint" id="mixnote"></p>
 </div>
 <div><svg class="radar" id="radar" viewBox="0 0 300 236" width="300" height="236"></svg><div class="hint" id="radarnote"></div></div>
 <div class="row" style="grid-column:1/-1"><span class="hint" id="balline"></span><button class="pri" id="balance" hidden>Balance this year now</button><button id="stopbal" hidden>Stop</button></div>
</section>

<section class="card">
 <h2>Automation</h2>
 <p class="mut">With the daily run on, gapfree commits today's plan at the planned minutes, opens and closes the issues, merges the pull requests, and starts the next year with a fresh seed. Missed days are caught up on the next pass.</p>
 <div class="row"><label><input type="checkbox" id="forward"> Keep committing every day</label><span id="autostatus" class="mut"></span><button id="tick">Run a pass now</button></div>
 <p class="hint" id="servicehint"></p>
 <table id="upcoming"></table>
</section>

<details class="card" id="settings"><summary>Settings</summary><div id="settings-body">
 <div class="row">
  <label>Token <input id="token" type="password" placeholder="blank = gh auth token" size="26"></label>
  <label>Timezone <input id="tz" size="18"></label>
  <label>Hours <input type="number" id="h0" min="0" max="23"> to <input type="number" id="h1" min="1" max="24"></label>
 </div>
 <div class="row"><label>Commit messages, one per line<br><textarea id="messages" rows="4" cols="34"></textarea></label><button id="save">Save settings</button></div>
</div></details>
<details class="card" open><summary>Log</summary><pre id="log"></pre></details>
</main>
<div id="dragtip"></div>
<script>
const $ = id => document.getElementById(id);
const KEYS = ['commits', 'prs', 'issues', 'reviews'];
let Y = new Date().getFullYear(), S = null, timer = null, mixTimer = null, mix = null;
const api = (p, b) => fetch(p, b ? {method: 'POST', body: JSON.stringify(b)} : {}).then(async r => {
  const j = await r.json(); if (!r.ok) throw new Error(j.error); return j; });
const level = n => n <= 0 ? 0 : n <= 2 ? 1 : n <= 4 ? 2 : n <= 7 ? 3 : 4;
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const editing = () => document.activeElement && document.activeElement.matches('input:not([type=range]):not([type=checkbox]),textarea,select');
const fail = e => { $('who').textContent = e.message; $('who').className = 'sub err'; };

async function load() { S = await api('/api/state?year=' + Y); render(); loadLog(); }
async function loadLog() { $('log').textContent = (await api('/api/log')).log; $('log').scrollTop = 1e9; }

function render() {
  const s = S.settings;
  if (!mix || s.mix_auto || !mixTimer) mix = {...S.mix_now};
  $('mixauto').checked = s.mix_auto;
  $('avatar').src = S.avatar || '';
  $('ver').textContent = 'v' + S.version;
  $('who').textContent = S.error || ('@' + S.login + (s.repo ? ' · ' + s.repo : ''));
  $('who').className = S.error ? 'sub err' : 'sub';
  $('pill-repo').textContent = S.repo_ready ? s.repo + ' · ' + S.repo_commits + ' commits' : (s.repo ? s.repo + ' · not published' : 'no repo yet');
  $('pill-repo').className = 'pill ' + (S.repo_ready ? 'ok' : 'warn');
  $('pill-run').textContent = S.busy ? 'working: ' + S.busy : (s.forward ? 'daily run on' : 'daily run off');
  $('pill-run').className = 'pill ' + (S.busy ? 'warn' : s.forward ? 'ok' : '');
  const setup = $('setup');
  if (S.repo_ready && setup.parentElement !== $('settings-body')) { $('settings-body').prepend(setup); setup.classList.replace('card', 'inner'); }
  if (!S.repo_ready && setup.parentElement === $('settings-body')) { document.querySelector('main').insertBefore(setup, $('graphcard')); setup.classList.replace('inner', 'card'); }
  if (!editing()) { $('newfull').textContent = (S.login || 'you') + '/' + ($('newname').value || 'activity-log'); }
  $('samplepre').textContent = `${$('newfull').textContent}  (private)\n├─ README.md      "A running log of daily activity."\n└─ log/${S.today.slice(0,4)}/${S.today.slice(5)}.md\n     # ${S.today}\n     - 09:23 ${s.tz}\n     - 09:31 ${s.tz}`;
  $('year').textContent = Y; $('yearlbl').textContent = Y;
  $('total').textContent = S.total; $('plannedc').textContent = S.planned_commits; $('plannedd').textContent = S.planned_days;
  $('density').value = s.density; $('densityv').textContent = s.density + '%';
  $('lo').value = s.range[0]; $('hi').value = s.range[1]; $('rangev').textContent = s.range[0] + '–' + s.range[1];
  $('weekends').checked = s.weekends; $('forward').checked = s.forward;
  if (!editing()) { $('tz').value = s.tz; $('h0').value = s.hours[0]; $('h1').value = s.hours[1]; $('messages').value = s.messages.join('\n'); }
  $('tick').disabled = !!S.busy || !S.repo_ready;
  if (sel && selYear !== Y) { sel = null; }
  rangeLabel();
  renderGrid(); renderMix(); renderAuto();
  $('topup').checked = !!s.topup;
}

function renderGrid() {
  const first = new Date(Y, 0, 1).getDay(), cell = 11, step = 14;
  let out = '', months = [];
  Object.entries(S.days).forEach(([d, [r, o, p, prs, iss]], i) => {
    const idx = i + first, col = Math.floor(idx / 7), row = idx % 7;
    if (d.endsWith('-01')) months.push([col, MON[+d.slice(5, 7) - 1]]);
    const have = r + o;
    const cls = (r > 0 ? 'real' : o > 0 ? 'ours' : p > 0 ? 'plan' : '') + (d === S.today ? ' today' : '') + (S.created && d < S.created ? ' pre' : '');
    const fill = have ? `var(--c${level(have)})` : p ? `color-mix(in srgb, var(--c${level(p)}) 40%, var(--c0))` : 'var(--c0)';
    const parts = [];
    if (r) parts.push(r + ' yours'); if (o) parts.push(o + ' in the repo'); if (p) parts.push(p + ' planned');
    const extra = (prs ? `, ${prs} PR${prs > 1 ? 's' : ''}` : '') + (iss ? `, ${iss} issue${iss > 1 ? 's' : ''}` : '');
    out += `<rect x="${30 + col * step}" y="${18 + row * step}" width="${cell}" height="${cell}" fill="${fill}" class="${cls}" data-d="${d}"><title>${d}: ${parts.join(' + ') || 'nothing'}${extra}${S.created && d < S.created ? ' (before your account existed)' : ''}</title></rect>`;
  });
  months.forEach(([c, m]) => out += `<text x="${30 + c * step}" y="10">${m}</text>`);
  ['Mon', 'Wed', 'Fri'].forEach((n, i) => out += `<text x="0" y="${18 + (1 + i * 2) * step + 9}">${n}</text>`);
  const cols = Math.ceil((Object.keys(S.days).length + first) / 7);
  $('g').setAttribute('width', 30 + cols * step); $('g').setAttribute('height', 18 + 7 * step);
  $('g').innerHTML = out + '<g id="selg"></g>';
  paintSel();
}

function renderMix() {
  KEYS.forEach(k => { $('mix-' + k).value = mix[k]; $('mixv-' + k).textContent = mix[k] + '%'; });
  const avg = (S.settings.range[0] + S.settings.range[1]) / 2, per = 1 / Math.max(1, mix.commits);
  const f = k => (avg * mix[k] * per).toFixed(1);
  $('mixnote').textContent = (S.settings.mix_auto ? `Shape for ${Y} drawn from its seed; move a slider to set it by hand, Randomize year for another draw. ` : '') + `An average active day (${avg} commits) brings about ${f('prs')} pull requests, ${f('issues')} issues and ${f('reviews')} reviews.`;
  const tot = KEYS.reduce((a, k) => a + S.mix[k], 0) || 1;
  const yr = Object.fromEntries(KEYS.map(k => [k, Math.round(100 * S.mix[k] / tot)]));
  const cx = 150, cy = 118, R = 82, ang = {commits: -Math.PI / 2, prs: 0, issues: Math.PI / 2, reviews: Math.PI};
  const pt = (k, v) => [cx + Math.cos(ang[k]) * R * v / 100, cy + Math.sin(ang[k]) * R * v / 100];
  const poly = (m, style) => `<polygon points="${KEYS.map(k => pt(k, m[k]).join(',')).join(' ')}" ${style}/>`;
  let svg = '';
  [25, 50, 75, 100].forEach(v => svg += poly({commits: v, prs: v, issues: v, reviews: v}, 'fill="none" stroke="#30363d"'));
  KEYS.forEach(k => { const [x, y] = pt(k, 100); svg += `<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="#30363d"/>`; });
  svg += poly(yr, 'fill="rgba(88,166,255,.18)" stroke="#58a6ff" stroke-width="1.5"');
  svg += poly(mix, 'fill="rgba(57,211,83,.18)" stroke="#39d353" stroke-width="1.5"');
  const lab = {commits: [cx, 18, 'middle'], prs: [cx + R + 8, cy + 4, 'start'], issues: [cx, cy + R + 20, 'middle'], reviews: [cx - R - 8, cy + 4, 'end']};
  const name = {commits: 'Commits', prs: 'PRs', issues: 'Issues', reviews: 'Reviews'};
  KEYS.forEach(k => svg += `<text x="${lab[k][0]}" y="${lab[k][1]}" text-anchor="${lab[k][2]}">${name[k]} ${yr[k]}%</text>`);
  $('radar').innerHTML = svg;
  $('radarnote').innerHTML = `<span style="color:#39d353">green</span> target · <span style="color:#58a6ff">blue</span> this year: ${S.mix.commits} commits, ${S.mix.prs} PRs, ${S.mix.issues} issues, ${S.mix.reviews} reviews (public activity + repo + plan)`;
  const b = S.balance || {}, n = b.need || {}, gap = (n.prs || 0) + (n.issues || 0) + (n.reviews || 0);
  const running = /^balance/.test(S.busy || '');
  $('balline').textContent = !b.have ? '' : running ? `Balancing ${Y}: see the log.` : gap ? `${Y} holds ${b.have.commits} commits, ${b.have.prs} PRs, ${b.have.issues} issues, ${b.have.reviews} reviews. Matching the sliders means +${n.prs} PRs, +${n.issues} issues, +${n.reviews} reviews, all dated today, about ${(gap / S.settings.balance_rate).toFixed(1)} h at ${S.settings.balance_rate} an hour.` : `${Y} already matches the sliders.`;
  $('balance').hidden = !gap || running; $('balance').disabled = !!S.busy; $('stopbal').hidden = !running;
}

function renderAuto() {
  const s = S.settings;
  $('autostatus').textContent = s.forward ? `on since ${s.forward_since}${S.last_tick ? ', last pass ' + S.last_tick : ''}` : 'off';
  $('servicehint').innerHTML = S.service ? `Runs as a ${S.service} service, so it survives reboots.` :
    `This process was started by hand and stops with its terminal. To keep it alive across reboots run <code>sh install.sh</code> from the checkout, or <code>curl -fsSL https://raw.githubusercontent.com/ekruges/gapfree/main/install.sh | sh</code>.`;
  let t = '<tr><th>Upcoming</th><th class="n">Commits</th><th class="n">PRs</th><th class="n">Issues</th><th class="n">Reviews</th><th>Window</th></tr>';
  S.upcoming.forEach(u => t += `<tr><td>${u.date === S.today ? 'today' : u.date}</td><td class="n">${u.commits}</td><td class="n">${u.prs}</td><td class="n">${u.issues}</td><td class="n">${u.reviews}</td><td>${u.first} to ${u.last}</td></tr>`);
  $('upcoming').innerHTML = S.upcoming.length ? t : '<tr><td class="mut">Nothing planned in the next months. Raise the density.</td></tr>';
}

function balance(changed, v) {
  const m = {...mix}; v = Math.max(changed === 'commits' ? 10 : 0, Math.min(100, Math.round(v)));
  m[changed] = v;
  const others = KEYS.filter(k => k !== changed), rest = 100 - v, cur = others.reduce((a, k) => a + m[k], 0);
  others.forEach(k => m[k] = cur ? Math.round(m[k] * rest / cur) : Math.round(rest / others.length));
  if (m.reviews > m.prs) { if (changed === 'reviews') m.prs = m.reviews; else m.reviews = m.prs; }
  const pool = KEYS.filter(k => k !== changed && k !== 'reviews' && !(changed === 'reviews' && k === 'prs'));
  let diff = 100 - KEYS.reduce((a, k) => a + m[k], 0);
  for (const k of pool) { const room = diff < 0 ? -(m[k] - (k === 'commits' ? 10 : 0)) : diff; const step = diff < 0 ? Math.max(room, diff) : diff; m[k] += step; diff -= step; if (!diff) break; }
  if (diff) m[changed] += diff;
  return m;
}
KEYS.forEach(k => $('mix-' + k).oninput = e => {
  mix = balance(k, +e.target.value); renderMix();
  clearTimeout(mixTimer); mixTimer = setTimeout(() => api('/api/settings', {mix, mix_auto: false}).then(() => { mixTimer = null; return load(); }).catch(fail), 200);
});
const rnd = (a, b) => a + Math.random() * (b - a);
$('shuffle').onclick = () => {
  // a shape a real profile could have: commits lead, PRs next, reviews a share of the PRs
  const m = {commits: rnd(45, 75), prs: rnd(10, 25), issues: rnd(5, 15)};
  m.reviews = m.prs * rnd(0.3, 0.9);
  const tot = KEYS.reduce((a, k) => a + m[k], 0);
  KEYS.forEach(k => m[k] = Math.round(m[k] * 100 / tot));
  m.commits += 100 - KEYS.reduce((a, k) => a + m[k], 0);
  mix = m; api('/api/settings', {mix, mix_auto: false}).then(load).catch(fail);
};
$('mixauto').onchange = () => { mix = null; api('/api/settings', {mix_auto: $('mixauto').checked}).then(load).catch(fail); };

function settings() {
  const lo = +$('lo').value, hi = +$('hi').value;
  return {density: +$('density').value, range: [Math.min(lo, hi), Math.max(lo, hi)], weekends: $('weekends').checked,
    tz: $('tz').value, hours: [+$('h0').value, +$('h1').value],
    messages: $('messages').value.split('\n').map(x => x.trim()).filter(Boolean), token: $('token').value};
}
const saveSettings = () => api('/api/settings', settings()).then(() => { $('token').value = ''; load(); }).catch(fail);
['density', 'lo', 'hi'].forEach(id => $(id).oninput = () => {
  $('densityv').textContent = $('density').value + '%';
  $('rangev').textContent = Math.min($('lo').value, $('hi').value) + '–' + Math.max($('lo').value, $('hi').value);
  clearTimeout(timer); timer = setTimeout(saveSettings, 150);
});
$('weekends').onchange = saveSettings;
$('topup').onchange = () => api('/api/settings', {topup: $('topup').checked}).then(load).then(rangeLabel).catch(fail);
$('save').onclick = saveSettings;
$('prev').onclick = () => { Y--; load(); };
$('next').onclick = () => { Y++; load(); };
$('randomize').onclick = () => api('/api/randomize', {year: Y}).then(load).catch(fail);
$('forward').onchange = () => api('/api/forward', {on: $('forward').checked}).then(load).catch(fail);
$('tick').onclick = () => api('/api/tick', {}).then(load).catch(fail);
$('balance').onclick = () => { if (confirm($('balline').textContent + '\nEvery one of them lands on today, and GitHub may slow the run down. Go?')) api('/api/balance', {year: Y}).then(load).catch(fail); };
$('stopbal').onclick = () => api('/api/stop', {}).then(load).catch(fail);
function pin(d) {
  const v = prompt(`Commits gapfree should add on ${d} (0 = keep empty, blank = automatic)`, S.days[d][2] || '');
  if (v === null) return;
  api('/api/override', {date: d, count: v.trim() === '' ? null : +v}).then(load).catch(fail);
}
// drag across squares to pick a period; a plain click pins a day's count
let sel = null, selYear = null, drag = null;
const grid = $('g');
const ndays = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 864e5) + 1;
function tip(e) {
  const t = $('dragtip'), [a, b] = [drag.start, drag.end].sort();
  t.textContent = a === b ? `${a} (keep dragging to select a period)` : `${a} to ${b} · ${ndays(a, b)} days`;
  t.style.display = 'block'; t.style.left = (e.clientX + 14) + 'px'; t.style.top = (e.clientY + 14) + 'px';
}
grid.onpointerdown = e => { const d = e.target.dataset.d; if (!d) return; drag = {start: d, end: d, moved: false}; grid.setPointerCapture(e.pointerId); tip(e); };
grid.onpointermove = e => {
  if (!drag) return;
  const el = document.elementFromPoint(e.clientX, e.clientY), d = el && el.dataset.d;
  if (d && d !== drag.end) { drag.end = d; drag.moved = true; paintSel(); }
  tip(e);
};
grid.onpointerup = e => {
  if (!drag) return;
  grid.releasePointerCapture(e.pointerId);
  $('dragtip').style.display = 'none';
  const d = drag; drag = null;
  if (!d.moved) { paintSel(); return pin(d.start); }
  sel = [d.start, d.end].sort(); selYear = Y; paintSel(); rangeLabel();
};
function paintSel() {
  const [a, b] = drag ? [drag.start, drag.end].sort() : (sel || []);
  let out = '';
  if (a) grid.querySelectorAll('rect[data-d]').forEach(r => {
    if (r.dataset.d >= a && r.dataset.d <= b) out += `<rect x="${r.getAttribute('x')}" y="${r.getAttribute('y')}" width="11" height="11"/>`;
  });
  const g = $('selg'); if (g) g.innerHTML = out;
}
const clearSel = () => { sel = null; drag = null; $('dragtip').style.display = 'none'; paintSel(); rangeLabel(); };
$('clearsel').onclick = clearSel;
document.addEventListener('keydown', e => { if (e.key === 'Escape') clearSel(); });

// setup
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x === b));
  $('tab-new').hidden = b.dataset.tab !== 'new'; $('tab-existing').hidden = b.dataset.tab !== 'existing';
  if (b.dataset.tab === 'existing') api('/api/repos').then(rs => {
    $('repos').innerHTML = rs.map(r => `<option value="${r.name}">${r.name}${r.private ? '' : ' (public)'}</option>`).join('') || '<option>no repos found</option>';
  }).catch(fail);
});
$('newname').oninput = () => { $('newfull').textContent = (S.login || 'you') + '/' + ($('newname').value || 'activity-log'); };
const publish = (repo, msg) => { msg.textContent = 'publishing'; api('/api/publish', {repo}).then(r => { msg.textContent = `ready: ${r.repo}, ${r.commits} commits`; load(); }).catch(e => { msg.textContent = e.message; msg.className = 'err'; }); };
$('publish').onclick = () => publish($('newname').value.trim() || 'activity-log', $('setupmsg'));
$('use').onclick = () => publish($('repos').value, $('setupmsg2'));

// fill a period
function preset(p) {
  const t = new Date(S.today + 'T12:00:00'), y = t.getFullYear(), iso = x => x.toISOString().slice(0, 10);
  let a, b;
  if (p === 'year') { a = new Date(Y, 0, 1, 12); b = Y === y ? new Date(t - 864e5) : new Date(Y, 11, 31, 12); }
  else if (p === 'last') { a = new Date(y - 1, 0, 1, 12); b = new Date(y - 1, 11, 31, 12); }
  else { a = new Date(t); a.setFullYear(y - 1); b = new Date(t - 864e5); }
  sel = [iso(a), iso(b)]; selYear = Y; paintSel(); rangeLabel();
}
function rangeLabel() {
  $('clearsel').hidden = !sel;
  if (!sel) { $('seltext').textContent = 'Drag across the squares to pick a period to fill, or use a preset.'; $('backfill').textContent = 'Backfill'; $('backfill').disabled = true; return; }
  const [a, b] = sel;
  api(`/api/range?from=${a}&to=${b}`).then(r => {
    $('seltext').textContent = `${a} to ${b}: ${r.commits ? `${r.days} empty days, ${r.commits} commits to add` : 'nothing left to fill'}` + (S.created && a < S.created ? ` (starts before your account, created ${S.created})` : '');
    $('backfill').textContent = r.commits ? `Backfill ${r.commits} commits` : 'Backfill';
    $('backfill').disabled = !r.commits || !!S.busy || !S.repo_ready;
  }).catch(fail);
}
document.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => preset(b.dataset.preset));
$('backfill').onclick = () => {
  if (!sel || !confirm(`${$('seltext').textContent}\nPush them into ${S.settings.repo}?`)) return;
  const early = S.created && sel[0] < S.created;
  if (early && !confirm(`Your GitHub account was created on ${S.created}. Commits dated before that are an obvious tell.\nFill the earlier days anyway?`)) return;
  api('/api/backfill', {from: sel[0], to: sel[1], before_creation: !!early}).then(load).catch(fail);
};

load().catch(fail);
setInterval(() => { if (!editing()) load().catch(fail); }, 5000);
</script>
"""

if __name__ == "__main__":
    CFG = load()
    cmd = sys.argv[1:] or ["serve"]
    if cmd[0] == "serve":
        serve()
    elif cmd[0] == "version":
        print(__version__)
    elif cmd[0] == "setup":
        setup(CFG)
    elif cmd[0] == "tick":
        print(tick(CFG), "commits")
    elif cmd[0] == "balance" and len(cmd) > 1:
        balance_run(CFG, int(cmd[1]))
    elif cmd[0] == "backfill" and len(cmd) > 2:
        print(backfill(CFG, cmd[1], cmd[2], "--before-creation" in cmd), "commits")
    else:
        print(__doc__)
