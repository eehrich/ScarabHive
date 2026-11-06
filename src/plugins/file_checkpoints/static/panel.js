// Files panel: what the agent changed in files, per checkpoint (file_checkpoints), as diffs -- and /rewind <n>.
// Follows the shell's session; ?session_id= pins it (the chat's context link) and <pk-session> says so.
import { api, ApiError, html, render, update, icon, session, confirm, toast, diffLines, setTitle } from '/static/kit/panel-kit.js';

const view = document.getElementById('filesView');
let latest = 0;

const empty = (title, text = '') => html`<div class="pk-empty">${icon('history')}<div class="pk-empty-title">${title}</div>
  ${text && html`<div>${text}</div>`}</div>`;

const query = (params) => new URLSearchParams(params).toString();

function where(entry) {
  if (entry.dropped) return `a turn /undo dropped, after turn ${entry.after_turn}`;
  return `before turn ${entry.turn}`;
}

function untrackedLine(untracked) {
  if (!untracked.length) return '';
  const calls = untracked.map((u) => `${u.tool} ×${u.count}`).join(', ');
  return html`<p class="pk-muted" style="margin:0">${icon('triangle-alert', { size: 'sm' })}
    Not recorded, only counted (they may have changed files too): <span class="pk-mono">${calls}</span></p>`;
}

function fileRow(entry, file) {
  return html`<details class="fc-file" data-checkpoint="${entry.number}" data-path="${file.path}">
    <summary class="pk-row"><span class="pk-mono pk-grow">${file.path}</span>
      <span class="pk-badge">${file.change}</span></summary>
    <div class="fc-diff" style="margin-top: var(--space-2)"><div class="pk-skeleton" style="height: 3lh"></div></div>
  </details>`;
}

function checkpointCard(entry, id) {
  return html`<div class="pk-card" data-session="${id}">
    <div class="pk-card-head pk-row">
      <h3 class="pk-card-title pk-grow">Checkpoint ${entry.number} <span class="pk-muted">· ${where(entry)}</span></h3>
      <button class="pk-btn pk-btn--sm pk-btn--danger" data-rewind="${entry.number}">${icon('rotate-ccw', { size: 'sm' })} Put back</button>
    </div>
    ${entry.question && html`<p class="pk-muted" style="margin:0 0 var(--space-2)">${entry.question}</p>`}
    <div class="pk-stack">${entry.files.map((file) => fileRow(entry, file))}</div>
    ${untrackedLine(entry.untracked || [])}
  </div>`;
}

async function load() {
  const request = ++latest;  // switching fast: only the newest answer is shown
  const id = session.shown;
  if (!id) {
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
    setTitle('Files');
    update(view, html`<div class="pk-empty">${icon('circle-alert')}<div class="pk-empty-title">${text}</div></div>`);
    return;
  }
  if (request !== latest) return;
  const entries = data.checkpoints || [];
  setTitle(entries.length ? `Files · ${entries.length}` : 'Files');
  if (!entries.length) {
    update(view, empty('No file changes', 'Nothing the agent changed through its file tools is recorded for this session.'));
    return;
  }
  // Newest first: the latest change is the one looked at most. An unchanged answer draws nothing, so open
  // diffs stay as they are; a changed one opens again what was open (and asks for those diffs anew).
  const key = (d) => `${d.closest('[data-session]').dataset.session}\n${d.dataset.checkpoint}\n${d.dataset.path}`;
  const open = new Set([...view.querySelectorAll('details.fc-file[open]')].map(key));
  if (update(view, [...entries].reverse().map((entry) => checkpointCard(entry, id)))) {
    for (const details of view.querySelectorAll('details.fc-file')) {
      if (open.has(key(details))) details.open = true;
    }
  }
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

async function rewind(id, number) {
  const ok = await confirm(
    `Put every file the agent changed since checkpoint ${number} back as it was then? The conversation stays as it is.`,
    { title: 'Put files back', confirmLabel: 'Put back', danger: true });
  if (!ok) return;
  let overwrite = false;
  for (;;) {
    try {
      const report = await api('/chat/rewind', { method: 'POST', json: { session_id: id, checkpoint: number, overwrite }, quiet: true });
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
      if (!await confirm(`${error.message}\n\nPut back what can be, overwriting the changes made outside the agent?`,
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
  const button = event.target.closest('[data-rewind]');
  if (button) rewind(button.closest('[data-session]').dataset.session, Number(button.dataset.rewind));
});
document.addEventListener('refresh', load);
session.onChange(load);
load();
