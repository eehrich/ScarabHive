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

# --- Explizite Cache-Breakpoints (GPT-5.6+) ----------------------------------
#
# Empirisch kartiert (2026-07-21, 15 Experimente via OpenRouter /responses):
# Implizites 5.6-Caching matcht NUR Exakt-Wiederholungen und Konversations-
# Fortsetzungen — ein Request, der einen langen Prefix teilt und dann mitten
# im letzten Item divergiert, cached IMMER 0 (auch bei 29k Tokens identischem
# Prefix). Fix lt. OpenAI-Doku: `prompt_cache_breakpoint` am Content-Part
# markiert das Ende eines wiederverwendbaren Prefix; Hit = laengster Prefix
# aus byte-identischen KOMPLETTEN Breakpoint-Bloecken. Max 4 Cache-Writes
# pro Request (impliziter Breakpoint belegt einen Slot -> max 3 explizite).
#
# Pipelines markieren Block-Grenzen im Task-Text mit diesem Sentinel; die
# OpenAI-faehigen Clients splitten daran in Content-Parts mit Breakpoint-
# Markern, alle anderen Provider-Pfade STRIPPEN den Sentinel rueckstandsfrei.
CACHE_BP_SENTINEL = "\n<<<CACHE_BREAKPOINT>>>\n"
MAX_EXPLICIT_BREAKPOINTS = 3


def split_cache_breakpoint_blocks(text: str) -> list[str]:
    """Text an CACHE_BP_SENTINEL in Bloecke teilen (Sentinel entfaellt).

    Hoechstens MAX_EXPLICIT_BREAKPOINTS Grenzen bleiben erhalten; weitere
    Sentinels werden in den letzten Block gemergt. Leere Bloecke (Sentinel
    am Anfang/Ende, Doppel-Sentinel) fallen weg. Ohne Sentinel: [text].
    """
    if CACHE_BP_SENTINEL not in text:
        return [text]
    raw = text.split(CACHE_BP_SENTINEL)
    blocks = [b for b in raw if b]
    if not blocks:
        return [""]
    if len(blocks) > MAX_EXPLICIT_BREAKPOINTS + 1:
        head = blocks[:MAX_EXPLICIT_BREAKPOINTS]
        tail = "".join(blocks[MAX_EXPLICIT_BREAKPOINTS:])
        blocks = head + [tail]
    return blocks


def strip_cache_breakpoints(text: str) -> str:
    """Sentinel rueckstandsfrei entfernen (fuer Provider ohne Breakpoint-Support)."""
    if CACHE_BP_SENTINEL not in text:
        return text
    return "".join(text.split(CACHE_BP_SENTINEL))


# --- Segment-Leiter (Konzept v2, docs/prompt_cache_design.md §3.2) -----------
#
# Task-Sequenz-Agenten (viele Einzel-Calls, wachsender gemeinsamer Prefix)
# deklarieren im Task: [static] S [append_only] S [volatile]. Der Client
# ergaenzt aus einer Prozess-Registry einen dritten Marker (BP1) an der
# append_only-Grenze des VORGAENGER-Calls — dessen gespeicherter Prefix ist
# byte-identisch -> Read-Hit ab Call 2. BP2 (deklariertes append-Ende)
# schreibt den laengeren Prefix fuer den Folgecall; BP0 (static-Ende) macht
# Prefix-Brueche billig. Live validiert (Probe L): Hit ab Call 2, Writes nur
# Delta, Bruch = 1 Miss + sofortiges Relearn.

#: Marker-Stile pro Modell (LLMModelConfig.prompt_cache_marker_style)
MARKER_STYLE_OPENAI = "openai"        # prompt_cache_breakpoint (GPT-5.6+)
MARKER_STYLE_ANTHROPIC = "anthropic"  # cache_control ephemeral
MARKER_STYLE_NONE = "none"            # Marker strippen (deepseek/gemini/...)

#: prompt_cache_mode-Werte (AgentConfig, docs §4)
CACHE_MODE_AUTO = "auto"
CACHE_MODE_MULTI_TURN = "multi_turn"
CACHE_MODE_TASK_SEQUENCE = "task_sequence"
CACHE_MODE_ONE_SHOT = "one_shot"
CACHE_MODE_OFF = "off"

#: Leiter-Mindestwachstum: unterhalb ~1024 Tokens (4096 Zeichen) kann der
#: naechste Call keinen eigenen cachebaren Read auf dem Delta bilden.
LADDER_MIN_PREFIX_CHARS = 4096


