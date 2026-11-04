"""The first anyOf/oneOf option is merged as a schema, not value by value.

Sanitizing each value of the option ran the keyword filter over its
`properties` map, so parameters NAMED like keywords ("format", "title",
"pattern") vanished while `required` still listed them.
"""
from plugins.llm_common.schema_sanitize import sanitize_schema_for_gemini


def test_properties_named_like_keywords_survive_an_any_of_merge():
    schema = {"anyOf": [
        {"type": "object",
         "properties": {"format": {"type": "string"}, "title": {"type": "string"},
                        "pattern": {"type": "string", "pattern": "^a"}},
         "required": ["format", "title"]},
        {"type": "null"}]}

    out = sanitize_schema_for_gemini(schema)

    assert out["type"] == "object"
    assert set(out["properties"]) == {"format", "title", "pattern"}
    assert out["required"] == ["format", "title"]
    # the keyword `pattern` inside a property is still stripped
    assert "pattern" not in out["properties"]["pattern"]
