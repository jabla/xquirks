"""The link the button of a device check's last page opens: a one-time address of this Home Assistant that sends the
browser on to GitHub (OPEN_URL). The device check learns from it that the tab opened: the view ends the check's external
step, the dialog then shows the end page (an abort with our text and Close). The address is built from the browser's own
base (the frontend sends it as HA-Frontend-Base with every subentry flow request). Without it the button opens GitHub
directly, as before: the dialog then stays on the external step until its X.

Each address works once, only while its device check lives and for an hour at most (TTL): a page whose browser blocked
the tab keeps a working Open website. Its target is stored here, never taken from the request: the view can only send a
browser to the GitHub link the check built. The tab gets a page that goes on to it at once (PAGE), not a redirect: a
reverse proxy reads the response head into a small buffer (nginx: 4 KB by default) and a prefilled form can be longer."""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any, Final

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.http import current_request
from homeassistant.util.hass_dict import HassKey
from yarl import URL

_LOGGER = logging.getLogger(__name__)

OPEN_URL = "/api/xquirks/open/{token}"
# How long an address works, in seconds, and how many wait at most (the oldest goes first).
TTL = 3600.0
MAX_LINKS = 50
# The header that names the browser's base.
BASE_HEADER = "HA-Frontend-Base"
# The page the tab gets: on to the target at once, a link in case the browser does not follow (url: HTML-escaped).
PAGE = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<meta http-equiv="refresh" content="0;url={url}"><title>GitHub</title></head>'
        '<body><p><a href="{url}">Continue to GitHub</a></p></body></html>')
# What the view hands the device check: no request can send it (the frontend's input is a dict from JSON).
OPENED: Final[Any] = object()
# A host the browser can open as it is: a name or an IPv4 address (punycode for other names), or an IPv6 address.
HOST = re.compile(r"[A-Za-z0-9._-]+|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")


class Links:
    """The addresses that wait for their tab: token -> (flow id of the device check, GitHub link, expiry)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.waiting: dict[str, tuple[str, str, float]] = {}

    def add(self, flow_id: str, target: str) -> str:
        now = self.clock()
        self.waiting = {token: found for token, found in self.waiting.items() if found[2] > now}
        while len(self.waiting) >= MAX_LINKS:
            self.waiting.pop(next(iter(self.waiting)))
        token = secrets.token_urlsafe(32)
        self.waiting[token] = (flow_id, target, now + TTL)
        return token

    def drop(self, flow_id: str) -> None:
        """The addresses of a device check that ended (its dialog closed, or it went on): they work no more."""
        self.waiting = {token: found for token, found in self.waiting.items() if found[0] != flow_id}

    def take(self, token: str) -> tuple[str, str] | None:
        """The flow id and target of an address that still works; it works no more. None otherwise."""
        found = self.waiting.pop(token, None)
        if found is None or found[2] <= self.clock():
            return None
        return found[0], found[1]


class OpenView(HomeAssistantView):
    """A one-time address: ends the device check's external step (once), then a page on to its GitHub link. Without a login:
    a new tab of the browser opens it. An address that was used, expired or never existed gets a short text."""

    url = OPEN_URL
    name = "api:xquirks:open"
    requires_auth = False

    def __init__(self, links: Links) -> None:
        self.links = links

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        if (found := self.links.take(token)) is None:
            return web.Response(status=404, text="This link was used already or has expired. Go back to Home Assistant.",
                                headers={"Cache-Control": "no-store"})
        flow_id, target = found
        hass = request.app[KEY_HASS]
        try:
            await hass.config_entries.subentries.async_configure(flow_id, OPENED)
        except UnknownFlow:
            # The dialog was closed meanwhile (or Home Assistant restarted between): the tab still gets its form.
            pass
        except Exception:  # noqa: BLE001 - the tab gets its form whatever happened to the dialog
            _LOGGER.warning("The device check %s did not end", flow_id, exc_info=True)
        return web.Response(text=PAGE.format(url=html.escape(target, quote=True)), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})


DATA_LINKS: HassKey[Links] = HassKey("xquirks_links")


@callback
def frontend_base() -> str | None:
    """The base address of the browser that sent the current request (scheme, host and port), None without a request or
    a usable header. Only http and https, nothing but an origin. Not Origin: a client on another site would get an address
    there."""
    if (request := current_request.get()) is None or not (value := request.headers.get(BASE_HEADER)):
        return None
    return _origin(value)


def _origin(value: str) -> str | None:
    try:
        url = URL(value)
    except (TypeError, ValueError):
        return None
    if (url.scheme not in ("http", "https") or not url.raw_host or not HOST.fullmatch(url.raw_host) or url.user is not None
            or url.password is not None or url.path not in ("", "/") or url.query_string or url.fragment):
        return None
    try:
        return str(url.origin())
    except ValueError:
        return None


@callback
def async_drop(hass: HomeAssistant, flow_id: str) -> None:
    """The addresses of the device check flow_id work no more (the flow is removed)."""
    if (store := hass.data.get(DATA_LINKS)) is not None:
        store.drop(flow_id)


@callback
def async_link(hass: HomeAssistant, flow_id: str, target: str) -> str:
    """The address the button opens: a one-time address of this Home Assistant for target, or target itself when the
    request names no usable base. The first call registers the view."""
    if (base := frontend_base()) is None:
        return target
    if (links := hass.data.get(DATA_LINKS)) is None:
        links = hass.data[DATA_LINKS] = Links()
        hass.http.register_view(OpenView(links))
    return base + OPEN_URL.format(token=links.add(flow_id, target))
