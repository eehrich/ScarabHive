// Command history for the composer: Arrow-Up recalls what was sent before.
//
// There is no store of its own. The history IS the session's user messages,
// rebuilt whenever a session is loaded, so a resumed conversation brings its
// history back and nothing can drift apart from the transcript. The terminal
// chat seeds itself from the same place (cli_utils/chat/context.py:_history_seed).
//
// The hard part is not the history, it is not stealing the arrow keys. The
// composer is a textarea, so Up/Down are also ordinary caret movement, and a
// history that grabs them unconditionally eats the caret move and, worse,
// throws away a half-written draft.
//
// So the keys are never intercepted up front. The default action runs, and a
// tick later we look: did the caret actually move? If it did, the keystroke
// was caret movement and we stay out of it. If it did not, the caret was
// already at the very top (or bottom) and the key had nothing left to do --
// that is when the history takes over. This is exact for wrapped lines too,
// where no character offset can tell you which visual row you are on, and it
// gives the familiar escalation: the first Up walks to the top of the draft,
// the next one reaches back into the history.
(function () {
  'use strict';

  var entries = [];
  var index = null;   // null = not browsing; otherwise position in `entries`
  // Edits made while browsing, by index. The slot at entries.length holds the
  // unsent draft. Shells keep a working copy per line like this, so typing
  // into a recalled entry and then walking on does not silently discard it --
  // setValue() writes input.value programmatically, which leaves the browser
  // no undo entry to get it back from.
  var working = {};

  // A stored message longer than this is not a thing anyone wants back in the
  // composer: a /skill turn stores the EXPANDED skill body as its user
  // message, 6-33 KB of it. Mirrors _HISTORY_MAX_CHARS in cli_utils/chat/context.py.
  var MAX_CHARS = 2000;

  // The exact inverse of the "//" unescape in chat_commands.parse_chat_command
  // -- these two patterns are _COMMAND_WORD and _QUALIFIED_WORD from
  // src/agent_system/chat_commands.py, which is the authority; a test in
  // tests/js/ fails if they drift apart. A message sent as "//compact" is
  // stored as "/compact", and offered back raw it would RUN the command
  // instead of being sent again. Escaping a head that would NOT be unescaped
  // on the way in is just as wrong: the agent would get the extra slash.
  var COMMAND_WORD = /^\/[A-Za-z?][A-Za-z0-9_-]*$/;
  var QUALIFIED_WORD = /^\/[A-Za-z][A-Za-z0-9_-]*:[A-Za-z][A-Za-z0-9_-]*$/;

  function needsEscape(text) {
    var stripped = text.trim();
    if (stripped.indexOf('/') !== 0 || stripped.indexOf('//') === 0) return false;
    if (stripped.indexOf('\n') !== -1) return false;
    var head = stripped.split(/\s+/)[0];
    return COMMAND_WORD.test(head) || QUALIFIED_WORD.test(head);
  }

  /** Readable text of a message whose content may be multimodal. */
  function messageText(message) {
    var content = message && message.content;
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';
    return content.map(function (part) {
      if (!part) return '';
      if (typeof part === 'string') return part;
      return part.text || ('[' + (part.type || 'part') + ']');
    }).filter(Boolean).join(' ');
  }

  function push(text) {
    var value = (text || '').trim();
    if (!value) return;
    // Consecutive repeats add nothing but distance to the older entries.
    if (entries.length && entries[entries.length - 1] === value) return;
    if (value.length > MAX_CHARS) return;
    if (needsEscape(value)) value = '/' + value;
    entries.push(value);
  }

  function rebuild(messages, input) {
    // Browsing was interrupted by the switch. The entries belong to the old
    // session, but the parked draft is the person's own unsent text -- give it
    // back instead of leaving a recalled entry from a conversation that is no
    // longer on screen.
    if (input && index !== null) setValue(input, textAt(entries.length), 'end');
    entries = [];
    (messages || []).forEach(function (message) {
      if (message && message.role === 'user') push(messageText(message));
    });
    reset();
  }

  /**
   * Put text in the box the way a person would: caret parked where the next
   * keystroke of the same direction keeps browsing instead of moving.
   */
  function setValue(input, text, caret) {
    input.value = text;
    var pos = caret === 'start' ? 0 : text.length;
    try {
      input.setSelectionRange(pos, pos);
    } catch (e) {
      /* detached or unsupported input type */
    }
    // Auto-resize and the slash suggestions both listen on `input`.
    try {
      input.dispatchEvent(new Event('input', { bubbles: true, cancelable: false }));
    } catch (e) {
      var legacy = document.createEvent('Event');
      legacy.initEvent('input', true, false);
      input.dispatchEvent(legacy);
    }
    // A recalled "/vars" reopens the slash dropdown, which owns Up/Down while
    // it is open -- that would freeze the browsing we are in the middle of.
    try {
      if (window.slashCommands && window.slashCommands.close) {
        window.slashCommands.close();
      }
    } catch (e) {
      /* slash module absent */
    }
  }

  /** The edited text of a slot if it has one, otherwise the stored entry. */
  function textAt(position) {
    if (Object.prototype.hasOwnProperty.call(working, position)) {
      return working[position];
    }
    return entries[position] || '';
  }

  function older(input) {
    if (!entries.length) return;
    if (index === null) {
      working = {};
      index = entries.length;
    }
    working[index] = input.value;  // keep what is in the box before leaving it
    if (index === 0) return;       // oldest entry reached
    index -= 1;
    setValue(input, textAt(index), 'start');
  }

  function newer(input) {
    if (index === null) return;
    working[index] = input.value;
    index += 1;
    setValue(input, textAt(index), 'end');
    if (index >= entries.length) reset();  // back at the draft: done browsing
  }

  function reset() {
    index = null;
    working = {};
  }

  function init() {
    var input = document.getElementById('task');
    if (!input) return;

    input.addEventListener('keydown', function (e) {
      // The slash dropdown handles Up/Down in the capture phase while it is
      // open and marks them handled. Its claim wins.
      if (e.defaultPrevented) return;
      if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;

      if (e.key === 'Escape') {
        // Not browsing: Escape is not ours. Taking it here would wipe a
        // half-typed draft AND swallow the key from whatever else wants it.
        if (index === null) return;
        e.preventDefault();
        setValue(input, textAt(entries.length), 'end');
        reset();
        return;
      }
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;

      // Deliberately no preventDefault: let the caret move if it can, and
      // decide afterwards. `isComposing` guards IME candidate lists, which
      // use the same keys.
      if (e.isComposing) return;
      var before = input.selectionStart;
      var text = input.value;
      var up = e.key === 'ArrowUp';
      window.setTimeout(function () {
        if (input.value !== text) return;             // something else edited it
        if (input.selectionStart !== before) return;  // the caret moved: not ours
        if (up) older(input); else newer(input);
      }, 0);
    });

    var form = document.getElementById('f');
    if (form) {
      // On `document`, in the CAPTURE phase, on purpose: the chat module's
      // own submit handler empties the box SYNCHRONOUSLY for an ordinary
      // message (chat_module.js, `taskInput.value = ''`). A second listener
      // on the form runs after it and would record an empty string every
      // time. Capture on an ancestor runs before any listener on the target.
      document.addEventListener('submit', function (event) {
        if (event.target !== form) return;
        push(input.value);
        reset();
      }, true);
    }

    // Switching or resuming a session swaps the history with it. Same event
    // the chat module restores the messages from.
    window.addEventListener('session:loaded', function (event) {
      var session = event && event.detail && event.detail.session;
      if (session) rebuild(session.messages, input);
    });
    // "New conversation" and deleting the current one dispatch this instead,
    // and never a 'session:loaded' -- a session that starts by simply sending
    // a message is never loaded at all. Without this the entries kept piling
    // up across every later conversation for the life of the tab.
    window.addEventListener('session:new', function () {
      rebuild([], input);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
