from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


KEYWORDS = {
    "if",
    "else",
    "for",
    "while",
    "break",
    "continue",
    "return",
    "var",
    "varip",
    "const",
    "na",
    "export",
    "import",
    "method",
    "as",
    "switch",
    "case",
    "default",
    "type",
    "enum",
    "and",
    "or",
    "not",
    "int",
    "float",
    "bool",
    "string",
    "color",
    "line",
    "label",
    "box",
    "table",
    "array",
    "matrix",
    "map",
    "series",
    "simple",
    "input",
    "then",
    "to",
    "by",
    "in",
}

TYPE_NAMES = {
    "int",
    "float",
    "bool",
    "string",
    "color",
    "array",
    "matrix",
    "map",
    "line",
    "label",
    "box",
    "table",
    "polyline",
    "linefill",
    "footprint",
    "volume_row",
    "series",
    "simple",
    "const",
}

OVERLOAD_SUFFIX_RE = re.compile(r"-\d+$")
ADDITIONAL_CONSTANT_NAMESPACES = {
    "adjustment",
    "alert",
    "backadjustment",
    "barmerge",
    "barstate",
    "color",
    "currency",
    "dayofweek",
    "display",
    "dividends",
    "earnings",
    "extend",
    "font",
    "format",
    "hline",
    "label",
    "line",
    "location",
    "math",
    "order",
    "plot",
    "position",
    "scale",
    "session",
    "settlement_as_close",
    "shape",
    "size",
    "splits",
    "strategy",
    "table",
    "text",
    "footprint",
    "volume_row",
    "xloc",
    "yloc",
}

SUPPLEMENTAL_NAMESPACE_MEMBERS = {
    "request": {"footprint"},
    "footprint": {
        "buy_volume",
        "sell_volume",
        "total_volume",
        "delta",
        "poc",
        "vah",
        "val",
        "get_row_by_price",
        "rows",
    },
    "volume_row": {
        "up_price",
        "down_price",
        "buy_volume",
        "sell_volume",
        "total_volume",
        "delta",
        "has_buy_imbalance",
        "has_sell_imbalance",
    },
}


@dataclass(slots=True, frozen=True)
class FunctionOverloadSpec:
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...]
    signature: str
    variadic: bool = False


@dataclass(slots=True, frozen=True)
class FunctionSpec:
    name: str
    syntax: str | None
    description: str | None
    overloads: tuple[FunctionOverloadSpec, ...]


@dataclass(slots=True, frozen=True)
class BuiltinData:
    variable_paths: frozenset[str]
    function_paths: frozenset[str]
    standalone_variables: frozenset[str]
    standalone_functions: frozenset[str]
    known_namespaces: frozenset[str]
    namespace_members: dict[str, frozenset[str]]


