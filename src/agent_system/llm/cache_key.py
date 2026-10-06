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

1. den FUEHRENDEN System-/Developer-Messages, komplett — ausser den
   INJIZIERTEN (``injected_by`` gesetzt). Der System-Prompt ist pro Agent
   konstant; ihn voll zu hashen ist deterministisch und verhindert, dass
   ein langer System-Prompt (>4k, z.B. scene_planner) das Fenster
   auffrisst, bevor buchspezifischer Inhalt sichtbar wird. Ein injizierter
   Block dort ist das Gegenteil von konstant: er wird jeden Call neu
   gebaut, und mitgehasht wanderte der Key mit jedem abgehakten
   Todo-Punkt auf eine neue Shard. Das eigentliche Heilmittel ist die
   STELLE — ein Block am Ende laesst den Prefix davor byte-identisch —,
   diese Regel deckt, was trotzdem im Kopf steht.

   ⚠️ Der Umkehrschluss: was markiert ist, traegt keine Identitaet mehr.
   Ein injizierter Block, der pro Buch/Story verschiedenen Text haette
   (z.B. ein ``simple_prompt_inject`` mit buchspezifischer
   ``template_vars``-Variable), faellt damit aus dem Key und legt zwei
   Laeufe auf dieselbe Shard. Was ein Lauf vom anderen unterscheidet,
   gehoert in den System-Prompt oder in die Task-Message.
2. der ERSTEN Nicht-System-Message, auf ``PREFIX_CHARS`` Zeichen gekappt.
   Nur die erste: spaeter angehaengte Turns derselben Session aendern den
   Key damit nie — alle Calls einer Konversation bleiben in derselben
   Cache-Gruppe. Injected messages are skipped here too: a note behind the
   system prompt is the same for every run and names no book. Only when no
   user task follows does the first injected one count, as before.

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


# --- Segment-Leiter -----------
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


def _key_field(msg: Any, name: str) -> Any:
    """A field of a message, dict or ChatMessage.

    The key is derived from the ORIGINAL messages, not from the finished
    payload: a system block carries no ``injected_by`` there (httpx drops it at
    its rung step, the Responses client builds new items and keeps it on
    developer items only), and without the marker the derivation cannot tell a
    block rebuilt on every call from a prompt.
    """
    if isinstance(msg, dict):
        return msg.get(name)
    return getattr(msg, name, None)


