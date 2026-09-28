"""SSO login via a real browser, then harvest the ServiceNow session for REST calls.

ServiceNow accepts REST calls authenticated by the UI session cookie as long as the
request also carries the session's CSRF token (g_ck) in the X-UserToken header.
We use Playwright with a persistent profile so the IdP session survives between
runs: `tc login` is interactive once, later refreshes run headless.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse

from .config import PROFILE_DIR, SESSION_FILE, Config


class NeedLogin(Exception):
    pass


# Runs inside the page: returns {token, user} once logged in as a real user, else null.
_PROBE_JS = """
async () => {
  const token = window.g_ck || (window.NOW && window.NOW.g_ck);
  if (!token) return null;
  const q = 'sys_id=javascript:gs.getUserID()';
  const r = await fetch('/api/now/table/sys_user?sysparm_limit=1&sysparm_fields=sys_id,user_name,name&sysparm_query=' + encodeURIComponent(q),
    { headers: { 'Accept': 'application/json', 'X-UserToken': token } });
  if (!r.ok) return null;
  const user = ((await r.json()).result || [])[0];
  if (!user || user.user_name === 'guest') return null;
  return { token, user };
}
"""


def _launch(p, cfg: Config, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kw = dict(user_data_dir=str(PROFILE_DIR), headless=headless)
    if cfg.browser_channel:
        try:
            return p.chromium.launch_persistent_context(channel=cfg.browser_channel, **kw)
        except Exception:
            pass  # channel not installed; fall back to bundled Chromium
    return p.chromium.launch_persistent_context(**kw)


def harvest(cfg: Config, interactive: bool, timeout_s: int | None = None) -> dict:
    """Open the portal, wait until authenticated, and save token + cookies."""
    from playwright.sync_api import sync_playwright

    timeout_s = timeout_s or (300 if interactive else 60)
    host = urlparse(cfg.instance).hostname
    result = None
    with sync_playwright() as p:
        ctx = _launch(p, cfg, headless=not interactive)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(cfg.instance + cfg.portal_page, wait_until="domcontentloaded")
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    if urlparse(page.url).hostname == host:
                        result = page.evaluate(_PROBE_JS)
                        if result:
                            break
                except Exception:
                    pass  # page mid-navigation during SSO redirects
                page.wait_for_timeout(1500)
            if not result:
                raise NeedLogin("Not logged in to ServiceNow. Run `tc login`.")
            cookies = {c["name"]: c["value"] for c in ctx.cookies(cfg.instance)}
        finally:
            ctx.close()

    session = {"token": result["token"], "user": result["user"], "cookies": cookies, "saved_at": time.time()}
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(session, f)
    return session


def load_session() -> dict | None:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