PYTHON_OVERRIDE_SPECS = {
    "array.new_linefill": {
        "name": "array.new_linefill",
        "syntax": "array.new_linefill(size?, initial_value?)",
        "overloads": [
            {
                "requiredParams": [],
                "optionalParams": ["size", "initial_value"],
                "signature": "array.new_linefill(size?, initial_value?)",
            }
        ],
    },
    "time": {
        "name": "time",
        "syntax": "time(timeframe?, session?, timezone?, bars_back?)",
        "overloads": [
            {
                "requiredParams": [],
                "optionalParams": ["timeframe", "session", "timezone", "bars_back"],
                "signature": "time(timeframe?, session?, timezone?, bars_back?)",
            }
        ],
    },
    "log.info": {
        "name": "log.info",
        "syntax": "log.info(message, ...)",
        "overloads": [
            {
                "requiredParams": ["message"],
                "optionalParams": [],
                "signature": "log.info(message, ...)",
            }
        ],
    },
    "timestamp": {
        "name": "timestamp",
        "syntax": "timestamp(dateString) or timestamp(year, month, day, hour?, minute?, second?) or timestamp(timezone, year, month, day, hour?, minute?, second?)",
        "overloads": [
            {
                "requiredParams": ["dateString"],
                "optionalParams": [],
                "signature": "timestamp(dateString)",
            },
            {
                "requiredParams": ["year", "month", "day"],
                "optionalParams": ["hour", "minute", "second"],
                "signature": "timestamp(year, month, day, hour?, minute?, second?)",
            },
            {
                "requiredParams": ["timezone", "year", "month", "day"],
                "optionalParams": ["hour", "minute", "second"],
                "signature": "timestamp(timezone, year, month, day, hour?, minute?, second?)",
            },
        ],
    },
    "plotcandle": {
        "name": "plotcandle",
        "syntax": "plotcandle(open, high, low, close, title?, color?, wickcolor?, editable?, show_last?, bordercolor?, display?, force_overlay?)",
        "overloads": [
            {
                "requiredParams": ["open", "high", "low", "close"],
                "optionalParams": ["title", "color", "wickcolor", "editable", "show_last", "bordercolor", "display", "force_overlay"],
                "signature": "plotcandle(open, high, low, close, title?, color?, wickcolor?, editable?, show_last?, bordercolor?, display?, force_overlay?)",
            }
        ],
    },
    "fill": {
        "name": "fill",
        "syntax": "fill(plot1, plot2, color?, title?, editable?, show_last?, fillgaps?, display?, top_value?, bottom_value?, top_color?, bottom_color?)",
        "overloads": [
            {
                "requiredParams": ["plot1", "plot2"],
                "optionalParams": [
                    "color",
                    "title",
                    "editable",
                    "show_last",
                    "fillgaps",
                    "display",
                    "top_value",
                    "bottom_value",
                    "top_color",
                    "bottom_color",
                ],
                "signature": "fill(plot1, plot2, color?, title?, editable?, show_last?, fillgaps?, display?, top_value?, bottom_value?, top_color?, bottom_color?)",
            }
        ],
    },
    "input.bool": {
        "name": "input.bool",
        "syntax": "input.bool(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.bool(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "input.color": {
        "name": "input.color",
        "syntax": "input.color(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.color(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "input.float": {
        "name": "input.float",
        "syntax": "input.float(defval, title?, minval?, maxval?, step?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "options", "minval", "maxval", "step", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.float(defval, title?, minval?, maxval?, step?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "input.int": {
        "name": "input.int",
        "syntax": "input.int(defval, title?, minval?, maxval?, step?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "options", "minval", "maxval", "step", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.int(defval, title?, minval?, maxval?, step?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "input.string": {
        "name": "input.string",
        "syntax": "input.string(defval, title?, options?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "options", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.string(defval, title?, options?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "input.time": {
        "name": "input.time",
        "syntax": "input.time(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.time(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "input.timeframe": {
        "name": "input.timeframe",
        "syntax": "input.timeframe(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
        "overloads": [
            {
                "requiredParams": ["defval"],
                "optionalParams": ["title", "tooltip", "inline", "group", "confirm", "display", "active"],
                "signature": "input.timeframe(defval, title?, tooltip?, inline?, group?, confirm?, display?, active?)",
            }
        ],
    },
    "request.footprint": {
        "name": "request.footprint",
        "syntax": "request.footprint(ticks_per_row, va_percent?, imbalance_percent?)",
        "overloads": [
            {
                "requiredParams": ["ticks_per_row"],
                "optionalParams": ["va_percent", "imbalance_percent"],
                "signature": "request.footprint(ticks_per_row, va_percent?, imbalance_percent?)",
            }
        ],
    },
    "footprint.buy_volume": {
        "name": "footprint.buy_volume",
        "syntax": "footprint.buy_volume(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.buy_volume(id)"}
        ],
    },
    "footprint.sell_volume": {
        "name": "footprint.sell_volume",
        "syntax": "footprint.sell_volume(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.sell_volume(id)"}
        ],
    },
    "footprint.total_volume": {
        "name": "footprint.total_volume",
        "syntax": "footprint.total_volume(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.total_volume(id)"}
        ],
    },
    "footprint.delta": {
        "name": "footprint.delta",
        "syntax": "footprint.delta(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.delta(id)"}
        ],
    },
    "footprint.poc": {
        "name": "footprint.poc",
        "syntax": "footprint.poc(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.poc(id)"}
        ],
    },
    "footprint.vah": {
        "name": "footprint.vah",
        "syntax": "footprint.vah(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.vah(id)"}
        ],
    },
    "footprint.val": {
        "name": "footprint.val",
        "syntax": "footprint.val(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.val(id)"}
        ],
    },
    "footprint.get_row_by_price": {
        "name": "footprint.get_row_by_price",
        "syntax": "footprint.get_row_by_price(id, price)",
        "overloads": [
            {
                "requiredParams": ["id", "price"],
                "optionalParams": [],
                "signature": "footprint.get_row_by_price(id, price)",
            }
        ],
    },
    "footprint.rows": {
        "name": "footprint.rows",
        "syntax": "footprint.rows(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "footprint.rows(id)"}
        ],
    },
    "volume_row.up_price": {
        "name": "volume_row.up_price",
        "syntax": "volume_row.up_price(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.up_price(id)"}
        ],
    },
    "volume_row.down_price": {
        "name": "volume_row.down_price",
        "syntax": "volume_row.down_price(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.down_price(id)"}
        ],
    },
    "volume_row.buy_volume": {
        "name": "volume_row.buy_volume",
        "syntax": "volume_row.buy_volume(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.buy_volume(id)"}
        ],
    },
    "volume_row.sell_volume": {
        "name": "volume_row.sell_volume",
        "syntax": "volume_row.sell_volume(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.sell_volume(id)"}
        ],
    },
    "volume_row.total_volume": {
        "name": "volume_row.total_volume",
        "syntax": "volume_row.total_volume(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.total_volume(id)"}
        ],
    },
    "volume_row.delta": {
        "name": "volume_row.delta",
        "syntax": "volume_row.delta(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.delta(id)"}
        ],
    },
    "volume_row.has_buy_imbalance": {
        "name": "volume_row.has_buy_imbalance",
        "syntax": "volume_row.has_buy_imbalance(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.has_buy_imbalance(id)"}
        ],
    },
    "volume_row.has_sell_imbalance": {
        "name": "volume_row.has_sell_imbalance",
        "syntax": "volume_row.has_sell_imbalance(id)",
        "overloads": [
            {"requiredParams": ["id"], "optionalParams": [], "signature": "volume_row.has_sell_imbalance(id)"}
        ],
    },
    "box.new": {
        "name": "box.new",
        "syntax": "box.new(top_left, bottom_right, ...) or box.new(left, top, right, bottom, ...)",
        "overloads": [
            {
                "requiredParams": ["top_left"],
                "optionalParams": [
                    "bottom_right",
                    "border_color",
                    "border_width",
                    "border_style",
                    "extend",
                    "xloc",
                    "bgcolor",
                    "text",
                    "text_size",
                    "text_color",
                    "text_halign",
                    "text_valign",
                    "text_wrap",
                    "text_font_family",
                    "force_overlay",
                    "text_formatting",
                ],
                "signature": "box.new(top_left, bottom_right, border_color, border_width, border_style, extend, xloc, bgcolor, text, text_size, text_color, text_halign, text_valign, text_wrap, text_font_family, force_overlay, text_formatting)",
            },
            {
                "requiredParams": ["left", "top", "right", "bottom"],
                "optionalParams": [
                    "border_color",
                    "border_width",
                    "border_style",
                    "extend",
                    "xloc",
                    "bgcolor",
                    "text",
                    "text_size",
                    "text_color",
                    "text_halign",
                    "text_valign",
                    "text_wrap",
                    "text_font_family",
                    "force_overlay",
                    "text_formatting",
                ],
                "signature": "box.new(left, top, right, bottom, border_color, border_width, border_style, extend, xloc, bgcolor, text, text_size, text_color, text_halign, text_valign, text_wrap, text_font_family, force_overlay, text_formatting)",
            },
        ],
    },
    "line.new": {
        "name": "line.new",
        "syntax": "line.new(first_point, second_point, ...) or line.new(x1, y1, x2, y2, ...)",
        "overloads": [
            {
                "requiredParams": ["first_point"],
                "optionalParams": ["second_point", "xloc", "extend", "color", "style", "width", "force_overlay"],
                "signature": "line.new(first_point, second_point, xloc, extend, color, style, width, force_overlay)",
            },
            {
                "requiredParams": ["x1", "y1", "x2", "y2"],
                "optionalParams": ["xloc", "extend", "color", "style", "width", "force_overlay"],
                "signature": "line.new(x1, y1, x2, y2, xloc, extend, color, style, width, force_overlay)",
            },
        ],
    },
    "label.new": {
        "name": "label.new",
        "syntax": "label.new(point, ...) or label.new(x, y, text, ...)",
        "overloads": [
            {
                "requiredParams": ["point"],
                "optionalParams": [
                    "text",
                    "xloc",
                    "yloc",
                    "color",
                    "style",
                    "textcolor",
                    "size",
                    "textalign",
                    "tooltip",
                    "text_font_family",
                    "force_overlay",
                    "text_formatting",
                ],
                "signature": "label.new(point, text, xloc, yloc, color, style, textcolor, size, textalign, tooltip, text_font_family, force_overlay, text_formatting)",
            },
            {
                "requiredParams": ["x", "y"],
                "optionalParams": [
                    "text",
                    "xloc",
                    "yloc",
                    "color",
                    "style",
                    "textcolor",
                    "size",
                    "textalign",
                    "tooltip",
                    "text_font_family",
                    "force_overlay",
                    "text_formatting",
                ],
                "signature": "label.new(x, y, text, xloc, yloc, color, style, textcolor, size, textalign, tooltip, text_font_family, force_overlay, text_formatting)",
            },
        ],
    },
}


