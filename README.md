# gapfree

Free, self-hosted filler for the GitHub contribution graph. One Python file,
no dependencies beyond `python3` and `git`.

It does what gapless.sh sells for $2 a month, with the same planning
arithmetic for the commit squares, and adds what that service skips: a mix
of pull requests, issues and reviews so the activity overview on your
profile fills in too, a scheduler that keeps the plan running on your own
machine for good, and a one-click private repo to put it all in.

Every commit appends one line to `log/YYYY/MM-DD.md` in that repo, so the
history stays inspectable and easy to delete.

## Install

Anything with `python3` (3.9+) and `git`: a Raspberry Pi, a 256 MB LXC, your
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

## First run

1. Open http://localhost:7331. If the `gh` CLI is logged in on that machine
   you are already connected. Otherwise paste a token under Settings:
   `repo` scope (classic) or Contents, Issues and Pull requests write access
   on the one repo (fine-grained).
2. Repository card: press Publish. A private `activity-log` repo is created
   with a README and the log layout shown in the preview. Or switch to
   "Existing repo" and pick one you already have; gapfree only appends to
   `log/` in it.
3. Turn on "Include private contributions" on your GitHub profile
   (Contribution settings on the profile page), or the private repo's
   activity stays hidden.

The UI binds to localhost only. To reach it from elsewhere, put Tailscale or
an authenticating proxy in front of it. There is no login.

## The knobs

The plan is deterministic: every year has a random seed, and each day is
hashed with that seed (FNV-1a over `date:tag`, the arithmetic gapless.sh
ships in its bundle). Change a slider and the same days move, so the
preview is exactly what gets pushed.

Contribution graph

- Density: share of days that get commits. Weekends run at 40% of it.
- Commits/day: each active day draws uniformly from the range.
- Weekends: off keeps Saturday and Sunday empty.
- Randomize year: new seed for the shown year.
- Click any square to pin its count (0 keeps it empty, blank returns it to automatic).

Activity mix

Four linked sliders, the shares GitHub's activity overview shows: commits,
pull requests, issues, code review. Move one and the rest give way
proportionally. The radar shows the target in green and what the year adds
up to in blue (your public activity, what is in the repo, and the plan).

Not sure what shape to pick? "Shuffle a balanced shape" draws one a real
profile could have (commits lead, pull requests next, reviews a share of
those). "Fresh shape every year" derives the mix from each year's seed
instead of the sliders, so the overview changes a little from year to year
without anyone touching it.

Per active day the mix sets how many extras ride along with the commits:
some commit slices (one to three commits) go out on a branch and get
rebase-merged through a pull request, some of those pull requests get a
review comment first, and issues open during the day and close at the end.
GitHub credits one review per pull request, so reviews never exceed PRs.

Timing: one random start inside the hours window, then a constant gap of 3
to 12 minutes between commits, which is what real gapless output looks like.

## Fill a period

Pick a date range (presets: this year, last year, last 12 months) and press
Backfill. Every past day in the range that is still empty (nothing of yours,
nothing in the repo) gets its planned commits, dated to their planned minutes. Issues, PRs and
reviews cannot be backdated (GitHub stamps them at creation), so past days
get commits only.

## Automation

"Keep committing every day" makes the service run today's plan at the
planned minutes, open and close the issues, merge the pull requests, and
carry on into the next year with a fresh seed. If the machine was off, the
next pass catches up the missed days (commits only). The Automation card
shows the service it runs under and the next planned days.

Cron users can skip the service and run `python3 gapfree.py tick` every few
minutes; the web UI is still needed once for setup.

## Files

- `~/.gapfree/config.json`: settings, seeds, overrides, issue and PR numbers per day (mode 600).
- `~/.gapfree/repo`: local clone of the activity repo.
- `~/.gapfree/gapfree.log`: what was pushed and when.

`GAPFREE_HOME`, `GAPFREE_PORT` and `GAPFREE_BIND` override the defaults.

## Check

```sh
python3 test_gapfree.py
```

Fails if the planner drifts from the reference values taken from gapless.sh
or the mix stops adding up.
