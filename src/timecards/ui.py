"""Run ServiceNow list UI actions (e.g. "Submit for Approval") in a headless browser.

UI actions are server scripts behind the classic list's "Actions on selected rows"
menu; they aren't exposed over REST, and setting `state` directly would skip their
logic (approvals, notifications). So we load the list with the saved session
cookies, tick exactly the expected rows, and pick the action.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from . import auth
from .config import Config


class UIActionError(Exception):
    pass


_FIND_ACTION_JS = """
label => {
  for (const s of document.querySelectorAll('select')) {
    const o = [...s.options].find(o => o.text.trim() === label);
    if (o) return { select: s.id, value: o.value };
  }
  return null;
}
"""

_CHECKED_JS = """
prefix => [...document.querySelectorAll('tr.list_row input[type=checkbox]')]
        .filter(c => c.checked).map(c => c.id.replace(prefix, ''))
"""


def run_list_action(cfg: Config, table: str, query: str, expected_ids: set[str], label: str,
                    dry_run: bool = False) -> list[str]:
    """Select exactly `expected_ids` in the `table` list filtered by `query` and run `label`.

    Returns any messages ServiceNow showed after the action.
    """
    from playwright.sync_api import sync_playwright

    session = auth.load_session()
    if not session:
        raise auth.NeedLogin("Not logged in. Run `tc login`.")
    host = urlparse(cfg.instance).hostname
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context()
            ctx.add_cookies([{"name": k, "value": v, "domain": host, "path": "/", "secure": True}
                             for k, v in session["cookies"].items()])
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())  # "Are you sure?" confirmations
            page.goto(f"{cfg.instance}/{table}_list.do?sysparm_query={quote(query)}", wait_until="networkidle")
            if urlparse(page.url).hostname != host:
                raise auth.NeedLogin("ServiceNow session expired. Run `tc login`.")

            rows = set(page.eval_on_selector_all(
                "tr.list_row input[type=checkbox]", f"els => els.map(e => e.id.replace('check_{table}_', ''))"))
            if rows != expected_ids:
                raise UIActionError(f"List shows {len(rows)} row(s), expected {len(expected_ids)}; not submitting.")
            action = page.evaluate(_FIND_ACTION_JS, label)
            if not action:
                raise UIActionError(f"'{label}' isn't available for these cards.")

            for sys_id in expected_ids:
                page.locator(f"#check_{table}_{sys_id}").check(force=True)
            if set(page.evaluate(_CHECKED_JS, f"check_{table}_")) != expected_ids:
                raise UIActionError("Couldn't select exactly the expected rows; not submitting.")

            if dry_run:
                return [f"Dry run: {len(expected_ids)} row(s) selected and '{label}' is available; not run."]
            page.select_option(f"#{action['select']}", value=action["value"])
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            return [m.strip() for m in page.eval_on_selector_all(
                ".outputmsg_text, .notification-message, .outputmsg_error", "els => els.map(e => e.innerText)") if m.strip()]
        finally:
            browser.close()