def _load_json(filename: str) -> dict[str, object]:
    base = resources.files("pinescript_validator").joinpath("data", filename)
    with base.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _load_namespace_constants() -> dict[str, frozenset[str]]:
    raw = _load_json("namespace_constants.json")
    output: dict[str, frozenset[str]] = {}
    for namespace, members in raw.items():
        if not isinstance(namespace, str) or not isinstance(members, list):
            continue
        output[namespace] = frozenset(str(member) for member in members)
    return output


@lru_cache(maxsize=1)
def load_builtin_data() -> BuiltinData:
    raw = _load_json("generated.json")
    raw_vars = set(raw.get("vars", {}).keys())
    raw_funcs = {
        name
        for name in raw.get("funcs", {}).keys()
        if not OVERLOAD_SUFFIX_RE.search(name)
    }
    raw_funcs.update(PYTHON_OVERRIDE_SPECS.keys())

    namespace_members: dict[str, set[str]] = {}
    for full_name in raw_vars | raw_funcs:
        if "." not in full_name:
            continue
        namespace, remainder = full_name.split(".", 1)
        member = remainder.split(".", 1)[0]
        namespace_members.setdefault(namespace, set()).add(member)

    for namespace, members in _load_namespace_constants().items():
        namespace_members.setdefault(namespace, set()).update(members)

    for namespace, members in SUPPLEMENTAL_NAMESPACE_MEMBERS.items():
        namespace_members.setdefault(namespace, set()).update(members)

    for namespace in ADDITIONAL_CONSTANT_NAMESPACES:
        namespace_members.setdefault(namespace, set())

    known_namespaces = frozenset(namespace_members.keys())
    return BuiltinData(
        variable_paths=frozenset(raw_vars),
        function_paths=frozenset(raw_funcs),
        standalone_variables=frozenset(name for name in raw_vars if "." not in name),
        standalone_functions=frozenset(name for name in raw_funcs if "." not in name),
        known_namespaces=known_namespaces,
        namespace_members={key: frozenset(value) for key, value in namespace_members.items()},
    )


