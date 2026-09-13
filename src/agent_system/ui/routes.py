"""Routes of the UI itself: the kit catalogue page."""
from __future__ import annotations

import re
from functools import lru_cache

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .resources import STATIC_DIR, ui_templates

router = APIRouter(prefix="/ui", tags=["ui"])
templates = ui_templates()


@lru_cache(maxsize=1)
def sprite_icons() -> tuple[str, ...]:
    """Icon names in the kit sprite, in sprite order."""
    sprite = (STATIC_DIR / "kit" / "icons.svg").read_text(encoding="utf-8")
    return tuple(re.findall(r'<symbol id="([^"]+)"', sprite))


@router.get("/kit", response_class=HTMLResponse)
async def kit_page(request: Request):
    """Every kit component in every state -- documentation and visual check."""
    return templates.TemplateResponse(request, "kit/kit_page.html", {"icons": sprite_icons()})
