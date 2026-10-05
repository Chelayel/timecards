"""`tc` command line entry point."""

from __future__ import annotations

from datetime import date, timedelta

import typer
from rich.console import Console
from rich.table import Table

from . import auth, config, overrides
from .api import ApiError, ServiceNow

app = typer.Typer(no_args_is_help=True, help="ServiceNow time cards without the portal.")
console = Console()

WeekOpt = typer.Option(None, "--week", "-w", help="Any date in the target week (YYYY-MM-DD). Default: default_week in config.")
OffsetOpt = typer.Option(None, "--offset", "-o", help="Weeks from this week: 0 = this week, -1 = last week.")


def _week(cfg: config.Config, week: str | None, offset: int | None) -> date:
    if week:
        return cfg.week_start(date.fromisoformat(week)) + timedelta(weeks=offset or 0)
    return cfg.week_start() + timedelta(weeks=cfg.default_week if offset is None else offset)


def _workdays(cfg: config.Config) -> set[str]:
    """Days that have default hours; ranges of PTO dates skip the others (weekends)."""
    return {d for r in cfg.rows for d, h in r.hours.items() if h} or set(config.DAYS[1:6])


def _pending(cards) -> list:
    """Cards that saving would create, change or delete."""
    return [c for c in cards if (c.sys_id is None and c.total and not c.deleted)
            or (c.sys_id and c.deleted)
            or (c.sys_id and c.dirty and c.hours != c.orig)]


def _print_week(sn: ServiceNow, week: date, cards=None, title: str | None = None) -> None:
    """Print a week. With planned `cards`, new cards are green and changed hours yellow (old→new)."""
    cfg = sn.cfg
    if cards is None:
        cards = sn.week_cards(week)
    cards = [c for c in cards if c.sys_id or (c.total and not c.deleted)]
    live = [c for c in cards if not c.deleted]
    t = Table(title=title or f"Week of {week:%a %b %d, %Y}")
    t.add_column("Task", no_wrap=True, overflow="ellipsis", max_width=20)
    t.add_column("Category", no_wrap=True)
    for d in cfg.week_days:
        t.add_column(d[:3].title(), justify="right", min_width=3)
    t.add_column("Total", justify="right", style="bold", min_width=5)
    t.add_column("State")

    def cell(c, d):
        h = c.hours[d]
        if c.deleted:
            return f"[red strike]{h:g}[/]"
        if c.sys_id is None:
            return f"[green]{h:g}[/]"
        if c.orig and h != c.orig[d]:
            return f"[yellow]{c.orig[d]:g}→{h:g}[/]"
        return f"{h:g}"

    for c in cards:
        cat = "/".join([c.category, *(v for v in c.fields.values() if v)])
        if c.deleted:
            state = "[red]delete[/]"
        elif c.sys_id is None:
            state = "[green]new[/]"
        else:
            state = f"[yellow]{c.state}*[/]" if c in _pending(cards) else c.state
        total = f"[red strike]{c.total:g}[/]" if c.deleted else f"{c.total:g}"
        t.add_row(c.task_label or "-", cat, *[cell(c, d) for d in cfg.week_days], total, state)
    if cards:
        t.add_section()
        t.add_row("", "Total", *[f"{sum(c.hours[d] for c in live):g}" for d in cfg.week_days],
                  f"{sum(c.total for c in live):g}", "")
    console.print(t if cards else f"[yellow]No time cards for week of {week}.[/]")


def _plan_and_save(sn: ServiceNow, wk: date, yes: bool, dry_run: bool, **plan) -> bool:
    """Show what a fill would do, ask, then save. Returns True if something was saved."""
    cards, log = sn.plan_week(wk, **plan)
    for line in log:
        if line.startswith("source"):
            console.print(f"Based on: {line.split(None, 1)[1]}")
    changes = _pending(cards)
    _print_week(sn, wk, cards, title=f"Week of {wk:%a %b %d, %Y} — preview")
    extra = [line.split(None, 1)[1] for line in log if line.startswith("extra")]
    if extra:
        console.print(f"[yellow]Already in this week but not in the source: {', '.join(extra)}. "
                      "Run with --replace to remove them.[/]")
    if not changes:
        console.print("Nothing to change.")
        return False
    new = sum(c.sys_id is None for c in changes)
    gone = sum(bool(c.deleted) for c in changes)
    console.print(f"{new} card(s) to create, {len(changes) - new - gone} to update, {gone} to delete "
                  "([green]green[/] = new, [yellow]yellow[/] = changed, [red]red[/] = deleted).")
    if dry_run:
        console.print("Dry run: nothing saved.")
        return False
    if not (yes or typer.confirm("Save to ServiceNow?")):
        console.print("Nothing saved.")
        return False
    sn.save_cards(wk, cards)
    console.print("[green]Saved.[/]")
    _print_week(sn, wk)
    return True