class CacheBoundaryRegistry:
    """Prozess-globale Registry fuer die KUMULATIVE Segment-Leiter.

    GPT-5.6-Match-Regel (E2E-kartiert 2026-07-21): Ein gespeicherter
    Breakpoint-Prefix trifft nur, wenn der neue Request die Block- UND
    Marker-Struktur des Vorgaengers bis dorthin EXAKT reproduziert; erlaubt
    ist nur ANHAENGEN. Ein wandernder oder entfernter Marker bricht alle
    dahinter verankerten Reads (gemessen: konstant nur Static-Hit bzw. 0%).
    Die API akzeptiert dabei problemlos 6+ Marker — das 4er-Limit gilt nur
    fuer NEUE Cache-Writes pro Request (unsere sind <=2).

    Deshalb speichert die Registry pro Key die RUNG-LISTE (Offsets im
    append_only-Block): jeder Call reproduziert alle bisherigen Rungs als
    markierte Bloecke und haengt hoechstens eine neue ans Ende
    (MIN_RUNG_CHARS verduennt). Validierung per Hash bis zur letzten Rung;
    Bruch -> Liste reset, Relearn ab dem naechsten Call.

    Commit passiert zur PLAN-Zeit: sequenziell korrekt, parallel kostet es
    hoechstens einen Miss mit Selbstheilung — nie Korrektheit.
    """

    _MAX_KEYS = 128  # > parallele Buecher x GPT-Task-Sequenz-Agenten (~10)
    #: Neue Rung erst ab diesem Zuwachs (~1024 Tokens) — verduennt die
    #: Marker-Zahl; zwischen Rungs wiederholen Calls exakt dieselbe Struktur.
    MIN_RUNG_CHARS = 4096
    #: Sicherheits-Cap: alte Rungs duerfen NIE entfernt werden (bricht die
    #: Struktur-Reproduktion), also stoppt das Anhaengen — Wachstum bleibt
    #: dann im unmarkierten Tail, Hits bis zur letzten Rung bleiben stabil.
    MAX_RUNGS = 64

    def __init__(self) -> None:
        self._entries: dict[str, tuple[list[int], str]] = {}
        self._order: list[str] = []  # LRU, aeltester zuerst

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

    def rungs_for(self, key: str, append_text: str) -> list[int]:
        """Gueltige Rung-Liste fuer den aktuellen append-Block (sonst [])."""
        entry = self._entries.get(key)
        if not entry:
            return []
        rungs, digest = entry
        if not rungs:
            return []
        last = rungs[-1]
        if last <= len(append_text) and self._digest(append_text[:last]) == digest:
            return rungs
        return []

    def commit(self, key: str, append_text: str, rungs: list[int]) -> list[int]:
        """Rung-Liste fortschreiben (Plan-Zeit) und zurueckgeben.

        Haengt eine neue Rung ans append-Ende, wenn seit der letzten
        mindestens MIN_RUNG_CHARS gewachsen ist.
        """
        if not append_text:
            return rungs
        last = rungs[-1] if rungs else 0
        if (
            len(rungs) < self.MAX_RUNGS
            and (len(append_text) - last >= self.MIN_RUNG_CHARS or not rungs)
        ):
            rungs = rungs + [len(append_text)]
        self._entries[key] = (rungs, self._digest(append_text[:rungs[-1]]))
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        while len(self._order) > self._MAX_KEYS:
            evicted = self._order.pop(0)
            self._entries.pop(evicted, None)
        return rungs


#: Modulweite Registry — Agent/Client sind prozessweite Singletons, die
#: Leiter braucht den Zustand ueber Client-Instanzen hinweg.
boundary_registry = CacheBoundaryRegistry()


def plan_cache_blocks(
    text: str,
    *,
    mode: str | None,
    key: str | None,
    max_markers: int = MAX_EXPLICIT_BREAKPOINTS,
    registry: CacheBoundaryRegistry | None = None,
) -> list[tuple[str, bool]] | None:
    """Sentinel-Text in (Block, markiert?)-Liste uebersetzen.

    Rueckgabe None = kein Sentinel enthalten (Caller laesst Content
    unangetastet). mode=off -> Sentinels strippen, keine Marker.
    mode=task_sequence + Registry -> Leiter: zusaetzlicher BP1-Split an
    der Vorgaenger-Grenze im append-Block (= mittlerer deklarierter Block
    bei [static]S[append]S[volatile]) + Plan-Zeit-Commit der neuen Grenze.
    Budget: hoechstens ``max_markers`` markierte Bloecke; vergeben von
    HINTEN (BP2 Write-Anker > BP1 Read-Anker > BP0), damit bei knappem
    Budget (Anthropic: System/Tool-Marker zaehlen mit) die wertvollen
    Anker ueberleben.
    """
    if CACHE_BP_SENTINEL not in text:
        return None
    if mode == CACHE_MODE_OFF:
        return [(strip_cache_breakpoints(text), False)]
    blocks = split_cache_breakpoint_blocks(text)
    if len(blocks) < 2:
        return [(blocks[0] if blocks else "", False)]

    # Kumulative Leiter (nur task_sequence, nur bei genau 2 deklarierten
    # Grenzen [static]S[append]S[volatile] — mehr deklarierte Grenzen =
    # Pipeline weiss es besser):
    if (
        mode == CACHE_MODE_TASK_SEQUENCE
        and registry is not None
        and key
        and len(blocks) == 3
    ):
        static_text, append_text, volatile_text = blocks
        rungs = registry.rungs_for(key, append_text)
        rungs = registry.commit(key, append_text, rungs)
        # Struktur: [static(BP)] + eine markierte Scheibe pro Rung + der
        # unmarkierte Rest (append-Tail hinter der letzten Rung + volatile
        # VERSCHMOLZEN — das wandernde append-Ende darf NIE markiert werden,
        # sonst bricht die Struktur-Reproduktion beim naechsten Call).
        out: list[tuple[str, bool]] = [(static_text, True)]
        prev_r = 0
        for r in rungs:
            out.append((append_text[prev_r:r], True))
            prev_r = r
        out.append((append_text[prev_r:] + volatile_text, False))
        # Leere Scheiben (Rung exakt am Ende) rausfiltern, Reihenfolge stabil
        return [(blk, m) for blk, m in out if blk]

    # Ohne Leiter: alle deklarierten Grenzen markieren; Budget von HINTEN
    # vergeben (BP0 zuerst opfern — relevant fuer Anthropic, wo System-/
    # Tool-Marker bereits 2 der 4 Slots belegen).
    n_boundaries = len(blocks) - 1
    marked_from = max(0, n_boundaries - max_markers)
    return [
        (blk, marked_from <= i < n_boundaries)
        for i, blk in enumerate(blocks)
    ]


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
