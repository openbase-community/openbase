"""Branded pages the CLI's loopback login callback serves to the browser.

After `openbase-coder login` (also run by the desktop app's onboarding), the
browser lands on the local callback. These pages tell the user they are signed
in and should return to whichever started the login, the Openbase app or their
terminal, matching the cloud
web's sign-in look so the hand-off never reads like a paywall.
"""

from __future__ import annotations

import html
import importlib.resources
import json
from string import Template

_RESOURCES = importlib.resources.files("openbase_coder_cli.resources.login")

_CHECK_ICON = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>'
)
_RETRY_ICON = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>'
)


def _read(name: str) -> str:
    return (_RESOURCES / name).read_text(encoding="utf-8")


def _render(
    *,
    page_title: str,
    icon_svg: str,
    eyebrow: str,
    heading: str,
    subtitle: str,
    card_html: str,
    close_note: str,
    script_html: str = "",
) -> bytes:
    page = Template(_read("login-page.html")).substitute(
        page_title=html.escape(page_title),
        logo_svg=_read("openbase-logo-and-text.svg"),
        icon_svg=icon_svg,
        eyebrow=html.escape(eyebrow),
        heading=html.escape(heading),
        subtitle=html.escape(subtitle),
        card_html=card_html,
        close_note=html.escape(close_note),
        script_html=script_html,
    )
    return page.encode("utf-8")


def login_complete_page(*, desktop_url: str | None) -> bytes:
    """The page shown once the CLI has received a successful login.

    ``desktop_url`` is the app deep link to reopen. It is only passed when the
    desktop app started the login; a terminal-started login (e.g. during
    ``./scripts/setup``, before the app is built) has no ``openbase://`` handler
    registered, so the page just sends the user back to their terminal.
    """
    if desktop_url is None:
        return _render(
            page_title="Signed in to Openbase",
            icon_svg=_CHECK_ICON,
            eyebrow="Signed in",
            heading="You're signed in.",
            subtitle="Return to your terminal to keep going.",
            card_html="""
          <ul>
            <li><span class="step">1</span><span>Head back to your <strong>terminal</strong>. Setup continues there.</span></li>
          </ul>""",
            close_note="You can close this tab.",
        )

    escaped_desktop_url = html.escape(desktop_url, quote=True)
    card_html = f"""
          <ul>
            <li><span class="step">1</span><span>The <strong>Openbase app</strong> reopens on its own. If it doesn't, use the button below.</span></li>
          </ul>
          <a class="button" href="{escaped_desktop_url}">Open the Openbase app</a>"""
    script_html = f"""<script>
      window.setTimeout(function () {{
        window.location.href = {json.dumps(desktop_url)};
      }}, 250);
    </script>"""
    return _render(
        page_title="Signed in to Openbase",
        icon_svg=_CHECK_ICON,
        eyebrow="Signed in",
        heading="You're signed in.",
        subtitle="Return to the Openbase app to keep going.",
        card_html=card_html,
        close_note="You can close this tab.",
        script_html=script_html,
    )


def stale_login_page() -> bytes:
    """The page shown when an older login tab completes after a newer one began."""
    card_html = """
          <ul>
            <li><span class="step">1</span><span>A newer Openbase sign-in was started after this one.</span></li>
            <li><span class="step">2</span><span>Finish signing in from the <strong>newest</strong> Openbase sign-in tab.</span></li>
          </ul>"""
    return _render(
        page_title="Older Openbase sign-in ignored",
        icon_svg=_RETRY_ICON,
        eyebrow="Older sign-in",
        heading="Use the newest sign-in tab.",
        subtitle="This sign-in was replaced by a newer one, so it wasn't used.",
        card_html=card_html,
        close_note="You can close this tab.",
    )