@app.command()
def login():
    """Open a browser to complete SSO once; the session is reused afterwards."""
    cfg = config.load()
    console.print(f"Opening {cfg.instance}{cfg.portal_page}")
    console.print("Complete the SSO login in the browser window (5 min timeout)...")
    s = auth.harvest(cfg, interactive=True)
    console.print(f"[green]Logged in as {s['user']['name']} ({s['user']['user_name']}).[/]")


@app.command()
def init(
    from_last: bool = typer.Option(False, "--from-last", help="Generate default rows from your latest week."),
    instance: str = typer.Option(None, "--instance", "-i", help="Instance name or URL, e.g. acme or https://acme.service-now.com."),
    portal_page: str = typer.Option(None, "--portal-page", help="Page that requires login, e.g. /sp or /esc."),
):
    """Create the config file (~/.config/timecards/config.toml) and set your instance."""
    created = config.ensure_config()
    values = {}
    if instance:
        values["instance"] = config.normalize_instance(instance)
    if portal_page:
        values["portal_page"] = portal_page if portal_page.startswith("/") else "/" + portal_page
    if values:
        config.set_values(**values)
        console.print(", ".join(f"{k} = {v}" for k, v in values.items()))
    if from_last:
        cfg = config.load()
        sn = ServiceNow(cfg)
        recent = sn.recent_cards()
        if not recent:
            console.print("[yellow]No previous time cards found.[/]")
            raise typer.Exit(1)
        latest = recent[0][0]
        rows = [config.DefaultRow(task=c.task_label, task_id=c.task_id, category=c.category, fields=c.fields,
                                  hours={d: c.hours[d] for d in config.DAYS if c.hours[d]})
                for w, c, _ in recent if w == latest and c.total and not sn.is_pto(c)]
        seen = set()  # a week with duplicate cards must not produce duplicate defaults
        rows = [r for r in rows if (k := (r.task_id, r.category, tuple(sorted(r.fields.items())))) not in seen
                and not seen.add(k)]
        lines = config.CONFIG_FILE.read_text().splitlines()
        first_row = next((i for i, l in enumerate(lines) if l.startswith("[[rows]]")), len(lines))
        start = date.fromisoformat(latest).strftime("%A").lower()  # the instance's real week start
        head = [f'week_starts_on = "{start}"' if l.startswith("week_starts_on") else l
                for l in lines[:first_row] if not l.startswith("# Tip:")]
        pto_at = next((i for i, l in enumerate(lines) if l.startswith("[pto]")), None)
        if pto_at is None:
            pto = config.PTO_CONFIG.strip("\n")
        else:  # keep the existing [pto] section with its comment lines
            while pto_at > 0 and lines[pto_at - 1].startswith("#"):
                pto_at -= 1
            pto = "\n".join(lines[pto_at:])
        config.CONFIG_FILE.write_text("\n".join(head).rstrip() + "\n\n" + config.render_rows(rows) + "\n" + pto + "\n")
        console.print(f"Wrote {len(rows)} default row(s) from week of {latest}.")
    elif not created:
        console.print("Config already exists.")
    console.print(f"Config: {config.CONFIG_FILE}")


@app.command()
def show(week: str = WeekOpt, offset: int = OffsetOpt):
    """Show time cards for a week."""
    cfg = config.load()
    _print_week(ServiceNow(cfg), _week(cfg, week, offset))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def fill(
    ctx: typer.Context,
    week: str = WeekOpt,
    offset: int = OffsetOpt,
    overwrite: bool = typer.Option(False, help="Reset hours of existing editable cards to the source week/defaults."),
    defaults: bool = typer.Option(False, "--defaults", help="Start from config [[rows]] instead of copying the week before."),
    replace: bool = typer.Option(False, "--replace", help="Make the week match the source: reset hours and delete other editable cards."),
    pto: list[str] = typer.Option([], "--pto", "-p", help="PTO for this run: YYYY-MM-DD[:HOURS], a..b ranges, commas."),
    submit: bool = typer.Option(False, help="Submit the week after filling."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Only show what would be saved."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save (and submit) without asking, e.g. from cron."),
):
    """Preview the week (copied from the week before, or your defaults) + PTO, then save after you confirm.

    Add per-day changes after the options: DAY CARD HOURS, comma separated, e.g.

        tc fill mon pto 4, tue general +1, fri task_work 6

    CARD is "pto" or a word matching one card (category, field value, task number/title).
    HOURS sets the value; +N/-N adjusts the default for that day; "pto" alone = full day.
    """
    try:
        changes = overrides.parse(ctx.args)
    except overrides.OverrideError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2)
    cfg = config.load()
    sn = ServiceNow(cfg)
    wk = _week(cfg, week, offset)
    pto_dates = config.load_pto() | config.parse_dates(pto, _workdays(cfg))
    if changes:
        console.print("Changes: " + ", ".join(map(str, changes)))
    try:
        _plan_and_save(sn, wk, yes, dry_run, overwrite=overwrite, pto_dates=pto_dates, changes=changes,
                       use_defaults=defaults, replace=replace)
    except ApiError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    if submit and not dry_run and (yes or typer.confirm("Submit this week?")):
        console.print(sn.submit_week(wk))


