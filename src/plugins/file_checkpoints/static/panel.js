// Files panel: what the agent changed in files, per checkpoint (file_checkpoints), as diffs -- and /rewind <n>.
// Follows the shell's session; ?session_id= pins it (the chat's context link) and <pk-session> says so.
import { api, ApiError, html, render, update, icon, session, confirm, toast, diffLines, setTitle, localTime } from '/static/kit/panel-kit.js';

const view = document.getElementById('filesView');
const hideSwitch = document.getElementById('hideReviewed');
let latest = 0;
// What the last answer listed, for drawing it again when a mark changes.
let shown = { id: null, entries: [] };

// What the viewer marked reviewed: a help to find their way, kept in this browser only. Per session, a file's
// change at the moment it was made -- a later change to it in the same checkpoint is unreviewed again.
const REVIEWED_KEY = 'fc.reviewed';
const KEEP_SESSIONS = 30;
const HIDE_KEY = 'fc.hideReviewed';

function readStored(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}

function writeStored(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* kept in this page only */ }
}

// Held here, saved to the browser: without storage (a private window, blocked site data) the marks last this page.
const storedMarks = readStored(REVIEWED_KEY, {});
let marksBySession = storedMarks && typeof storedMarks === 'object' && !Array.isArray(storedMarks) ? storedMarks : {};
const allMarks = () => ({ ...marksBySession });

const markOf = (entry, file) => `${entry.number}\n${file.path}\n${file.changed_at || ''}`;
const marksFor = (id) => new Set(Array.isArray(allMarks()[id]) ? allMarks()[id] : []);

function setMarks(id, marks) {
  const stored = allMarks();
  delete stored[id];   // to the end: the sessions marked last are the ones kept
  if (marks.size) stored[id] = [...marks];
  const ids = Object.keys(stored);
  for (const old of ids.slice(0, Math.max(0, ids.length - KEEP_SESSIONS))) delete stored[old];
  marksBySession = stored;
  writeStored(REVIEWED_KEY, stored);
}

// The span a change falls in, counted from now by the viewer's clock and calendar.
const GROUPS = ['Last hour', 'Today', 'Yesterday', 'Last 7 days', 'Earlier'];

function groupOf(stamp, now = new Date()) {
  const at = stamp ? new Date(stamp) : null;
  if (!at || Number.isNaN(at.getTime())) return 'Earlier';
  if (now - at < 3600e3) return 'Last hour';
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(now) - midnight(at)) / 864e5);   // rounded: a day with a clock change is 23 or 25 h
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return days < 7 ? 'Last 7 days' : 'Earlier';
}

const when = (stamp) => stamp
  && html`<time class="fc-time pk-muted pk-mono" datetime="${stamp}">${localTime(stamp, { seconds: true })}</time>`;

const empty = (title, text = '') => html`<div class="pk-empty">${icon('history')}<div class="pk-empty-title">${title}</div>
  ${text && html`<div>${text}</div>`}</div>`;

const query = (params) => new URLSearchParams(params).toString();

function where(entry) {
  if (entry.dropped) return `a turn /undo dropped, after turn ${entry.after_turn}`;
  return `before turn ${entry.turn}`;
}

// How the server names a call that was recorded except for what its look does not see.
const PARTLY = ' (partly not recorded: ';

/** [calls that left no record, calls recorded except for a part], each as "tool xN" text. */
function untrackedParts(untracked) {
  const whole = untracked.filter((u) => !u.tool.includes(PARTLY)).map((u) => `${u.tool} ×${u.count}`);
  const partly = untracked.filter((u) => u.tool.includes(PARTLY)).map((u) => {
    const [name, what] = u.tool.split(PARTLY);
    return `${name} ×${u.count} (${what.replace(/\)$/, '')})`;
  });
  return [whole, partly];
}