def _normalize_overloads(name: str, spec: dict[str, object]) -> tuple[FunctionOverloadSpec, ...]:
    overloads = spec.get("overloads")
    if isinstance(overloads, list) and overloads:
        normalized: list[FunctionOverloadSpec] = []
        for item in overloads:
            if not isinstance(item, dict):
                continue
            signature = str(item.get("signature") or spec.get("syntax") or name)
            normalized.append(
                FunctionOverloadSpec(
                    required_params=tuple(item.get("requiredParams", []) or []),
                    optional_params=tuple(item.get("optionalParams", []) or []),
                    signature=signature,
                    variadic="..." in signature,
                )
            )
        if normalized:
            return tuple(normalized)

    signature = str(spec.get("signature") or spec.get("syntax") or name)
    return (
        FunctionOverloadSpec(
            required_params=tuple(spec.get("requiredParams", []) or []),
            optional_params=tuple(spec.get("optionalParams", []) or []),
            signature=signature,
            variadic="..." in signature,
        ),
    )


@lru_cache(maxsize=1)
def load_function_specs() -> dict[str, FunctionSpec]:
    raw = _load_json("function_specs.json")
    raw.update(PYTHON_OVERRIDE_SPECS)
    specs: dict[str, FunctionSpec] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        specs[name] = FunctionSpec(
            name=str(spec.get("name") or name),
            syntax=spec.get("syntax") if isinstance(spec.get("syntax"), str) else None,
            description=spec.get("description") if isinstance(spec.get("description"), str) else None,
            overloads=_normalize_overloads(name, spec),
        )
    return specs
