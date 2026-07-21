"""Ableitung des OpenAI ``prompt_cache_key`` aus dem Prompt-Praefix.

GPT-5.6+ matcht den Prompt-Cache praktisch nur noch, wenn ein
``prompt_cache_key`` gesetzt ist (OpenAI-Doku, belegt Testlauf B936:
byte-identischer 10k-Prefix, 0 cached_tokens ohne Key). Der Key ist ein
Routing-Hint: Requests mit demselben Key landen auf derselben Cache-Shard,
~15 req/min pro Key sustained.

Ein statischer Key pro Agent kollidiert, sobald mehrere Buecher/Stories
parallel laufen: verschiedene Prefixe teilen sich dann eine Shard
(Eviction-Thrash) und reissen das Rate-Limit. Session-IDs taugen auch
nicht — die Pipeline erzeugt pro Schritt neue Sessions, der Cache-Verbund
wuerde zerrissen.

Loesung ``prompt_cache_key: "auto"`` — der Key wird zur Laufzeit gehasht
aus:

1. den FUEHRENDEN System-/Developer-Messages, komplett. Der System-Prompt
   ist pro Agent konstant; ihn voll zu hashen ist deterministisch und
   verhindert, dass ein langer System-Prompt (>4k, z.B. scene_planner)
   das Fenster auffrisst, bevor buchspezifischer Inhalt sichtbar wird.
2. der ERSTEN Nicht-System-Message, auf ``PREFIX_CHARS`` Zeichen gekappt.
   Nur die erste: spaeter angehaengte Turns derselben Session aendern den
   Key damit nie — alle Calls einer Konversation bleiben in derselben
   Cache-Gruppe.

Damit gilt automatisch: gleicher stabiler Prefix <-> gleicher Key.

- scene_planner u.ae. (buchstabiler Block frueh im Task): Key ist
  implizit pro Buch — parallele Buecher kollidieren nicht.
- Agents mit generischem Kickoff-Task: Key degeneriert zum Agent-Key
  (System-Hash) — exakt das Verhalten des statischen Keys, kein Verlust.

PREFIX_CHARS = 4096 Zeichen entspricht grob der minimalen cachebaren
Einheit von 1024 Tokens: Wer System-Prompt + diesen Task-Anfang teilt,
gehoert in dieselbe Cache-Gruppe; was erst spaeter divergiert, trennt
die Keys absichtlich nicht.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

PROMPT_CACHE_KEY_AUTO = "auto"
PREFIX_CHARS = 4096

_SYSTEM_ROLES = {"system", "developer"}


def _iter_msg_texts(msg: dict) -> Iterator[str]:
    """Textfragmente EINER Message (Rollen-Marker + Text-Parts).

    Versteht beide Formate:
    - Chat Completions: ``{"role": ..., "content": str | [{"type": "text",
      "text": ...}, ...]}``
    - Responses API input items: ``{"role": ..., "content":
      [{"type": "input_text"|"output_text", "text": ...}]}`` sowie
      function_call-Items (name/arguments/output).

    Nicht-Text-Parts (Bilder/Audio) werden uebersprungen; die Rolle geht
    mit ein, damit Rollen-Grenzen den Hash beeinflussen.
    """
    role = msg.get("role")
    if role:
        yield f"<{role}>"
    content: Any = msg.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    yield text
    # Responses-API function_call / function_call_output items
    for key in ("name", "arguments", "output"):
        val = msg.get(key)
        if isinstance(val, str) and val:
            yield val


def derive_prompt_cache_key(configured: str, messages: list) -> str:
    """Effektiven prompt_cache_key bestimmen.

    Statische Werte gehen unveraendert durch (explizites Override).
    ``"auto"`` -> ``auto-<sha256[:16]>`` ueber fuehrende System-Messages
    (voll) + erste Nicht-System-Message (auf PREFIX_CHARS gekappt) —
    Details im Modul-Docstring.
    """
    if configured != PROMPT_CACHE_KEY_AUTO:
        return configured
    h = hashlib.sha256()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role in _SYSTEM_ROLES:
            for frag in _iter_msg_texts(msg):
                h.update(frag.encode("utf-8", "replace"))
            continue
        # Erste Nicht-System-Message: gekapptes Fenster, dann Schluss —
        # spaetere Messages (auch nachgeschobene System-Injections) sind
        # nicht Teil des stabilen Prefix.
        taken = 0
        for frag in _iter_msg_texts(msg):
            if taken >= PREFIX_CHARS:
                break
            piece = frag[: PREFIX_CHARS - taken]
            h.update(piece.encode("utf-8", "replace"))
            taken += len(piece)
        break
    return f"auto-{h.hexdigest()[:16]}"
