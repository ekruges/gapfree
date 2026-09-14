# Changelog

## 0.1.0 (2026-09-14)

First release.

- Contribution graph preview with density, commits per day, weekends, per-year randomize and per-day pins. The planner uses the seeded FNV-1a hash gapless.sh ships, so plans look the same.
- Activity mix: linked commits / pull requests / issues / code review sliders with a radar preview, a balanced shuffle, and an optional fresh shape per year.
- Drag a period on the graph and backfill it. Days that already have activity are skipped, and dates before the account existed need a confirmation.
- Daily run: commits at their planned minutes, issues opened and closed, commit slices merged through rebase pull requests with review comments, catch-up after downtime.
- One-click private repo setup, or pick an existing repo.
- Installer for launchd, systemd (user or system) and cron; Docker files.