def _iter_msg_texts(msg: Any) -> Iterator[str]:
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
    role = _key_field(msg, "role")
    if role:
        yield f"<{role}>"
    content: Any = _key_field(msg, "content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            # Dict ODER pydantic-Modell: ChatMessage.content ist
            # List[ContentItem], die Parts sind dort TextContent/ImageContent,
            # keine Dicts. Nur-Dict gelesen blieb von so einer Message der
            # Rollen-Marker uebrig — der Key war fuer JEDEN Agenten mit
            # Listen-Content derselbe, also eine geteilte Shard statt einer
            # pro Buch.
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(text, str):
                yield text
    # Responses-API function_call / function_call_output items
    for key in ("name", "arguments", "output"):
        val = _key_field(msg, key)
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
    first: Any = None      # the message that names the run
    injected: Any = None   # the first injected one, if no task follows it
    for msg in messages:
        # Was keine Message ist, wird uebersprungen — wie vorher, nur dass ein
        # ChatMessage-Objekt jetzt eine ist. Ohne die zweite Haelfte faellt so
        # ein Fremdkoerper in den Zweig unten, hasht nichts und BRICHT: die
        # Task-Message danach ginge nicht mehr in den Key ein, und zwei Buecher
        # teilten sich eine Shard.
        if not isinstance(msg, dict) and not hasattr(msg, "role"):
            continue
        role = str(_key_field(msg, "role") or "")
        if role in _SYSTEM_ROLES:
            # Not an injected block. Whoever puts one into the leading run
            # rebuilds it on every step -- the todo list, the restored
            # context, a rendered prompt block. Hashed along, the key moved
            # with the text: every tick of a checkbox sent the run to a fresh
            # shard, so not even the system prompt in FRONT of the block could
            # be read back. The key answers "which prefix is this", and a text
            # that is rebuilt per call is not part of any prefix. Measured:
            # same agent, two todo states, two keys.
            #
            # An UNMARKED block still counts -- nobody rebuilds it, and it
            # really is a different prompt.
            if _key_field(msg, "injected_by"):
                continue
            for frag in _iter_msg_texts(msg):
                h.update(frag.encode("utf-8", "replace"))
            continue
        if _key_field(msg, "injected_by"):
            # The same rule outside the head: a note injected as a user
            # message right behind the system prompt (simple_prompt_inject,
            # after_system) is the same text for every run of the agent.
            # Taken for "the first message", it put every book on one shard.
            injected = injected if injected is not None else msg
            continue
        # A run whose only task is injected (forge news on a woken session, a
        # summary heading a compacted chat) keeps it: hashing the answer
        # behind it put those sessions on one key and moved it after call 1.
        first = msg if role == "user" or injected is None else injected
        break
    else:
        first = injected
    # Erste Nicht-System-Message: gekapptes Fenster, dann Schluss —
    # spaetere Messages (auch nachgeschobene System-Injections) sind
    # nicht Teil des stabilen Prefix.
    taken = 0
    for frag in _iter_msg_texts(first) if first is not None else ():
        if taken >= PREFIX_CHARS:
            break
        piece = frag[: PREFIX_CHARS - taken]
        h.update(piece.encode("utf-8", "replace"))
        taken += len(piece)
    return f"auto-{h.hexdigest()[:16]}"


# --- Anthropic cache_control (ephemeral breakpoints) -------------------------
#
# GETEILTE Single-Source-of-Truth fuer ALLE Claude-Pfade (nativ Anthropic-SDK,
# Anthropic-via-OpenRouter im httpx-Client, kuenftig Responses-Client). Anthropic
# cached alles BIS EINSCHLIESSLICH eines ``cache_control``-Markers; EIN Breakpoint
# am Prefix-Ende deckt den ganzen Prefix davor ab. Hartes Limit: hoechstens
# ANTHROPIC_MAX_CACHE_BLOCKS Bloecke mit cache_control pro Request (System +
# Tools + Messages ZUSAMMEN) — Ueberschreiten = HTTP 400.
#
# Jeder Client bringt sein eigenes Nachrichtenformat mit (Chat-Completions-Dicts
# vs. native Anthropic-Bloecke vs. Responses-input_items); die POLICY (was, wie
# oft, in welcher Reihenfolge markiert wird) ist identisch und lebt hier. Die
# Clients komponieren nur die granularen Helfer an den Stellen, wo ihre Daten
# verfuegbar sind (System frueh, Tools spaet).

#: Anthropic-Cache-Marker. Bewusst pro Aufruf kopiert (``dict(...)``), damit
#: kein geteiltes Objekt versehentlich mutiert wird.
ANTHROPIC_EPHEMERAL: dict = {"type": "ephemeral"}

#: Hartes API-Limit: max. so viele cache_control-Bloecke pro Request.
ANTHROPIC_MAX_CACHE_BLOCKS = 4

#: Text-Block-Typen ueber die Formate: Chat-Completions ``text``,
#: Responses-API ``input_text``/``output_text``. System-/Tool-Marker gehoeren
#: immer auf einen Textblock (nie auf image).
_ANTHROPIC_TEXT_TYPES = ("text", "input_text", "output_text")

#: Blocktypen, die am Konversations-TAIL cache_control tragen duerfen: Text
#: plus ``tool_result``. Ein Agent-Turn endet oft auf einem tool_result-Block
#: (nativer Anthropic-Pfad: ``{"role":"user","content":[{"type":"tool_result",
#: ...}]}``) — den zu markieren laesst den Breakpoint auch dann ans Turn-Ende
#: wandern. Ohne das cachte der native Multi-Turn-Pfad tool-endende Turns nicht
#: (der OpenRouter-Pfad tut es, weil dort Tool-Ergebnisse String-Text sind, der
#: zu einem Textblock gehoben wird) — diese Menge stellt die Paritaet her.
#: Anthropic erlaubt cache_control auf text/image/tool_use/tool_result/document.
_ANTHROPIC_TAIL_TYPES = _ANTHROPIC_TEXT_TYPES + ("tool_result",)

#: Rollen, deren Vorhandensein eine ECHTE laufende Konversation belegt.
_HISTORY_ROLES = ("assistant", "tool")


def anthropic_cache_conversation(mode: str | None, has_history: bool) -> bool:
    """Ob der wachsende Konversations-Tail als cache_control-Breakpoint markiert
    wird (Anthropic-Multi-Turn-Pattern: der Marker wandert jede Runde ans Ende;
    der Vorgaenger-Prefix ist ein Byte-Prefix des neuen Requests und wird
    gelesen statt voll bezahlt).

    - ``multi_turn``: ab Runde 1 (deklarierte Konversation).
    - ``auto`` / ``None``: erst wenn echte Historie existiert (>=1 assistant/
      tool-Message) — kein verschwendeter Write auf echten Einzel-Calls.
    - ``task_sequence`` (Leiter) / ``one_shot`` / ``off``: nein.
    """
    if mode == CACHE_MODE_MULTI_TURN:
        return True
    if mode in (None, CACHE_MODE_AUTO):
        return has_history
    return False


def messages_have_history(message_dicts: list) -> bool:
    """>=1 assistant/tool-Message => laufende Konversation (kein Einzel-Call)."""
    return any(
        isinstance(m, dict) and m.get("role") in _HISTORY_ROLES
        for m in message_dicts
    )


def mark_last_text_block(blocks: list) -> bool:
    """cache_control: ephemeral auf den LETZTEN Textblock einer Block-Liste.

    Gibt zurueck, ob markiert wurde. Idempotent auf demselben Block. Versteht
    Chat-Completions- und Responses-Textblock-Typen (_ANTHROPIC_TEXT_TYPES).
    """
    if not isinstance(blocks, list):
        return False
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if isinstance(b, dict) and b.get("type") in _ANTHROPIC_TEXT_TYPES:
            b["cache_control"] = dict(ANTHROPIC_EPHEMERAL)
            return True
    return False


def mark_message_tail(msg: dict) -> bool:
    """cache_control auf den letzten Textblock EINER Message; bare-string-Content
    wird zuvor in einen Textblock gehoben. Gibt zurueck, ob markiert wurde."""
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        if not content:
            return False
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(ANTHROPIC_EPHEMERAL)}
        ]
        return True
    if isinstance(content, list):
        return mark_last_text_block(content)
    return False


