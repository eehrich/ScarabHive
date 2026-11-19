"""An endpoint's JSON answer without FastAPI's ``jsonable_encoder`` walk over data that is JSON already.

FastAPI hands what an endpoint returns to ``jsonable_encoder`` -- a pure-Python walk over every value -- and then
to ``json.dumps``. For a session with its whole conversation, which the chat reads again while a run goes on, the
walk cost more than the encoding: measured under load (50 agents, 50 viewers), it held the GIL for about 5 % of the
API's Python time. A record read from a JSON file is JSON already.

``json_answer`` encodes such data with ``json.dumps`` directly, with the very arguments Starlette's
``JSONResponse.render`` uses -- for plain JSON data the bytes are FastAPI's own, with one exception: a dict key
starting with ``_sa`` stays. ``jsonable_encoder`` drops those silently, a guard for SQLAlchemy state that plain
JSON never holds, so a key like ``_saved`` vanished from the answer before. Anything ``json.dumps`` does not take
(a datetime, a model, bytes, a NaN) goes the way it went before: ``jsonable_encoder``, then ``JSONResponse``.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


def json_answer(content: Any, status_code: int = 200) -> Response:
    """``content`` as the JSON response FastAPI would make of it."""
    try:
        body = json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"))
    except (TypeError, ValueError):
        return JSONResponse(jsonable_encoder(content), status_code=status_code)
    return Response(body.encode("utf-8"), status_code=status_code, media_type="application/json")
