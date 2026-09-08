"""Render the report as a fullscreen "Slopmeter" dashboard.

The HTML/CSS/JS lives in ``dashboard.html`` next to this module; this file
only embeds the JSON payload and the drawn icon (used as logo and favicon).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote

_TEMPLATE = Path(__file__).with_name("dashboard.html")

# A green slop blob wearing a gauge: the "Slopmeter" mascot.
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    "<defs>"
    '<linearGradient id="sm-body" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#7ed957"/><stop offset="1" stop-color="#2f9e44"/>'
    "</linearGradient>"
    '<linearGradient id="sm-dial" x1="0" y1="1" x2="1" y2="0">'
    '<stop offset="0" stop-color="#c9e34a"/><stop offset="0.55" stop-color="#f0b429"/><stop offset="1" stop-color="#ef476f"/>'
    "</linearGradient>"
    "</defs>"
    # dripping blob body
    '<path d="M32 4c14 0 26 9 26 22 0 7-3 11-3 16 0 4 3 7 3 11 0 4-3 6-6 6-4 0-5-5-5-8 0-3-2-4-3-4-2 0-3 3-3 7 0 4-2 7-6 7s-6-3-6-7c0-3-1-5-3-5s-3 3-3 6c0 4-2 8-6 8s-6-3-6-6c0-5 3-8 3-13C14 26 6 22 6 22 6 11 18 4 32 4z" '
    'fill="url(#sm-body)" stroke="#1d5c2a" stroke-width="2" stroke-linejoin="round"/>'
    # gauge face
    '<circle cx="32" cy="27" r="15" fill="#0f1114" stroke="#1d5c2a" stroke-width="2"/>'
    '<path d="M21 32 A12 12 0 0 1 43 32" fill="none" stroke="url(#sm-dial)" stroke-width="4.5" stroke-linecap="round"/>'
    '<line x1="32" y1="31" x2="39" y2="22" stroke="#e7ebe4" stroke-width="2.5" stroke-linecap="round"/>'
    '<circle cx="32" cy="31" r="2.5" fill="#e7ebe4"/>'
    # eyes
    '<circle cx="24" cy="15" r="2" fill="#0f1114"/><circle cx="40" cy="15" r="2" fill="#0f1114"/>'
    "</svg>"
)


def favicon_uri() -> str:
    return "data:image/svg+xml," + quote(ICON_SVG)


def render_html(repos: List[Dict], default_days: int = 30, generated: str = "") -> str:
    """*repos*: [{slug, name, active, envs}, ...] — several repos share one dashboard."""
    payload = {
        "defaultDays": default_days,
        "generated": generated,
        "repos": repos,
        "active": repos[0]["slug"],
    }
    title = repos[0]["name"] if len(repos) == 1 else f"{len(repos)} repos"
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    tpl = _TEMPLATE.read_text(encoding="utf-8")
    return (
        tpl.replace("__DATA__", data_json)
        .replace("__REPO__", title)
        .replace("__FAVICON__", favicon_uri())
        .replace("__ICON__", ICON_SVG)
    )
