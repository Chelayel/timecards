# timecards

Fill, review and submit ServiceNow time cards from the terminal instead of the portal.

- **SSO-friendly:** `tc login` opens a real browser once; the session is reused for API calls.
- **Copies your last week:** `tc fill` repeats your most recent week without PTO (tasks, categories,
  hours), skipping cards that already exist. Config `[[rows]]` are the fallback (or `--defaults`).
- **Preview first:** every write shows the week (new cells green, changes `old→new`) and asks.
- **PTO:** full or partial days; the PTO hours come off your other cards.
- **One-off changes:** `tc fill mon pto 4, tue general +1` tweaks days without editing config.
- **Submit:** runs your instance's own submit action (e.g. "Submit for Approval"), so approvals
  and notifications happen exactly as they do in the UI.
- **TUI:** `tc ui` is an editable grid of the week.

> Unofficial tool, not affiliated with ServiceNow. It uses your own session and permissions
> through the standard Table API and list UI. Check your organization's policies before automating.

## How it works

- Reads and writes the `time_card` table through the REST Table API, authenticated with your
  browser session cookie plus its CSRF token (`g_ck`) — no password or API credentials stored.
- Submitting runs a UI action from the classic `time_card` list ("Actions on selected rows…")
  in a headless browser, after checking the selected rows match what the API returned.

## Setup

```sh
git clone https://github.com/Chelayel/timecards && cd timecards
uv tool install --editable .      # installs the `tc` command
uvx playwright install chromium   # only needed if you don't have Chrome
tc init --instance acme --portal-page /sp   # writes ~/.config/timecards/config.toml
```

`--instance` takes a name (`acme` → `https://acme.service-now.com`) or a full URL.
`--portal-page` is any page on the instance that requires login (the page you normally open
for time cards works best). Both are stored per machine; `TC_INSTANCE` / `TC_PORTAL_PAGE`
environment variables override them. Then:

```sh
tc login              # complete SSO in the browser window
tc discover           # your recent cards, categories and time_card field names
tc init --from-last   # default rows (and week start) from your latest week
```

Session data and config live in `~/.config/timecards/` (session file is `chmod 600`).

## Weekly routine

On Monday, for the week that just ended:

```sh
tc fill -n                          # 1. preview: the week before + saved PTO (nothing saved)
tc fill mon pto 4, thu general +1   # 2. save, with any one-off changes (asks first)
tc show                             # 3. double-check
tc submit                           # 4. Submit for Approval (asks first)
```

Plan PTO ahead with `tc pto 2026-12-24..2026-12-31`; every `fill` applies it.
If your session expired, `tc` offers to open the login window, then continues.

## Usage

Commands act on **last week** by default (`default_week = -1`); use `-o 0` for this week,
`-o -2` for two weeks ago, or `-w YYYY-MM-DD` for the week containing a date.

| Command | What it does |
|---|---|
| `tc fill [mon pto 4, tue general +1 …]` | Preview the week before (or `--defaults`) + PTO + one-off changes, save on confirm (`-n` dry run, `-y` no prompt, `--pto DATE`, `--overwrite`) |
| `tc show` | Show the week |
| `tc ui` | Grid editor: Enter edit · a add · x delete · f fill · p PTO day · s save · S submit · [ ] week |
| `tc submit` | Show the week, then run `submit_action` on its editable cards (`-n` to check only) |
| `tc pto 2026-10-12 2026-10-16:4 2026-12-24..2026-12-31` | Save PTO days (`:4` = 4h; ranges skip weekends); `tc pto` lists, `-r` removes |
| `tc clear` | Delete the week's editable cards (after a preview + confirm) |
| `tc discover` | Recent cards, category/task combos, and `time_card` field names |

### One-off changes on the fill line

Add `DAY CARD HOURS` groups after `tc fill` to change specific days for this run:

```sh
tc fill mon pto 4, tue general +1, fri task_work 6
tc fill wed pto            # full PTO day
```

- `CARD` is `pto`, or one word that identifies a single card: its category, a field value
  (e.g. a subcategory), or part of the task number/title.
- `4` sets the hours; `+1` / `-1` adjust the copied (or default) hours for that day (re-running never
  adds twice); `pto` without hours is a full day. PTO hours come off the other cards.

## Configuration

`~/.config/timecards/config.toml` — see the comments `tc init` writes. An example for an
instance with Monday weeks, a custom `u_subcategory` field, and PTO logged as admin time:

```toml
instance = "https://example.service-now.com"
portal_page = "/sp"
week_starts_on = "monday"
default_week = -1
fill_from = "last_week"     # or "defaults" to always use [[rows]]
editable_states = ["Active", "Pending", "Rejected"]
submit_action = "Submit for Approval"

[[rows]]
task = "PRJTASK0012345"   # number, title or sys_id
category = "task_work"
hours = { monday = 7, tuesday = 7, wednesday = 7, thursday = 7, friday = 7 }

[[rows]]
task = ""
category = "admin"
fields = { u_subcategory = "general" }
hours = { monday = 1, tuesday = 1, wednesday = 1, thursday = 1, friday = 1 }

[pto]
category = "admin"
fields = { u_subcategory = "pto" }
hours = 8
```

- **What fill starts from:** with `fill_from = "last_week"`, the most recent week (up to 8 back)
  that has cards and no PTO is copied — a PTO week has other cards zeroed, so it's skipped.
  If there is none, `[[rows]]` are used. The preview says which: `Based on: copied from week of …`.
- **Tasks** can be a number, the title ServiceNow displays, or a sys_id (`task_id`). Titles are
  matched against tasks you've logged time on before, since titles repeat across projects.
- **Week start:** if a created card lands in a different week, it is deleted immediately and
  `tc` tells you which `week_starts_on` to set.
- **PTO:** full day puts `hours` on the PTO card and zeroes the others; `DATE:4` moves 4 hours
  from the other cards (biggest first). Re-running never double-counts.

## Scheduling (macOS/Linux)

Fill the week that just ended every Monday at 9:00; review and `tc submit` yourself:

```sh
(crontab -l 2>/dev/null; echo "0 9 * * 1 $(which tc) fill --yes >> ~/.config/timecards/fill.log 2>&1") | crontab -
```

If the SSO session has expired, the job fails with "run `tc login`". In a terminal, `tc`
offers to open the login window and then re-runs your command.

## Troubleshooting

| Message | Fix |
|---|---|
| `No ServiceNow instance set` / login opens the wrong site | Run `tc init --instance <name> --portal-page <page>` on that machine (each machine has its own config). |
| `Not logged in to ServiceNow. Run tc login.` | SSO session expired: `tc login` (or answer **y** when `tc` offers). |
| `ServiceNow moved the card to week …` | Set `week_starts_on` to the day it names; the misplaced card was already deleted. |
| `'admin' matches several cards (…)` | Use a more specific word from the list, e.g. `general` instead of `admin`. |
| `Task '…' not found` | Log time on it once in the portal, or put its sys_id in `task_id`. |
| `'Submit for Approval' isn't available` | Set `submit_action` to the label in your instance's time card list actions. |
| `… is listed twice in config.toml` | Remove the duplicate `[[rows]]` block. |

## Development

```sh
uv run pytest   # tests run against an in-memory fake of the time_card table
```

## Updating

```sh
cd timecards && git pull
uv tool install --editable . --force   # refresh the `tc` command (needed when entry points change)
```

## License

MIT
