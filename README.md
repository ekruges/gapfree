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

On a homelab box or container where you are root, the installer writes a
system unit instead of a user one. Set `GAPFREE_BIND=0.0.0.0` before
running it if the UI should be reachable from outside the container (put
Tailscale or an SSH tunnel in front; there is no login). 256 MB of RAM and
a 2 GB disk are plenty.

## First run

The installer ends in `gapfree setup`, which you can rerun any time:

```sh
gapfree setup
```

It logs you in through the GitHub CLI (a device code, so it works on a
headless box: open the link on any device and type the code), asks whether
you have a second account for the badges (log in to it, or it opens the
signup page and waits), picks the repo, and restarts the service. No tokens
to copy. Without the CLI, paste tokens under Settings instead: `repo` scope
(classic). The second account's token has to be classic too, because a
fine-grained token cannot be scoped to a repo another user owns.

Then:

1. Open http://localhost:7331. Repository card: press Publish for a fresh
   private `activity-log` repo, or pick an existing one under "Existing
   repo"; gapfree only appends to `log/` in it.
2. Turn on "Include private contributions" on your GitHub profile
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

## Achievements

The Achievements card reads the badges on your public profile and shows what
each one takes. What this account learned the hard way: Quickdraw (close an
issue within 5 minutes) and YOLO (merge a PR unreviewed) are earned solo, and
gapfree does both on its own. Self-merged pull requests, self-co-authored
commits and self-answered discussions earned nothing, even from a public
repo, so Pull Shark, Pair Extraordinaire and Galaxy Brain need a second
account you own. Put that account's token under Settings as the buddy:
gapfree invites it to the repo, lets it merge your pull requests, adds it as
co-author on PR commits, and has it ask the Q&A questions you answer and
accept. Each stops at its top tier. "Earn the badges now" (or `gapfree
badges`) does the first two merged PRs, the co-authored commit and two
answered discussions immediately, so you can confirm the badges land
instead of waiting for the daily run to get there.

## Files

- `~/.gapfree/config.json`: settings, tokens, seeds, overrides, issue and PR numbers per day (mode 600).
- `~/.gapfree/repo`: local clone of the activity repo.
- `~/.gapfree/gapfree.log`: what was pushed and when.

`GAPFREE_HOME`, `GAPFREE_PORT` and `GAPFREE_BIND` override the defaults.

## Check

```sh
python3 test_gapfree.py
```

Fails if the planner drifts from the reference values taken from gapless.sh
or the mix stops adding up.
