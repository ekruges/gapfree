# gapfree

Free, self-hosted filler for the GitHub contribution graph. One Python file,
no dependencies beyond `python3` and `git`.

It does what gapless.sh sells for $2 a month, using the same planning
arithmetic, and adds the parts that service skips: it keeps the plan running
on your own machine forever, and it can open issues and pull requests and
leave reviews so every contribution type on the profile fills in, not only
the commit squares.

Everything lands in one private repo you own (created for you if missing).
Each commit appends one line to `log/YYYY/MM-DD.md`, so the history stays
inspectable and easy to delete.

## Install

Anything with `python3` (3.9+) and `git`. A Raspberry Pi, a 256 MB LXC, your
laptop.

```sh
curl -fsSL https://raw.githubusercontent.com/ekruges/gapfree/main/install.sh | sh
```

That puts `gapfree.py` in `~/.gapfree`, registers a user service (launchd on
macOS, systemd on Linux, cron as fallback) and opens http://localhost:7331.

Docker instead:

```sh
git clone https://github.com/ekruges/gapfree && cd gapfree && docker compose up -d
```

Or just run it:

```sh
python3 gapfree.py serve
```

## Setup

1. Open http://localhost:7331 and expand Settings.
2. Repo: `owner/name`. It is created as a private repo if it does not exist.
   Using an existing repo is fine; gapfree only appends commits.
3. Token: leave blank if the `gh` CLI is logged in on that machine. Otherwise
   paste a token with `repo` scope (classic) or Contents, Issues and Pull
   requests write access on that one repo (fine-grained).
4. Turn on "Include private contributions" on your GitHub profile
   (Contribution settings on the profile page), or the private repo's activity
   stays hidden.

The UI binds to localhost only. If you want it reachable elsewhere, put
Tailscale or an authenticating proxy in front of it. There is no login.

## What the sliders do

The plan is deterministic: every year has a random seed, and each day is
hashed with that seed (FNV-1a over `date:tag`, the arithmetic gapless.sh
ships in its bundle). Change a slider and the same days move, so the preview
is exactly what gets pushed.

- Density: share of days that get commits. Weekends run at 40% of it.
- Commits/day: each active day draws uniformly from the range.
- Include weekends: off means Saturday and Sunday stay empty.
- Issues: share of active days that also open an issue (closed at the end of the day).
- Pull requests: share of active days whose commits arrive on a branch, get
  merged through a PR, and, with "Review each PR", get a review comment first.
- Hours: window the commits are timestamped in. One random start, then a
  constant gap of 3 to 12 minutes between commits, which is what real
  gapless output looks like.
- Randomize: new seed for the shown year.
- Click any square to pin its count (0 keeps it empty, blank returns it to automatic).

Days that already have real activity are never touched by a backfill.

## Backfill and the daily run

"Backfill past dates" pushes every planned commit for the shown year, dated
to their planned minutes. "Keep committing every day" makes the service
commit today's plan at the planned times, day after day. If the machine was
off, the next run catches up the missed days.

Issues, PRs and reviews cannot be backdated (GitHub stamps them at creation),
so they only happen on the day itself while the daily run is on.

Cron users can skip the service and run `python3 gapfree.py tick` every few
minutes instead; the web UI is still needed once to set things up.

## Files

- `~/.gapfree/config.json`: settings, seeds, overrides (mode 600).
- `~/.gapfree/repo`: local clone of the activity repo.
- `~/.gapfree/gapfree.log`: what was pushed and when.

`GAPFREE_HOME`, `GAPFREE_PORT` and `GAPFREE_BIND` override the defaults.

## Check

```sh
python3 test_gapfree.py
```

Fails if the planner drifts from the reference values taken from gapless.sh.
