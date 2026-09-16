"""Smallest check that fails if the planner drifts from gapless.sh's arithmetic or the mix breaks."""
import datetime as dt
import os
import tempfile

os.environ["GAPFREE_HOME"] = tempfile.mkdtemp()  # never touch a real ~/.gapfree
import gapfree as g  # noqa: E402

# reference values from the gapless.sh bundle: eF(date, seed, tag)
assert g.fnv("2026-03-04", 12345, "in") == 0.097448
assert g.fnv("2026-03-04", 12345, "ct") == 0.306996
assert g.fnv("2025-12-31", 4294967295, "in") == 0.791879
assert g.fnv("2024-02-29", 0, "gp") == 0.516445

cfg = dict(g.DEFAULTS, seeds={"2025": 7}, density=60, range=[1, 11], hours=[9, 16], overrides={"2025-05-05": 3},
           mix={"commits": 60, "prs": 20, "issues": 12, "reviews": 8})
days = list(g.year_days(2025))
plan = {d: g.planned(cfg, d) for d in days}
assert plan == {d: g.planned(cfg, d) for d in days}, "plan must be deterministic"
assert plan["2025-05-05"] == 3, "override wins"
assert all(0 <= n <= 11 for n in plan.values())
wd = [d for d in days if dt.date.fromisoformat(d).weekday() < 5]
we = [d for d in days if dt.date.fromisoformat(d).weekday() >= 5]
wd_rate = sum(1 for d in wd if plan[d]) / len(wd)
we_rate = sum(1 for d in we if plan[d]) / len(we)
assert 0.5 < wd_rate < 0.7, wd_rate
assert 0.15 < we_rate < 0.35, we_rate
assert sum(1 for d in we if g.planned(dict(cfg, weekends=False), d)) == 0

tot = {"commits": 0, "prs": 0, "issues": 0, "reviews": 0}
for d in days:
    p = g.day_plan(cfg, d)
    assert (p is None) == (plan[d] == 0)
    if not p:
        continue
    ts = p["commits"]
    assert len(ts) == plan[d] and ts == sorted(ts) and 9 * 60 <= ts[0] and ts[-1] <= 16 * 60 - 1, (d, ts)
    gaps = {b - a for a, b in zip(ts, ts[1:])}
    assert len(gaps) <= 1 and all(1 <= x <= 12 for x in gaps), (d, ts)
    prev = 0
    for grp in p["prs"]:
        assert prev <= grp["start"] < grp["end"] <= len(ts) and 1 <= grp["end"] - grp["start"] <= 3, (d, p["prs"])
        prev = grp["end"]
    assert all(9 * 60 <= m <= 16 * 60 - 1 for m in p["issues"])
    for k, v in g.counts(p).items():
        tot[k] += v
assert tot["reviews"] <= tot["prs"]
for k in ("prs", "issues", "reviews"):
    want = cfg["mix"][k] / cfg["mix"]["commits"]
    got = tot[k] / tot["commits"]
    assert abs(got - want) < 0.06, (k, got, want)

m = g.clean_mix({"commits": 5, "prs": 10, "issues": 10, "reviews": 30})
assert sum(m.values()) == 100 and m["reviews"] <= m["prs"] and m["commits"] >= 10, m
print("ok", tot)

auto = dict(cfg, mix_auto=True)
for y in (2025, 2026, 2027):
    m = g.mix_for(auto, y)
    assert sum(m.values()) == 100 and m["reviews"] <= m["prs"] and 40 <= m["commits"] <= 80, (y, m)
    assert m == g.mix_for(auto, y), "auto shape must be stable for a year"
assert g.mix_for(auto, 2025) != g.mix_for(auto, 2026) or g.mix_for(auto, 2026) != g.mix_for(auto, 2027)
assert g.mix_for(cfg, 2025) == cfg["mix"]
print("auto mix ok", [g.mix_for(auto, y) for y in (2025, 2026, 2027)])

big = g.minutes(cfg, "2025-03-03", 40)
assert big == sorted(big) and len(set(big)) == 40 and big[-1] <= 16 * 60 - 1, big
print("busy day ok")
