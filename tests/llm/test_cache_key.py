"""Tests fuer llm/cache_key.py — prompt_cache_key "auto"-Ableitung."""

from agent_system.llm.cache_key import (
    PREFIX_CHARS,
    PROMPT_CACHE_KEY_AUTO,
    derive_prompt_cache_key,
)


def _chat(system: str, user: str) -> list:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _responses(system: str, user: str) -> list:
    return [
        {"role": "system", "content": [{"type": "input_text", "text": system}]},
        {"role": "user", "content": [{"type": "input_text", "text": user}]},
    ]


class TestStaticPassthrough:
    def test_static_value_unchanged(self):
        key = derive_prompt_cache_key("mein-agent", _chat("sys", "task"))
        assert key == "mein-agent"

    def test_auto_produces_hash(self):
        key = derive_prompt_cache_key(PROMPT_CACHE_KEY_AUTO, _chat("sys", "task"))
        assert key.startswith("auto-")
        assert len(key) == len("auto-") + 16


class TestPrefixGrouping:
    def test_same_prefix_same_key(self):
        a = derive_prompt_cache_key("auto", _chat("sys" * 100, "gleicher task"))
        b = derive_prompt_cache_key("auto", _chat("sys" * 100, "gleicher task"))
        assert a == b

    def test_early_divergence_different_key(self):
        a = derive_prompt_cache_key("auto", _chat("sys", "Buch A Synopsis ..."))
        b = derive_prompt_cache_key("auto", _chat("sys", "Buch B Synopsis ..."))
        assert a != b

    def test_long_system_prompt_does_not_mask_task(self):
        # Kern-Fall (User-Einwand): System-Prompt allein >PREFIX_CHARS
        # (z.B. scene_planner). Das Fenster zaehlt erst AB der ersten
        # Nicht-System-Message — verschiedene Buecher muessen trotz
        # identischem Riesen-System-Prompt verschiedene Keys bekommen.
        big_sys = "regelwerk " * 800  # ~8000 Zeichen
        a = derive_prompt_cache_key("auto", _chat(big_sys, "Buch A Synopsis"))
        b = derive_prompt_cache_key("auto", _chat(big_sys, "Buch B Synopsis"))
        assert a != b

    def test_different_system_prompt_different_key(self):
        a = derive_prompt_cache_key("auto", _chat("agent eins", "task"))
        b = derive_prompt_cache_key("auto", _chat("agent zwei", "task"))
        assert a != b

    def test_divergence_behind_prefix_window_same_key(self):
        # Divergenz JENSEITS der ersten PREFIX_CHARS Zeichen der
        # Task-Message trennt die Keys absichtlich nicht.
        stable_task = "x" * PREFIX_CHARS
        a = derive_prompt_cache_key("auto", _chat("sys", stable_task + "Szene 1"))
        b = derive_prompt_cache_key("auto", _chat("sys", stable_task + "Szene 2 anders"))
        assert a == b

    def test_appended_messages_keep_key(self):
        # Folge-Turns derselben Session (auch nachgeschobene System-
        # Injections) aendern den Key nie — nur fuehrender System-Prompt
        # + erste Task-Message zaehlen.
        msgs = _chat("sys", "task")
        a = derive_prompt_cache_key("auto", msgs)
        longer = msgs + [
            {"role": "assistant", "content": "antwort"},
            {"role": "system", "content": "mid-run injection"},
            {"role": "user", "content": "folgefrage"},
        ]
        b = derive_prompt_cache_key("auto", longer)
        assert a == b


class TestFormats:
    def test_chat_and_responses_format_extract_same_text(self):
        # Beide Serialisierungen desselben Prompts -> gleicher Key
        # (Format-Wechsel Chat <-> Responses darf die Gruppe nicht trennen).
        a = derive_prompt_cache_key("auto", _chat("sys-prompt", "task-text"))
        b = derive_prompt_cache_key("auto", _responses("sys-prompt", "task-text"))
        assert a == b

    def test_non_text_parts_and_junk_are_skipped(self):
        msgs = [
            {"role": "system", "content": "sys"},
            "kein-dict",
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "text", "text": "task"},
                ],
            },
        ]
        key = derive_prompt_cache_key("auto", msgs)
        assert key == derive_prompt_cache_key("auto", _chat("sys", "task"))

    def test_empty_messages_still_deterministic(self):
        a = derive_prompt_cache_key("auto", [])
        b = derive_prompt_cache_key("auto", [])
        assert a == b and a.startswith("auto-")
