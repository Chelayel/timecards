"""Textual TUI: edit a week's time cards in a grid, then save/submit to ServiceNow."""

from __future__ import annotations

from datetime import date, timedelta

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from .api import Card, ServiceNow
from .config import Config, load_pto

FIXED_COLS = ["Task", "Category"]


class Prompt(ModalScreen[str | None]):
    DEFAULT_CSS = """
    Prompt { align: center middle; }
    Prompt > Vertical { width: 60; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, value: str = ""):
        super().__init__()
        self.prompt, self.value = prompt, value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(value=self.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TimecardApp(App):
    TITLE = "Time cards"
    CSS = "#info { padding: 0 1; height: 2; } DataTable { height: 1fr; }"
    BINDINGS = [
        Binding("e", "edit", "Edit cell (or Enter)"),
        Binding("a", "add", "Add row"),
        Binding("x", "delete", "Delete row"),
        Binding("f", "fill", "Fill defaults"),
        Binding("p", "pto", "PTO day"),
        Binding("s", "save", "Save"),
        Binding("S", "submit", "Submit week"),
        Binding("[", "week(-1)", "Prev week"),
        Binding("]", "week(1)", "Next week"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config, week: date):
        super().__init__()
        self.cfg, self.week = cfg, week
        self.sn = ServiceNow(cfg)
        self.cards: list[Card] = []

    # -- layout / rendering ------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="info")
        yield DataTable(id="grid", cursor_type="cell", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        grid = self.query_one(DataTable)
        grid.add_columns(*FIXED_COLS, *[d[:3].title() for d in self.cfg.week_days], "Total", "State")
        self.load()

    @property
    def visible(self) -> list[Card]:
        return [c for c in self.cards if not c.deleted]

    @property
    def dirty(self) -> bool:
        return any(c.dirty or c.deleted for c in self.cards)

    def render_grid(self) -> None:
        grid = self.query_one(DataTable)
        pos = grid.cursor_coordinate
        grid.clear()
        for c in self.visible:
            mark = "*" if c.dirty else ""
            grid.add_row(mark + (c.task_label or "-"), c.category,
                         *[f"{c.hours[d]:g}" for d in self.cfg.week_days], f"{c.total:g}", c.state or "new")
        if self.visible:
            grid.cursor_coordinate = grid.validate_cursor_coordinate(pos)
            if self.screen is self.screen_stack[0]:
                grid.focus()  # an empty table can't take focus, so grab it once rows exist
        totals = "  ".join(f"{d[:3].title()} {sum(c.hours[d] for c in self.visible):g}" for d in self.cfg.week_days)
        status = "[yellow]unsaved changes[/]" if self.dirty else "[green]saved[/]"
        self.query_one("#info", Static).update(
            f"[b]Week of {self.week:%a %b %d, %Y}[/b]   total [b]{sum(c.total for c in self.visible):g}h[/b]   {status}\n{totals}"
        )

    def fail(self, e: Exception) -> None:
        self.notify(str(e), severity="error", timeout=10)

    # -- background calls --------------------------------------------------

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        self.call_from_thread(self.notify, "Loading...", timeout=1)
        try:
            cards = self.sn.week_cards(self.week)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.cards = cards
        self.call_from_thread(self.render_grid)

    @work(thread=True, exclusive=True)
    def action_fill(self) -> None:
        try:
            defaults, _ = self.sn.source_cards(self.week)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        existing = {c.key: c for c in self.visible}
        for d in defaults:
            cur = existing.get(d.key)
            if cur is None:
                self.cards.append(d)
            elif cur.editable and cur.total == 0:
                cur.hours, cur.dirty = d.hours, True
        try:
            self.sn.apply_pto(self.cards, self.sn.pto_days(self.week, load_pto()))
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.render_grid)
        self.call_from_thread(self.notify, "Filled (copied from the week before, or defaults). Press s to save.")

    @work(thread=True, exclusive=True)
    def action_save(self) -> None:
        try:
            self.sn.save_cards(self.week, self.cards)
            self.cards = self.sn.week_cards(self.week)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.render_grid)
        self.call_from_thread(self.notify, "Saved to ServiceNow.")

    @work(thread=True, exclusive=True)
    def do_submit(self) -> None:
        try:
            msg = self.sn.submit_week(self.week)
            self.cards = self.sn.week_cards(self.week)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.render_grid)
        self.call_from_thread(self.notify, msg)

    @work(thread=True, exclusive=True)
    def do_pto(self, day: str, hours: float | None) -> None:
        try:
            self.sn.apply_pto(self.cards, {day: hours})
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.render_grid)
        self.call_from_thread(self.notify, f"{day.title()} set to PTO. Press s to save.")

    @work(thread=True)
    def set_task(self, card: Card, number: str) -> None:
        try:
            card.task_id, card.task_label = self.sn.find_task(number) if number else ("", "")
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        card.dirty = True
        self.call_from_thread(self.render_grid)

    # -- actions -----------------------------------------------------------

    def current(self) -> tuple[Card, int] | None:
        grid = self.query_one(DataTable)
        if not self.visible:
            return None
        return self.visible[grid.cursor_row], grid.cursor_column

    def on_data_table_cell_selected(self, _: DataTable.CellSelected) -> None:
        self.action_edit()

    def action_edit(self) -> None:
        cur = self.current()
        if not cur:
            return
        card, col = cur
        if not card.editable:
            self.notify(f"Card is {card.state}; can't edit.", severity="warning")
            return
        days = self.cfg.week_days
        if col == 0:
            self.push_screen(Prompt("Task number (blank for none):", card.task_label),
                             lambda v: v is not None and self.set_task(card, v.strip()))
        elif col == 1:
            def set_cat(v: str | None) -> None:
                if v:
                    card.category, card.dirty = v.strip(), True
                    self.render_grid()
            self.push_screen(Prompt("Category:", card.category), set_cat)
        elif 2 <= col < 2 + len(days):
            day = days[col - 2]

            def set_hours(v: str | None) -> None:
                if v is None:
                    return
                try:
                    card.hours[day] = float(v or 0)
                except ValueError:
                    self.notify(f"Not a number: {v!r}", severity="error")
                    return
                card.dirty = True
                self.render_grid()
            self.push_screen(Prompt(f"Hours for {day.title()}:", f"{card.hours[day]:g}"), set_hours)

    def action_pto(self) -> None:
        cur = self.current()
        days = self.cfg.week_days
        col = cur[1] if cur else -1
        if not 2 <= col < 2 + len(days):
            self.notify("Move the cursor to a day column, then press p.", severity="warning")
            return
        day = days[col - 2]

        def got(v: str | None) -> None:
            if v is None:
                return
            try:
                self.do_pto(day, float(v) if v.strip() else None)
            except ValueError:
                self.notify(f"Not a number: {v!r}", severity="error")
        self.push_screen(Prompt(f"PTO hours on {day.title()} (blank = full day):"), got)

    def action_add(self) -> None:
        def got_task(number: str | None) -> None:
            if number is None:
                return

            def got_cat(cat: str | None) -> None:
                if not cat:
                    return
                card = Card(sys_id=None, task_id="", task_label="", category=cat.strip(), dirty=True)
                self.cards.append(card)
                self.render_grid()
                if number.strip():
                    self.set_task(card, number.strip())
            self.push_screen(Prompt("Category (e.g. task_work, admin, meeting):", "task_work"), got_cat)
        self.push_screen(Prompt("Task number (blank for none):"), got_task)

    def action_delete(self) -> None:
        cur = self.current()
        if not cur:
            return
        card, _ = cur
        if not card.editable:
            self.notify(f"Card is {card.state}; can't delete.", severity="warning")
            return
        if card.sys_id:
            card.deleted = True
        else:
            self.cards.remove(card)
        self.render_grid()

    def action_submit(self) -> None:
        if self.dirty:
            self.notify("Save first (s).", severity="warning")
            return
        self.push_screen(Prompt("Type 'yes' to submit this week:"),
                         lambda v: v and v.strip().lower() == "yes" and self.do_submit())

    def action_week(self, delta: int) -> None:
        if self.dirty:
            self.notify("Unsaved changes: save (s) or discard with reload (r).", severity="warning")
            return
        self.week += timedelta(weeks=delta)
        self.load()

    def action_reload(self) -> None:
        self.load()
