import json
from typing import Any

__all__ = ["safe_serialize"]

def safe_serialize(obj: Any) -> str:
    """Serialize arbitrary Python objects to JSON safely.

    Strategy:
    - First attempt direct json.dumps
    - On failure, recursively convert unsupported types (dict, list, primitives retained)
    - Fallback emits minimal error JSON if conversion still fails
    """
    def _convert(o):
        if isinstance(o, (str, int, float, bool)) or o is None:
            return o
        if isinstance(o, dict):
            return {str(k): _convert(v) for k, v in o.items()}
        if isinstance(o, (list, tuple, set)):
            return [_convert(v) for v in o]
        return f"<{type(o).__name__}>"

    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps(_convert(obj), ensure_ascii=False)
        except Exception:
            return json.dumps({"error": f"Unserializable object of type {type(obj).__name__}"}, ensure_ascii=False)