def mark_last_system(message_dicts: list) -> bool:
    """cache_control auf die LETZTE System-Message (deren Marker den gesamten
    System-Prefix abdeckt). Nur die letzte — jede zu markieren sprengt zusammen
    mit Tool-/Tail-Markern das 4-Block-Limit. Gibt zurueck, ob markiert wurde."""
    last_system = None
    for msg in message_dicts:
        if isinstance(msg, dict) and msg.get("role") == "system":
            last_system = msg
    if last_system is None:
        return False
    return mark_message_tail(last_system)


def mark_last_cacheable_block(blocks: list) -> bool:
    """cache_control: ephemeral auf den LETZTEN cachebaren Block einer Liste
    (Text ODER tool_result, s. _ANTHROPIC_TAIL_TYPES). Fuer den Konversations-
    Tail, damit ein tool_result-endender Turn den Breakpoint trotzdem ans Ende
    zieht. Gibt zurueck, ob markiert wurde."""
    if not isinstance(blocks, list):
        return False
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if isinstance(b, dict) and b.get("type") in _ANTHROPIC_TAIL_TYPES:
            b["cache_control"] = dict(ANTHROPIC_EPHEMERAL)
            return True
    return False


def mark_conversation_tail(message_dicts: list) -> bool:
    """cache_control auf den Tail der LETZTEN Message (wachsender Konversations-
    Prefix, Multi-Turn-Pattern). Aufrufer gated via anthropic_cache_conversation.
    Gibt zurueck, ob markiert wurde.

    Anders als System/Tools markiert der Tail auch einen tool_result-Block (nicht
    nur Text), damit tool-endende Agent-Turns den Breakpoint ans Ende ziehen
    (Paritaet nativer Pfad <-> OpenRouter-Pfad). bare-string-Content (OpenAI-
    Format-Tool-/User-Message) wird zuvor in einen Textblock gehoben.

    Byte-Stabilitaets-Vorbehalt: mit ``reasoning_details_mode=keep_last`` (Default)
    werden aeltere Reasoning-Bloecke eines THINKING-Modells jede Runde entfernt —
    das aendert den Prefix und bricht das Konversations-Caching ab dem ersten
    reasoning-tragenden Turn. Nicht-Thinking-Modelle und ``keep_all`` halten den
    Prefix stabil; Thinking-Agents mit vollem Multi-Turn-Cache brauchen
    ``reasoning_details_mode: keep_all``."""
    if not message_dicts:
        return False
    # Past a developer note: that is the RUN talking, not the conversation, and
    # its text is rebuilt for every call. A breakpoint on it would make the
    # prefix up to the marker differ every turn -- the tail cache would never
    # hit again, which is the exact opposite of what marking it is for.
    tail = [m for m in message_dicts if not (isinstance(m, dict) and m.get("role") == "developer")]
    if not tail:
        return False
    msg = tail[-1]
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        if not content:
            return False
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(ANTHROPIC_EPHEMERAL)}
        ]
        return True
    if isinstance(content, list):
        return mark_last_cacheable_block(content)
    return False


