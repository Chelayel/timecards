"""A small in-memory ServiceNow time_card table behind the real ServiceNow client."""

from __future__ import annotations

import uuid

import pytest

from timecards import api
from timecards.config import DAYS, Config, DefaultRow

TASK = "a" * 32
TITLE = "Build or Configure Solution"


def _match(rec: dict, cond: str) -> bool:
    for op in ("!=", ">=", "<=", "<", ">", "="):
        if op in cond:
            field, value = cond.split(op, 1)
            have = str(rec.get(field, ""))
            return {"!=": have != value, ">=": have >= value, "<=": have <= value,
                    "<": have < value, ">": have > value, "=": have == value}[op]
    return True


class FakeServiceNow(api.ServiceNow):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._session = {"user": {"sys_id": "me"}}
        self.db: dict[str, dict] = {}

    def _client(self):
        return None

    def add(self, week: str, category: str, hours: dict, task: str = "", state: str = "Frozen", **fields) -> str:
        sid = uuid.uuid4().hex
        self.db[sid] = {"sys_id": sid, "user": "me", "week_starts_on": week, "task": task, "category": category,
                        "state": state, "time_sheet": "", **{d: str(hours.get(d, 0)) for d in DAYS}, **fields}
        return sid

    def _out(self, r: dict) -> dict:
        out = {k: {"value": v, "display_value": v} for k, v in r.items()}
        out["task"] = {"value": r["task"], "display_value": TITLE if r["task"] == TASK else ""}
        return out

    def request(self, method, path, params=None, json=None):
        if path == "/api/now/table/task":
            raise api.ApiError("403 no read on task")
        sid = path.rsplit("/", 1)[1] if path.count("/") > 4 else None
        if method == "GET":
            conds = [c for c in params["sysparm_query"].split("^") if not c.startswith("ORDERBY")]
            rows = [r for r in self.db.values() if all(_match(r, c) for c in conds)]
            if "ORDERBYDESCweek_starts_on" in params["sysparm_query"]:
                rows.sort(key=lambda r: r["week_starts_on"], reverse=True)
            return {"result": [self._out(r) for r in rows]}
        if method == "POST":
            new = uuid.uuid4().hex
            self.db[new] = {"time_sheet": "", **json, "sys_id": new, "state": "Active"}
            return {"result": self._out(self.db[new])}
        if method == "PATCH":
            self.db[sid].update(json)
            return {"result": self._out(self.db[sid])}
        if method == "DELETE":
            self.db.pop(sid)
            return {}


WEEKDAYS = DAYS[1:6]


def week_hours(sn, week, key=lambda c: c.fields.get("u_subcategory") or c.category):
    return {key(c): c.hours for c in sn.week_cards(week)}


@pytest.fixture
def cfg():
    return Config("https://x.service-now.com", "/sp", "monday", "", rows=[
        DefaultRow(TITLE, "task_work", {d: 7 for d in WEEKDAYS}),
        DefaultRow("", "admin", {d: 1 for d in WEEKDAYS}, fields={"u_subcategory": "general"}),
    ], pto=DefaultRow("", "admin", {"day": 8}, fields={"u_subcategory": "pto"}))


@pytest.fixture
def sn(cfg):
    s = FakeServiceNow(cfg)
    s.add("2026-09-14", "task_work", {d: 7 for d in WEEKDAYS}, task=TASK)  # history so the title resolves
    return s