/** Calls that left no record, and -- apart, they are no warning of the same weight -- calls recorded except for a part. */
function untrackedLine(untracked) {
  const [whole, partly] = untrackedParts(untracked);
  return html`${whole.length ? html`<p class="pk-muted" data-untracked="whole" style="margin:0">${icon('triangle-alert', { size: 'sm' })}
      Not recorded, only counted (they may have changed files too):
      <span class="pk-mono">${whole.join(', ')}</span></p>` : ''}
    ${partly.length ? html`<p class="pk-muted" data-untracked="partly" style="margin:0">${icon('info', { size: 'sm' })}
      Recorded, except for what the look does not see: <span class="pk-mono">${partly.join(', ')}</span></p>` : ''}`;
}

function reviewButton(attrs, done, label) {
  return html`<button class="pk-btn pk-btn--sm pk-btn--ghost" ${attrs} aria-pressed="${String(done)}"
    title="${done ? 'Marked reviewed -- click to take the mark off' : 'Mark as reviewed'}">${icon(done ? 'circle-check' : 'check', { size: 'sm' })} ${label}</button>`;
}

// Reviewed is taken as it is: what puts it back goes until the mark comes off again.
function fileRow(entry, file, marks) {
  const done = marks.has(markOf(entry, file));
  return html`<details class="fc-file${done ? ' fc-reviewed' : ''}" data-checkpoint="${entry.number}" data-path="${file.path}">
    <summary class="pk-row"><span class="fc-path pk-mono pk-grow">${file.path}</span>
      ${when(file.changed_at)}
      <span class="pk-badge">${file.change}</span>
      ${!done && html`<button class="pk-btn pk-btn--sm pk-btn--ghost" data-revert-file="${entry.number}"
        title="Undo this checkpoint's change of this file alone">${icon('undo-2', { size: 'sm' })} Revert</button>`}
      ${reviewButton(html`data-review="${markOf(entry, file)}"`, done, 'Reviewed')}</summary>
    <div class="fc-diff" style="margin-top: var(--space-2)"><div class="pk-skeleton" style="height: 3lh"></div></div>
  </details>`;
}

// A checkpoint that recorded no file, only counted calls that may have changed some (a coding CLI's run): one
// line. /rewind <n> still takes its number; here it has nothing to show or put back of its own.
function countedRow(entry, id) {
  const [whole, partly] = untrackedParts(entry.untracked || []);
  // only calls recorded except for a part: nothing went unseen that the look could have seen -- no warning
  const said = [whole.length && html`only counted: <span class="pk-mono">${whole.join(', ')}</span>`,
    partly.length && html`changed only what the look does not see: <span class="pk-mono">${partly.join(', ')}</span>`].filter(Boolean);
  return html`<div class="fc-counted pk-row pk-muted" data-session="${id}" data-card="${entry.number}"
    title="${whole.length ? 'Not recorded, only counted: they may have changed files too' : 'Nothing here a rewind could put back'}">
    ${icon(whole.length ? 'triangle-alert' : 'info', { size: 'sm' })}
    <span class="pk-grow">Checkpoint ${entry.number} · ${where(entry)} · ${said.map((part, index) => html`${index ? ' · ' : ''}${part}`)}</span>
    ${when(entry.changed_at)}</div>`;
}

function checkpointCard(entry, id, marks) {
  if (!entry.files.length) return countedRow(entry, id);
  const all = entry.files.every((file) => marks.has(markOf(entry, file)));
  return html`<div class="pk-card${all ? ' fc-all-reviewed' : ''}" data-session="${id}" data-card="${entry.number}">
    <div class="pk-card-head pk-row">
      <h3 class="pk-card-title pk-grow">Checkpoint ${entry.number} <span class="pk-muted">· ${where(entry)}</span></h3>
      ${when(entry.changed_at)}
      ${reviewButton(html`data-review-all="${entry.number}"`, all, 'All reviewed')}
      ${!all && html`<button class="pk-btn pk-btn--sm" data-revert="${entry.number}"
        title="Undo this checkpoint's changes alone -- the later ones stay">${icon('undo-2', { size: 'sm' })} Revert</button>
      <button class="pk-btn pk-btn--sm pk-btn--danger" data-rewind="${entry.number}"
        title="Put every file back as it was before this checkpoint -- the later ones' changes too">${icon('rotate-ccw', { size: 'sm' })} Restore to here</button>`}
    </div>
    ${entry.question && html`<p class="pk-muted" style="margin:0 0 var(--space-2)">${entry.question}</p>`}
    <div class="pk-stack">${entry.files.map((file) => fileRow(entry, file, marks))}</div>
    ${untrackedLine(entry.untracked || [])}
  </div>`;
}