@app.command("pto")
def pto_cmd(
    dates: list[str] = typer.Argument(None, help="YYYY-MM-DD, or a..b range (weekdays only); add :HOURS for a partial day, e.g. 2026-10-12:4."),
    remove: bool = typer.Option(False, "--remove", "-r", help="Remove these dates instead of adding."),
    apply: bool = typer.Option(True, help="Update the affected weeks in ServiceNow now (after a preview)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without asking."),
):
    """Add/remove PTO days (no dates: list them). DATE:4 = 4h partial day; full days zero other cards."""
    cfg = config.load()
    saved = config.load_pto()
    if not dates:
        upcoming = [config.fmt_pto(d, h) for d, h in saved.items() if d >= cfg.week_start()]
        console.print("\n".join(upcoming) or "No upcoming PTO.")
        return
    new = config.parse_dates(dates, _workdays(cfg))
    if remove:
        config.save_pto({d: h for d, h in saved.items() if d not in new})
        console.print(f"Removed {len(new)} day(s). Run `tc fill --overwrite -w <date>` to restore default hours.")
        return
    if not cfg.pto:
        console.print("[red]Set the [pto] category/task in config.toml first (see `tc discover`).[/]")
        raise typer.Exit(1)
    config.save_pto(saved | new)
    console.print("PTO: " + ", ".join(config.fmt_pto(d, h) for d, h in new.items()))
    if apply:
        sn = ServiceNow(cfg)
        for wk in sorted({cfg.week_start(d) for d in new}):
            if not cfg.week_start() - timedelta(weeks=1) <= wk <= cfg.week_start():
                continue  # future weeks get it from `tc fill`; leave older weeks alone
            _plan_and_save(sn, wk, yes, False, pto_dates=new)


@app.command()
def submit(
    week: str = WeekOpt,
    offset: int = OffsetOpt,
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Check everything but don't submit."),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Show the week, then run "Submit for Approval" on its editable cards."""
    cfg = config.load()
    sn = ServiceNow(cfg)
    wk = _week(cfg, week, offset)
    _print_week(sn, wk)
    if dry_run or yes or typer.confirm(f"{cfg.submit_action} for this week?"):
        console.print(sn.submit_week(wk, dry_run=dry_run))


@app.command()
def clear(week: str = WeekOpt, offset: int = OffsetOpt, yes: bool = typer.Option(False, "--yes", "-y")):
    """Delete a week's editable (not yet submitted) cards, e.g. to start over."""
    cfg = config.load()
    sn = ServiceNow(cfg)
    wk = _week(cfg, week, offset)
    cards = [c for c in sn.week_cards(wk) if c.editable and c.sys_id]
    if not cards:
        console.print(f"No editable cards in week of {wk}.")
        return
    for c in cards:
        c.deleted = True
    _print_week(sn, wk, [c for c in sn.week_cards(wk)], title=f"Week of {wk:%a %b %d, %Y}")
    console.print(f"[red]{len(cards)} editable card(s) will be deleted[/] (submitted/frozen cards are kept).")
    if not (yes or typer.confirm("Delete them?")):
        console.print("Nothing deleted.")
        return
    sn.save_cards(wk, cards)
    console.print(f"[green]Deleted {len(cards)} card(s).[/]")


@app.command()
def discover(limit: int = 20):
    """Print your recent time cards and raw fields (to check tasks/categories/field names)."""
    cfg = config.load()
    sn = ServiceNow(cfg)
    console.print(f"User: {sn.me['name']} ({sn.me['user_name']})")
    recent = sn.recent_cards(limit)
    t = Table()
    for col in ("Week", "Task", "Category", "Total", "State"):
        t.add_column(col)
    for w, c, _ in recent:
        t.add_row(w, c.task_label or "-", c.category, f"{c.total:g}", c.state)
    console.print(t)
    cats = sorted({(c.category, c.task_label) for _, c, _ in recent})
    console.print("Category / task combos:", "; ".join(f"{c} / {t or '-'}" for c, t in cats))
    if recent:
        console.print("Fields on time_card:", ", ".join(sorted(recent[0][2])))


@app.command()
def ui(week: str = WeekOpt, offset: int = OffsetOpt):
    """Interactive terminal editor for a week."""
    from .tui import TimecardApp

    cfg = config.load()
    TimecardApp(cfg, _week(cfg, week, offset)).run()


def main():
    """Entry point: short errors instead of tracebacks, and offer to log in when the session expired."""
    import sys

    try:
        app()
    except auth.NeedLogin as e:
        if not (sys.stdin.isatty() and typer.confirm(f"{e} Log in now?", default=True)):
            console.print(f"[red]{e}[/]")
            raise SystemExit(1)
        login()
        app()  # run the original command again
    except (ApiError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)
