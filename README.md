# gapfree

Fills your GitHub contribution graph. Free.

There is a service that charges $2 a month to run a hash function and
`git commit` on a schedule. This is the hash function and `git commit`.
It should not be a paid service, so it is not one.

## Install

Mac, Linux, a Raspberry Pi, a 256 MB container in your homelab, whatever
has `python3` and `git`:

```sh
curl -fsSL https://raw.githubusercontent.com/ekruges/gapfree/main/install.sh | sh
```

It installs itself as a service that survives reboots, then asks you two
things: which GitHub account (a device code, you approve it in any browser)
and what to call the private repo the commits go into. Then it opens
http://localhost:7331. That is the whole setup.

Docker, if that is your thing:

```sh
git clone https://github.com/ekruges/gapfree && cd gapfree && docker compose up -d
```

Then open http://localhost:7331 and press Publish.

On a headless box, reach the page over Tailscale or `ssh -L 7331:localhost:7331 box`.
There is no login on it, so do not put it on the open internet.

## What it does

- Shows your real graph, with the plan drawn on top.
- Density, commits per day, weekends, one seed per year. Same arithmetic as the paid one, so it looks the same.
- A mix of commits, pull requests, issues and reviews, so the activity overview on your profile fills in too, not only the squares.
- Drag across the graph, press Backfill: past days get their commits, dated to the minute. Days that already have something are left alone.
- Switch on the daily run and it keeps going. Forever. Missed a day because the box was off? It catches up.

Every commit is one line in `log/YYYY/MM-DD.md` in a private repo of yours.
Delete the repo and it is all gone.

Turn on "Include private contributions" on your profile, or the squares stay
hidden. GitHub stamps issues and pull requests when they are created, so
those only happen on the day itself; backfill is commits only.

## Running it by hand

```sh
python3 gapfree.py setup     # log in, pick the repo
python3 gapfree.py serve     # the page and the daily run
python3 gapfree.py tick      # one pass, if you prefer cron
```

Settings live in `~/.gapfree/config.json`. `GAPFREE_HOME`, `GAPFREE_PORT`
and `GAPFREE_BIND` override the defaults.

## Check

```sh
python3 test_gapfree.py
```

MIT.
