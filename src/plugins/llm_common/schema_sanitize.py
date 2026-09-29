"""JSON-schema sanitization for Gemini-style function declarations.

Lives in llm_common because two providers speak this dialect: llm_gemini
(SDK/HTTP/Batch clients) and llm_openai_compat (OpenRouter routes Gemini
models behind an OpenAI-shaped API, so its clients sanitize tool schemas
the same way before sending).
"""

from __future__ import annotations

from typing import Any, Dict


def sanitize_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Remove JSON Schema keywords not supported by Gemini API and preserve
    their semantics by appending constraint info to the description field.

    This is the canonical schema sanitization function used by all Gemini clients
    (SDK, HTTP, and Batch). It recursively processes schemas to remove unsupported
    constructs while preserving essential type information.

    Gemini's Function Declaration schema only supports:
    - type, description, enum, properties, required, items, nullable

    Unsupported validation keywords (minimum, maximum, default, minLength, etc.)
    are stripped but their values are appended to the description so the LLM
    still knows about the constraints.

    Args:
        schema: JSON schema dict to sanitize

    Returns:
        Sanitized schema dict safe for Gemini API
    """
    if not isinstance(schema, dict):
        return schema

    # Keywords that Gemini doesn't support at all
    unsupported_keywords = {
        # Composition keywords
        "oneOf", "anyOf", "allOf", "$ref",
        # Meta keywords
        "$schema", "$id", "definitions", "$defs",
        "format", "title", "examples",
        # Object constraints
        "additionalProperties", "patternProperties", "unevaluatedProperties",
        # Default values
        "default",
        # Conditional schemas
        "if", "then", "else", "not",
        # Numeric constraints (NOT supported by Gemini)
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        # String constraints (NOT supported by Gemini)
        "minLength", "maxLength", "pattern", "contentMediaType", "contentEncoding",
        # Array constraints (NOT supported by Gemini)
        "minItems", "maxItems", "uniqueItems",
        # Other unsupported
        "const", "readOnly", "writeOnly", "deprecated",
    }

    # Keywords whose values should be appended to description when stripped
    _CONSTRAINT_LABELS: Dict[str, str] = {
        "minimum": "min",
        "maximum": "max",
        "exclusiveMinimum": "exclusive min",
        "exclusiveMaximum": "exclusive max",
        "minLength": "min length",
        "maxLength": "max length",
        "minItems": "min items",
        "maxItems": "max items",
        "default": "default",
        "const": "must be",
        "pattern": "pattern",
        "multipleOf": "multiple of",
        "uniqueItems": "unique items",
    }

    # Collect constraint annotations to append to description
    constraints: list[str] = []
    for kw, label in _CONSTRAINT_LABELS.items():
        if kw in schema:
            val = schema[kw]
            # uniqueItems is boolean
            if kw == "uniqueItems" and val is True:
                constraints.append(label)
            elif kw != "uniqueItems":
                constraints.append(f"{label}: {val}")

    result: Dict[str, Any] = {}

    for key, value in schema.items():
        # Skip completely unsupported keywords
        if key in unsupported_keywords:
            # For oneOf/anyOf/allOf, merge first option into result
            # This preserves 'type' and other essential fields
            if key in ("oneOf", "anyOf", "allOf"):
                if isinstance(value, list) and len(value) > 0:
                    first_option = value[0]
                    if isinstance(first_option, dict):
                        for opt_key, opt_val in first_option.items():
                            if opt_key not in result and opt_key not in unsupported_keywords:
                                result[opt_key] = sanitize_schema_for_gemini(opt_val)
            continue
        elif key == "properties" and isinstance(value, dict):
            # Recursively sanitize each property
            result[key] = {
                k: sanitize_schema_for_gemini(v)
                for k, v in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            # Recursively sanitize array items schema
            result[key] = sanitize_schema_for_gemini(value)
        else:
            result[key] = value

    # Append constraint info to description
    if constraints:
        constraint_str = ", ".join(constraints)
        existing = result.get("description", "")
        if existing:
            # Avoid duplicating if constraints already mentioned
            result["description"] = f"{existing} ({constraint_str})"
        else:
            result["description"] = f"({constraint_str})"

    return result
