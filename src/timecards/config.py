"""User config: instance URL, week policy, and the default rows used to fill a week."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "timecards"
CONFIG_FILE = CONFIG_DIR / "config.toml"
SESSION_FILE = CONFIG_DIR / "session.json"
PROFILE_DIR = CONFIG_DIR / "browser-profile"
PTO_FILE = CONFIG_DIR / "pto.txt"

# ServiceNow time_card day fields, Sunday-first like the platform.
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

DEFAULT_CONFIG = """\
# Your ServiceNow instance, and any page on it that needs login (used to sign in).
instance = "https://YOUR-INSTANCE.service-now.com"
portal_page = "/sp"

# Must match your instance's time sheet policy ("sunday" or "monday").
# `tc init --from-last` detects it from your history.
week_starts_on = "monday"

# Week that commands use without --week/--offset: -1 = previous week, 0 = this week.
default_week = -1

# time_card states you can still edit, and the time_card list action `tc submit` runs on them
# (the label in "Actions on selected rows..." on your instance).
editable_states = ["Active", "Pending", "Rejected"]
submit_action = "Submit for Approval"

# "chrome" or "msedge" uses your installed browser (best for SSO / device checks);
# "" uses Playwright's bundled Chromium.
browser_channel = "chrome"

# Custom time_card fields for `tc init --from-last` to copy into rows (fields used below are added automatically).
extra_fields = []