// Newest first -- the latest change is the one looked at most -- under the span it falls in.
function groups(entries, id, marks) {
  const by = new Map();
  for (const entry of [...entries].reverse()) {
    const name = groupOf(entry.changed_at);
    if (!by.has(name)) by.set(name, []);
    by.get(name).push(entry);
  }
  return GROUPS.filter((name) => by.has(name)).map((name) => html`<section class="fc-group pk-stack">
    <h2 class="pk-section-title">${name}</h2>
    ${by.get(name).map((entry) => checkpointCard(entry, id, marks))}
  </section>`);
}

// An unchanged drawing draws nothing, so open diffs stay as they are; a changed one opens again what was open
// (and asks for those diffs anew).
function draw() {
  const key = (d) => `${d.closest('[data-session]').dataset.session}\n${d.dataset.checkpoint}\n${d.dataset.path}`;
  const open = new Set([...view.querySelectorAll('details.fc-file[open]')].map(key));
  if (update(view, groups(shown.entries, shown.id, marksFor(shown.id)))) {
    for (const details of view.querySelectorAll('details.fc-file')) {
      if (open.has(key(details))) details.open = true;
    }
  }
}

async function load() {
  const request = ++latest;  // switching fast: only the newest answer is shown
  const id = session.shown;
  if (!id) {
    shown = { id: null, entries: [] };
    update(view, empty('No session', 'The panel follows the session that is open in the chat.'));
    setTitle('Files');
    return;
  }
  let data;
  try {
    data = await api(`/chat/checkpoints?${query({ session_id: id })}`, { quiet: true });
  } catch (error) {
    if (request !== latest) return;
    const text = error instanceof ApiError && error.status === 404 ? 'This session no longer exists' : error.message;
    shown = { id: null, entries: [] };
    setTitle('Files');
    update(view, html`<div class="pk-empty">${icon('circle-alert')}<div class="pk-empty-title">${text}</div></div>`);
    return;
  }
  if (request !== latest) return;
  const entries = data.checkpoints || [];
  setTitle(entries.length ? `Files · ${entries.length}` : 'Files');
  shown = { id, entries };
  if (!entries.length) {
    update(view, empty('No file changes', 'Nothing the agent changed through its file tools is recorded for this session.'));
    return;
  }
  draw();
}

function toggleMarks(id, wanted, on) {
  const marks = marksFor(id);
  for (const mark of wanted) {
    if (on) marks.add(mark);
    else marks.delete(mark);
  }
  setMarks(id, marks);
  draw();
}

async function showDiff(details) {
  const box = details.querySelector('.fc-diff');
  // The session the card was drawn for, not the one the chat shows now: they differ for a moment after a switch.
  const id = details.closest('[data-session]').dataset.session;
  let data;
  try {
    data = await api(`/chat/checkpoints/diff?${query({ session_id: id, checkpoint: details.dataset.checkpoint, path: details.dataset.path })}`,
      { quiet: true });
  } catch (error) {
    render(box, html`<p class="pk-text--danger" style="margin:0">${error.message}</p>`);
    delete details.dataset.loaded;   // opened again, it asks again
    return;
  }
  const note = data.note && html`<p class="pk-muted" style="margin:0 0 var(--space-2)">${data.note}</p>`;
  if (data.diff === null || data.diff === undefined) {
    render(box, note);
    return;
  }
  render(box, html`${note}
    ${data.diff ? html`<pre class="pk-code pk-code--lines pk-code--full">${diffLines(data.diff)}</pre>`
      : html`<p class="pk-muted" style="margin:0">Same content before and after.</p>`}
    ${data.truncated && html`<p class="pk-muted" style="margin:0">The diff is cut here.</p>`}`);
}