def mark_last_tool(tools: list) -> bool:
    """cache_control auf die letzte Tool-Definition (deckt den ganzen Tool-Block
    ab). Gibt zurueck, ob markiert wurde."""
    if not tools:
        return False
    last = tools[-1]
    if isinstance(last, dict):
        last["cache_control"] = dict(ANTHROPIC_EPHEMERAL)
        return True
    return False


def _iter_cache_carriers(container: Any) -> Iterator[dict]:
    """cache_control-tragende Dicts aus einem Container (Message-Dict mit
    content-Liste, Tool-Dict, oder roher Textblock)."""
    if not isinstance(container, dict):
        return
    content = container.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and "cache_control" in part:
                yield part
    elif "cache_control" in container:
        yield container


def cap_cache_control(
    ordered_groups: list,
    max_blocks: int = ANTHROPIC_MAX_CACHE_BLOCKS,
) -> None:
    """Defense-in-depth: ueber alle ``ordered_groups`` (in Anthropic-Prefix-
    Reihenfolge: Tools, dann System, dann Messages) nur die LETZTEN
    ``max_blocks`` cache_control-Bloecke behalten; cache_control von den
    frueheren in-place entfernen.

    Ein spaeterer Breakpoint cached alles, was ein frueherer wuerde — die
    fruehesten zu opfern verliert keine Coverage, garantiert aber, dass keine
    Marker-Kombination (System + Tools + Tail + Sentinel-Splits) je das harte
    HTTP-400-Limit reisst. ``ordered_groups`` ist eine Liste von Container-Listen
    (jede Gruppe wird der Reihe nach abgelaufen)."""
    carriers: list[dict] = []
    for group in ordered_groups:
        for container in group or []:
            carriers.extend(_iter_cache_carriers(container))
    excess = len(carriers) - max_blocks
    for c in carriers[:excess] if excess > 0 else []:
        c.pop("cache_control", None)
