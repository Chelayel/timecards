"""Thin ServiceNow Table API client for time_card / time_sheet."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from . import auth
from .config import DAYS, Config, DefaultRow, day_name

class ApiError(Exception):
    pass


@dataclass
class Card:
    sys_id: str | None
    task_id: str  # task sys_id ("" for none)
    task_label: str  # task number shown to the user
    category: str
    hours: dict[str, float] = field(default_factory=lambda: {d: 0.0 for d in DAYS})
    state: str = ""
    time_sheet: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    locked: bool = False  # state is not editable
    orig: dict[str, float] = field(default_factory=dict)  # hours as loaded from ServiceNow
    dirty: bool = False
    deleted: bool = False

    @property
    def total(self) -> float:
        return sum(self.hours.values())

    @property
    def key(self) -> tuple:
        """Identity for matching defaults to existing cards (e.g. admin/general vs admin/pto differ)."""
        return (self.task_id, self.category, tuple(sorted((k, v) for k, v in self.fields.items() if v)))

    @property
    def editable(self) -> bool:
        return not self.locked


def _v(rec: dict, name: str, display: bool = False) -> str:
    """Read a field from a sysparm_display_value=all record."""
    f = rec.get(name, "")
    if isinstance(f, dict):
        return f.get("display_value" if display else "value") or ""
    return f or ""


class ServiceNow:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = auth.load_session()
        self._http: httpx.Client | None = None
        self._recent: list[tuple[str, Card, dict]] | None = None

    # -- transport ---------------------------------------------------------

    def _client(self) -> httpx.Client:
        if self._session is None:
            self._session = auth.harvest(self.cfg, interactive=False)
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.cfg.instance,
                cookies=self._session["cookies"],
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-UserToken": self._session["token"],
                },
                timeout=30,
            )
        return self._http

    def _refresh(self) -> None:
        if self._http:
            self._http.close()
        self._http = None
        self._session = auth.harvest(self.cfg, interactive=False)

    def request(self, method: str, path: str, **kw) -> dict:
        resp = self._client().request(method, path, **kw)
        if resp.status_code in (301, 302, 303, 401):
            self._refresh()  # session expired; silently re-auth via the saved browser profile
            resp = self._client().request(method, path, **kw)
        if resp.status_code >= 400:
            try:
                msg = resp.json()["error"]["message"]
            except Exception:
                msg = resp.text[:300]
            raise ApiError(f"{method} {path} -> {resp.status_code}: {msg}")
        return resp.json() if resp.content else {}

    @property
    def me(self) -> dict:
        self._client()
        return self._session["user"]

    # -- time cards --------------------------------------------------------

    def _query_cards(self, query: str, limit: int = 200) -> list[dict]:
        return self.request("GET", "/api/now/table/time_card", params={
            "sysparm_query": query,
            "sysparm_display_value": "all",
            "sysparm_exclude_reference_link": "true",
            "sysparm_limit": limit,
        })["result"]

    def _to_card(self, rec: dict) -> Card:
        return Card(
            sys_id=_v(rec, "sys_id"),
            task_id=_v(rec, "task"),
            task_label=_v(rec, "task", display=True),
            category=_v(rec, "category"),
            hours={d: float(_v(rec, d) or 0) for d in DAYS},
            state=_v(rec, "state", display=True),
            time_sheet=_v(rec, "time_sheet"),
            fields={f: _v(rec, f) for f in self.cfg.extra_fields if _v(rec, f)},
            locked=bool(_v(rec, "state")) and _v(rec, "state") not in self.cfg.editable_states,
            orig={d: float(_v(rec, d) or 0) for d in DAYS},
        )

    def week_cards(self, week: date) -> list[Card]:
        q = f"user={self.me['sys_id']}^week_starts_on={week.isoformat()}^state!=Cancelled^ORDERBYsys_created_on"
        return [self._to_card(r) for r in self._query_cards(q)]

    def recent_cards(self, limit: int = 50) -> list[tuple[str, Card, dict]]:
        q = f"user={self.me['sys_id']}^ORDERBYDESCweek_starts_on"
        return [(_v(r, "week_starts_on"), self._to_card(r), r) for r in self._query_cards(q, limit)]

    def find_task(self, ref: str) -> tuple[str, str]:
        """Resolve a task number, title, or sys_id to (sys_id, label).

        Checks tasks you've logged time against first: titles repeat across projects,
        and many users can't query the task table directly.
        """
        ref = ref.strip()
        if self._recent is None:
            self._recent = self.recent_cards(200)
        for _, c, _ in self._recent:
            if c.task_id and ref in (c.task_id, c.task_label):
                return c.task_id, c.task_label
        if re.fullmatch(r"[0-9a-f]{32}", ref):
            return ref, ref
        try:
            res = self.request("GET", "/api/now/table/task", params={
                "sysparm_query": f"number={ref}^ORshort_description={ref}",
                "sysparm_fields": "sys_id,number,short_description",
                "sysparm_limit": 2,
            })["result"]
        except ApiError as e:
            raise ApiError(f"Can't look up task {ref!r} ({e}). Put its sys_id in task_id.") from e
        if len(res) > 1:
            raise ApiError(f"Task {ref!r} matches several tasks. Use its number or set task_id.")
        if not res:
            raise ApiError(f"Task {ref!r} not found (or not visible to you)")
        return res[0]["sys_id"], res[0]["short_description"] or res[0]["number"]

    def _resolve(self, row: DefaultRow) -> tuple[str, str]:
        if row.task_id:
            try:
                return self.find_task(row.task_id)[0], row.task or row.task_id
            except ApiError:
                return row.task_id, row.task or row.task_id
        return self.find_task(row.task) if row.task else ("", "")

    def _payload(self, card: Card) -> dict:
        body = {d: f"{card.hours.get(d, 0):g}" for d in DAYS}
        body["category"] = card.category
        body["task"] = card.task_id
        return body | card.fields

    def create_card(self, week: date, card: Card) -> Card:
        body = self._payload(card) | {"user": self.me["sys_id"], "week_starts_on": week.isoformat()}
        rec = self.request("POST", "/api/now/table/time_card", json=body,
                           params={"sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"})["result"]
        got = _v(rec, "week_starts_on")
        if got != week.isoformat():
            # The instance snapped the card to another week: undo, don't leave it in the wrong week.
            self.delete_card(_v(rec, "sys_id"))
            day = date.fromisoformat(got).strftime("%A").lower() if got else "?"
            raise ApiError(f"ServiceNow moved the card to week {got}; set week_starts_on = \"{day}\" in config.toml.")
        return self._to_card(rec)

    def update_card(self, card: Card) -> Card:
        rec = self.request("PATCH", f"/api/now/table/time_card/{card.sys_id}", json=self._payload(card),
                           params={"sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"})["result"]
        return self._to_card(rec)

    def delete_card(self, sys_id: str) -> None:
        self.request("DELETE", f"/api/now/table/time_card/{sys_id}")

    def submit_week(self, week: date, dry_run: bool = False) -> str:
        """Run the instance's submit UI action (default "Submit for Approval") on the week's editable cards."""
        from . import ui

        if not self.cfg.submit_action:
            raise ApiError("No submit_action configured in config.toml.")
        cards = [c for c in self.week_cards(week) if c.editable and c.sys_id]
        if not cards:
            return "Nothing editable to submit."
        states = "^OR".join(f"state={s}" for s in self.cfg.editable_states)
        query = f"user={self.me['sys_id']}^week_starts_on={week.isoformat()}^{states}"
        msgs = ui.run_list_action(self.cfg, "time_card", query, {c.sys_id for c in cards}, self.cfg.submit_action,
                                  dry_run=dry_run)
        if dry_run:
            return "\n".join(msgs)
        after = {c.sys_id: c.state for c in self.week_cards(week)}
        result = ", ".join(f"{c.task_label or c.category}: {c.state} → {after.get(c.sys_id, 'gone')}" for c in cards)
        return "\n".join([result, *msgs])

    # -- defaults ----------------------------------------------------------

    def default_cards(self) -> list[Card]:
        """Build unsaved cards from the config's default rows."""
        out = []
        for r in self.cfg.rows:
            task_id, label = self._resolve(r)
            out.append(Card(sys_id=None, task_id=task_id, task_label=label, category=r.category,
                            hours={d: r.hours.get(d, 0.0) for d in DAYS}, fields=dict(r.fields), dirty=True))
        return out

    def is_pto(self, c: Card) -> bool:
        p = self.cfg.pto
        return bool(p) and c.category == p.category and all(c.fields.get(k) == v for k, v in p.fields.items())

    def source_cards(self, week: date, use_defaults: bool = False) -> tuple[list[Card], str]:
        """Unsaved cards to base `week` on, and a description of where they came from.

        fill_from = "last_week": copy the most recent earlier week that has cards and no PTO
        (PTO zeroes other cards, so such a week isn't a typical one). Falls back to config rows.
        """
        if not use_defaults and self.cfg.fill_from == "last_week":
            q = (f"user={self.me['sys_id']}^week_starts_on<{week.isoformat()}"
                 f"^week_starts_on>={(week - timedelta(weeks=8)).isoformat()}^state!=Cancelled^ORDERBYDESCweek_starts_on")
            by_week: dict[str, list[Card]] = {}
            for rec in self._query_cards(q):
                by_week.setdefault(_v(rec, "week_starts_on"), []).append(self._to_card(rec))
            for wk in sorted(by_week, reverse=True):
                cards = [c for c in by_week[wk] if c.total]
                if not cards or any(self.is_pto(c) for c in cards):
                    continue
                merged: dict[tuple, Card] = {}
                for c in cards:  # identical cards in one week: keep the hours that were logged
                    if c.key in merged:
                        m = merged[c.key]
                        m.hours = {d: m.hours[d] + c.hours[d] for d in DAYS}
                    else:
                        merged[c.key] = Card(sys_id=None, task_id=c.task_id, task_label=c.task_label,
                                             category=c.category, hours=dict(c.hours), fields=dict(c.fields),
                                             dirty=True)
                return list(merged.values()), f"copied from week of {wk}"
        if not self.cfg.rows:
            raise ApiError("No earlier week to copy and no [[rows]] in config.toml.")
        return self.default_cards(), "from config defaults"

    def pto_card(self) -> Card:
        """Unsaved, empty card for PTO (from the [pto] config section)."""
        if not self.cfg.pto:
            raise ApiError("Set the [pto] category/task in config.toml (see `tc discover`).")
        task_id, label = self._resolve(self.cfg.pto)
        return Card(sys_id=None, task_id=task_id, task_label=label, category=self.cfg.pto.category,
                    fields=dict(self.cfg.pto.fields), dirty=True)

    def apply_pto(self, cards: list[Card], days: dict[str, float | None]) -> list[str]:
        """Put PTO on the given days (in place). Idempotent.

        Full day (None): PTO card gets [pto].hours, every other card goes to 0.
        Partial (hours): PTO card gets those hours, taken from the other cards, biggest first.
        """
        if not days:
            return []
        tmpl = self.pto_card()
        pto = next((c for c in cards if not c.deleted and c.key == tmpl.key), None)
        if pto is None:
            pto = tmpl
            cards.append(pto)
        if not pto.editable:
            raise ApiError(f"PTO card is {pto.state}; can't change it.")
        others = [c for c in cards if c is not pto and not c.deleted and c.editable]
        label = tmpl.task_label or tmpl.category
        log = []
        for day, hours in days.items():
            if hours is None:
                for c in others:
                    if c.hours[day]:
                        c.hours[day], c.dirty = 0.0, True
                hours = self.cfg.pto.hours["day"]
                note = "others 0"
            else:
                # Only move the difference, so re-applying the same PTO changes nothing.
                delta = hours - pto.hours[day]
                for c in sorted(others, key=lambda c: c.hours[day], reverse=True):
                    take = min(delta, c.hours[day]) if delta > 0 else delta
                    if take:
                        c.hours[day], c.dirty = c.hours[day] - take, True
                        delta -= take
                    if not delta:
                        break
                note = "taken from other cards"
            if pto.hours[day] != hours:
                pto.hours[day], pto.dirty = hours, True
            log.append(f"pto      {day.title()}: {hours:g}h {label}, {note}")
        return log

    def pto_days(self, week: date, dates: dict[date, float | None]) -> dict[str, float | None]:
        """{day name: hours} for the PTO dates that fall in the given week."""
        return {day_name(d): h for d, h in dates.items() if week <= d < week + timedelta(days=7)}

    def save_cards(self, week: date, cards: list[Card]) -> None:
        """Push creates / updates / deletes for a week's cards."""
        for c in cards:
            if c.deleted and c.sys_id:
                self.delete_card(c.sys_id)
            elif not c.deleted and c.sys_id is None and c.total:
                self.create_card(week, c)
            elif not c.deleted and c.sys_id and c.dirty:
                self.update_card(c)

    def plan_week(self, week: date, overwrite: bool = False,
                  pto_dates: dict[date, float | None] | None = None,
                  changes: list | None = None, use_defaults: bool = False) -> tuple[list[Card], list[str]]:
        """The week's cards with missing defaults added, then PTO, then per-day changes. Nothing is saved."""
        cards = self.week_cards(week)
        existing = {c.key: c for c in cards}
        log = []
        defaults, source = self.source_cards(week, use_defaults)
        log.append(f"source   {source}")
        for card in defaults:
            key = card.key
            label = f"{card.task_label or '(no task)'} / {card.category}"
            cur = existing.get(key)
            if cur is None:
                cards.append(card)
                existing[key] = card
                log.append(f"create   {label}")
            elif overwrite and cur.editable and cur.sys_id:
                cur.hours, cur.dirty = card.hours, True
                log.append(f"update   {label}")
            elif cur.sys_id:
                log.append(f"exists   {label} [{cur.state}]")
            else:
                raise ApiError(f"{label} is listed twice ({source}); remove one.")
        log += self.apply_pto(cards, self.pto_days(week, pto_dates or {}))
        log += self.apply_changes(cards, changes or [], {c.key: c.hours for c in defaults})
        return cards, log

    def apply_changes(self, cards: list[Card], changes: list, default_hours: dict[tuple, dict]) -> list[str]:
        """Apply overrides.Override items (in place)."""
        log = []
        for ch in changes:
            if ch.target == "pto":
                log += self.apply_pto(cards, {ch.day: ch.hours})
                continue
            card = self._match(cards, ch.target)
            if not card.editable:
                raise ApiError(f"{self._label(card)} is {card.state}; can't change it.")
            base = default_hours.get(card.key, card.hours)[ch.day] if ch.relative else 0.0
            new = max(0.0, base + ch.hours)
            if card.hours[ch.day] != new:
                card.hours[ch.day], card.dirty = new, True
            log.append(f"change   {ch.day[:3].title()} {self._label(card)}: {new:g}h")
        return log

    @staticmethod
    def _label(c: Card) -> str:
        return "/".join([c.task_label or c.category, *(v for v in c.fields.values() if v)])

    def _match(self, cards: list[Card], word: str) -> Card:
        """The one card that `word` identifies (category, field value, or task number/title)."""
        live = [c for c in cards if not c.deleted]
        exact = [c for c in live if word == c.category.lower() or word in (v.lower() for v in c.fields.values())]
        found = exact or [c for c in live if c.task_label and word in c.task_label.lower()]
        if len(found) == 1:
            return found[0]
        names = ", ".join(self._label(c) for c in (found or live))
        if not found:
            raise ApiError(f"No card matches {word!r}. Cards: {names}")
        raise ApiError(f"{word!r} matches several cards ({names}); use a more specific word.")

    def fill_week(self, week: date, overwrite: bool = False,
                  pto_dates: dict[date, float | None] | None = None) -> list[str]:
        """plan_week, then save. Idempotent."""
        cards, log = self.plan_week(week, overwrite, pto_dates)
        self.save_cards(week, cards)
        return log