// The three ways back, as /chat/rewind takes them: to before a checkpoint, one checkpoint alone, one file of it.
const WAYS = {
  rewind: (number) => ({
    body: { checkpoint: number },
    question: `Put every file the agent changed since checkpoint ${number} back as it was then? The conversation stays as it is.`,
    title: 'Restore to here', label: 'Restore to here', since: 'changed outside the agent' }),
  revert: (number) => ({
    body: { checkpoint: number, only: true },
    question: `Undo the changes of checkpoint ${number} alone? The checkpoints after it and the conversation stay as they are.`,
    title: 'Revert checkpoint', label: 'Revert', since: 'changed since -- by a later checkpoint or outside the agent' }),
  file: (number, path) => ({
    body: { checkpoint: number, path },
    question: `Undo the change of checkpoint ${number} to ${path}? Its other files and the later checkpoints stay as they are.`,
    title: 'Revert file', label: 'Revert', since: 'changed since -- by a later checkpoint or outside the agent' }),
};

async function rewind(id, way) {
  const ok = await confirm(way.question, { title: way.title, confirmLabel: way.label, danger: true });
  if (!ok) return;
  let overwrite = false;
  for (;;) {
    try {
      const report = await api('/chat/rewind', { method: 'POST', json: { session_id: id, ...way.body, overwrite }, quiet: true });
      toast(report.text || 'Files put back', { kind: 'ok' });
      break;
    } catch (error) {
      // Only a refusal that files stood in the way of is offered an overwrite -- a run still going is
      // refused with a 409 as well, and overwriting would not wait for it, only skip the files' check.
      const files = error instanceof ApiError && error.status === 409 && error.headers.get('X-Rewind-Refusal') === 'files';
      if (!files || overwrite) {
        toast(error.message, { kind: 'error' });
        break;
      }
      if (!await confirm(`${error.message}\n\nPut back what can be, overwriting what was ${way.since}?`,
        { title: 'Files stand in the way', confirmLabel: 'Overwrite', danger: true })) break;
      overwrite = true;
    }
  }
  load();
}

view.addEventListener('toggle', (event) => {
  const details = event.target;
  if (details.matches?.('details.fc-file') && details.open && !details.dataset.loaded) {
    details.dataset.loaded = '1';
    showDiff(details);
  }
}, true);
view.addEventListener('click', (event) => {
  const id = event.target.closest('[data-session]')?.dataset.session;
  const one = event.target.closest('[data-review]');
  if (one) {
    // In the file's summary, and a button there is its own: the click neither opens nor closes the diff.
    toggleMarks(id, [one.dataset.review], one.getAttribute('aria-pressed') !== 'true');
    return;
  }
  const all = event.target.closest('[data-review-all]');
  if (all) {
    const entry = shown.entries.find((e) => String(e.number) === all.dataset.reviewAll);
    if (entry) toggleMarks(id, entry.files.map((file) => markOf(entry, file)), all.getAttribute('aria-pressed') !== 'true');
    return;
  }
  const back = event.target.closest('[data-rewind], [data-revert], [data-revert-file]');
  if (!back) return;
  if (back.dataset.revertFile) {
    rewind(id, WAYS.file(Number(back.dataset.revertFile), back.closest('details.fc-file').dataset.path));
  } else if (back.dataset.revert) {
    rewind(id, WAYS.revert(Number(back.dataset.revert)));
  } else {
    rewind(id, WAYS.rewind(Number(back.dataset.rewind)));
  }
});
hideSwitch.checked = readStored(HIDE_KEY, false) === true;
view.classList.toggle('fc-hide-reviewed', hideSwitch.checked);
hideSwitch.addEventListener('change', () => {
  view.classList.toggle('fc-hide-reviewed', hideSwitch.checked);
  writeStored(HIDE_KEY, hideSwitch.checked);
});
document.addEventListener('refresh', load);
session.onChange(load);
load();