# One [[rows]] block per time card to create each week.
# task: task number, title, or sys_id. Leave "" for no task.
# task_id: optional task sys_id; wins over `task` (written by `tc init --from-last`).
# category: time_card category value (e.g. task_work, admin, meeting, training).
# fields: optional extra time_card fields, e.g. custom ones like { u_subcategory = "general" }.
#         Cards with different field values count as different cards.
# Tip: run `tc init --from-last` to generate these from your most recent week.
[[rows]]
task = ""
category = "admin"
hours = { monday = 8, tuesday = 8, wednesday = 8, thursday = 8, friday = 8 }
"""

PTO_CONFIG = """
# Card used for PTO days. Full day: the PTO card gets `hours`, every other card 0.
# Partial day (DATE:4): the hours come off the other cards, biggest first.
# Run `tc discover` to see the category/task your PTO cards normally use.
[pto]
task = ""
category = ""            # e.g. "admin" or "out_of_office"
# fields = { u_subcategory = "pto" }
hours = 8
"""


@dataclass
class DefaultRow:
    task: str
    category: str
    hours: dict[str, float]
    task_id: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    instance: str
    portal_page: str
    week_starts_on: str
    browser_channel: str
    editable_states: list[str] = field(default_factory=lambda: ["Active", "Pending", "Rejected"])
    submit_action: str = "Submit for Approval"
    default_week: int = -1
    extra_fields: list[str] = field(default_factory=list)
    rows: list[DefaultRow] = field(default_factory=list)
    pto: DefaultRow | None = None  # hours holds a single "day" entry: hours per PTO day

    def __post_init__(self):
        # Custom time_card fields to read back and match on: listed ones + any used in rows / [pto].
        used = [k for r in [*self.rows, *([self.pto] if self.pto else [])] for k in r.fields]
        self.extra_fields = list(dict.fromkeys([*self.extra_fields, *used]))

    @property
    def week_days(self) -> list[str]:
        """Day names in display order, starting at the configured week start."""
        i = DAYS.index(self.week_starts_on)
        return DAYS[i:] + DAYS[:i]

    def week_start(self, d: date | None = None) -> date:
        d = d or date.today()
        sunday_idx = (d.weekday() + 1) % 7
        offset = (sunday_idx - DAYS.index(self.week_starts_on)) % 7
        return d - timedelta(days=offset)


def ensure_config() -> bool:
    """Write the default config if missing. Returns True if it was created."""
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(DEFAULT_CONFIG + PTO_CONFIG)
    return True


def load() -> Config:
    ensure_config()
    raw = tomllib.loads(CONFIG_FILE.read_text())
    week_start = raw.get("week_starts_on", "monday").lower()
    if week_start not in DAYS:
        raise ValueError(f"week_starts_on must be one of {DAYS}, got {week_start!r}")
    rows = []
    for r in raw.get("rows", []):
        hours = {k.lower(): float(v) for k, v in r.get("hours", {}).items()}
        bad = set(hours) - set(DAYS)
        if bad:
            raise ValueError(f"Unknown day(s) in hours: {sorted(bad)}")
        rows.append(DefaultRow(task=r.get("task", ""), category=r.get("category", ""), hours=hours,
                               task_id=r.get("task_id", ""), fields=dict(r.get("fields", {}))))
    pto = None
    if p := raw.get("pto"):
        if p.get("category") or p.get("task") or p.get("task_id"):
            pto = DefaultRow(task=p.get("task", ""), category=p.get("category", ""),
                             hours={"day": float(p.get("hours", 8))}, task_id=p.get("task_id", ""),
                             fields=dict(p.get("fields", {})))
    return Config(
        instance=raw.get("instance", "").rstrip("/"),
        portal_page=raw.get("portal_page", "/sp"),
        week_starts_on=week_start,
        browser_channel=raw.get("browser_channel", "chrome"),
        editable_states=raw.get("editable_states", ["Active", "Pending", "Rejected"]),
        submit_action=raw.get("submit_action", "Submit for Approval"),
        default_week=int(raw.get("default_week", -1)),
        extra_fields=list(raw.get("extra_fields", [])),
        rows=rows,
        pto=pto,
    )


def render_rows(rows: list[DefaultRow]) -> str:
    """TOML text for [[rows]] blocks (used by `tc init --from-last`)."""
    out = []
    for r in rows:
        hours = ", ".join(f"{d} = {h:g}" for d, h in r.hours.items() if h)
        task_id = f'task_id = "{r.task_id}"\n' if r.task_id else ""
        fields = ", ".join(f'{k} = "{v}"' for k, v in r.fields.items() if v)
        fields = f"fields = {{ {fields} }}\n" if fields else ""
        out.append(f'[[rows]]\ntask = "{r.task}"\n{task_id}category = "{r.category}"\n{fields}hours = {{ {hours} }}\n')
    return "\n".join(out)


# -- PTO dates (pto.txt: "YYYY-MM-DD" for a full day, "YYYY-MM-DD 4" for 4 hours) ------

PtoDates = dict[date, float | None]  # None = full day


def parse_dates(specs: list[str], workdays: set[str]) -> PtoDates:
    """Parse "YYYY-MM-DD", "a..b" ranges (workdays only), each optionally ":HOURS"."""
    out: PtoDates = {}
    for spec in specs:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            part, _, hrs = part.partition(":")
            hours = float(hrs) if hrs else None
            if ".." in part:
                a, b = (date.fromisoformat(x) for x in part.split(".."))
                d = a
                while d <= b:
                    if day_name(d) in workdays:
                        out[d] = hours
                    d += timedelta(days=1)
            else:
                out[date.fromisoformat(part)] = hours
    return dict(sorted(out.items()))


def day_name(d: date) -> str:
    return DAYS[(d.weekday() + 1) % 7]


def fmt_pto(d: date, hours: float | None) -> str:
    return f"{d:%a %Y-%m-%d}" + (f" ({hours:g}h)" if hours is not None else "")


def load_pto() -> PtoDates:
    if not PTO_FILE.exists():
        return {}
    out: PtoDates = {}
    for line in PTO_FILE.read_text().splitlines():
        if line.strip():
            d, *h = line.split()
            out[date.fromisoformat(d)] = float(h[0]) if h else None
    return dict(sorted(out.items()))


def save_pto(dates: PtoDates) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PTO_FILE.write_text("".join(f"{d.isoformat()}{'' if h is None else f' {h:g}'}\n" for d, h in sorted(dates.items())))
