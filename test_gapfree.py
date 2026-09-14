"""Smallest check that fails if the planner drifts from gapless.sh's arithmetic."""
import datetime as dt
import gapfree as g

# reference values from the gapless.sh bundle: eF(date, seed, tag)
assert g.fnv("2026-03-04", 12345, "in") == 0.097448
assert g.fnv("2026-03-04", 12345, "ct") == 0.306996
assert g.fnv("2025-12-31", 4294967295, "in") == 0.791879
assert g.fnv("2024-02-29", 0, "gp") == 0.516445

cfg = dict(g.DEFAULTS, seeds={"2025": 7}, density=60, range=[1, 11], hours=[9, 16], overrides={"2025-05-05": 3})
days = list(g.year_days(2025))
plan = {d: g.planned(cfg, d) for d in days}
assert plan == {d: g.planned(cfg, d) for d in days}, "plan must be deterministic"
assert plan["2025-05-05"] == 3, "override wins"
assert all(0 <= n <= 11 for n in plan.values())
assert all(n == 0 or n >= 1 for n in plan.values())
wd = [d for d in days if dt.date.fromisoformat(d).weekday() < 5]
we = [d for d in days if dt.date.fromisoformat(d).weekday() >= 5]
wd_rate = sum(1 for d in wd if plan[d]) / len(wd)
we_rate = sum(1 for d in we if plan[d]) / len(we)
assert 0.5 < wd_rate < 0.7, wd_rate
assert 0.15 < we_rate < 0.35, we_rate
assert sum(1 for d in we if g.planned(dict(cfg, weekends=False), d)) == 0

for d in days:
    n = plan[d] or 1
    ts = g.minutes(cfg, d, n)
    assert ts == sorted(ts) and 9 * 60 <= ts[0] and ts[-1] <= 16 * 60 - 1, (d, ts)
    gaps = {b - a for a, b in zip(ts, ts[1:])}
    assert len(gaps) <= 1 and all(3 <= x <= 12 for x in gaps), (d, ts)
print("ok")
