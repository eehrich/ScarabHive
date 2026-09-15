// Session panel: what the active session carries -- messages, token estimate, context variables.
// Follows the shell's session; ?session_id= pins it (the chat's context link).
import { api, ApiError, html, render, icon, session, jsonView, navigate, setTitle } from '/static/kit/panel-kit.js';

const view = document.getElementById('sessionView');
const pinned = new URLSearchParams(location.search).get('session_id');
let latest = 0;

const EMPTY = html`<div class="pk-empty">${icon('message-square')}<div class="pk-empty-title">No session</div>
  <div>The panel follows the session that is open in the chat.</div></div>`;

function when(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function counts(messages) {
  const out = { user: 0, assistant: 0, tool: 0, toolCalls: 0, images: 0, tokens: 0, estimated: 0 };
  for (const message of messages) {
    if (message.role in out) out[message.role] += 1;
    out.toolCalls += (message.tool_calls || []).length;
    out.images += (message.images || []).length;
    // estimated_tokens is computed server-side (session_service); the panel does no guessing of its own
    if (typeof message.estimated_tokens === 'number') {
      out.tokens += message.estimated_tokens;
      out.estimated += 1;
    }
  }
  return out;
}

function varsTable(vars) {
  const entries = Object.entries(vars || {});
  if (!entries.length) return html`<p class="pk-muted" style="margin:0">None</p>`;
  return html`<dl class="pk-kv">${entries.map(([key, value]) => html`
    <dt class="pk-mono">${key}</dt>
    <dd>${value !== null && typeof value === 'object' ? jsonView(value) : html`<span class="pk-mono">${String(value)}</span>`}</dd>`)}</dl>`;
}

function descendants(tree) {
  return tree.map((node) => html`
    <details class="pk-card" style="padding: var(--space-3)">
      <summary class="pk-row"><span class="pk-mono">${node.agent_name}</span>
        <span class="pk-muted">${Object.keys(node.context_vars || {}).length} vars</span></summary>
      <div class="pk-stack" style="margin-top: var(--space-2)">
        ${varsTable(node.context_vars)}
        ${(node.children || []).length ? descendants(node.children) : ''}
      </div>
    </details>`);
}

async function load() {
  const request = ++latest;  // switching fast: only the newest answer is shown
  const id = pinned || session.id;
  if (!id) {
    render(view, EMPTY);
    setTitle('Session');
    return;
  }
  let data;
  try {
    data = await api(`/api/sessions/${encodeURIComponent(id)}`, { quiet: true });
  } catch (error) {
    if (request !== latest) return;
    const text = error instanceof ApiError && error.status === 404 ? 'This session no longer exists' : `Could not be loaded: ${error.message}`;
    render(view, html`<div class="pk-empty">${icon('circle-alert')}<div class="pk-empty-title">${text}</div></div>`);
    setTitle('Session');
    return;
  }
  if (request !== latest) return;
  const c = counts(data.messages || []);
  setTitle(data.title || 'Session');
  render(view, html`
    <div class="pk-card">
      <div class="pk-card-head"><h3 class="pk-card-title">${data.title || 'Untitled'}</h3>
        ${pinned ? html`<span class="pk-grow"></span>
          <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-act="follow" title="Show the session open in the chat instead">
            ${icon('pin-off', { size: 'sm' })} Follow the chat</button>` : ''}</div>
      <dl class="pk-kv">
        <dt>Agent</dt><dd>${data.agent_name || '—'}</dd>
        <dt>Model profile</dt><dd>${data.llm_profile || '—'}</dd>
        <dt>Created</dt><dd>${when(data.created_at)}</dd>
        <dt>Updated</dt><dd>${when(data.updated_at)}</dd>
        <dt>Id</dt><dd class="pk-mono">${data.session_id}</dd>
      </dl>
    </div>
    <div class="pk-stats">
      <div class="pk-stat"><div class="pk-stat-label">User messages</div><div class="pk-stat-value">${c.user}</div></div>
      <div class="pk-stat"><div class="pk-stat-label">Assistant messages</div><div class="pk-stat-value">${c.assistant}</div></div>
      <div class="pk-stat"><div class="pk-stat-label">Tool calls</div><div class="pk-stat-value">${c.toolCalls}</div></div>
      <div class="pk-stat"><div class="pk-stat-label">Estimated tokens</div>
        <div class="pk-stat-value">${c.estimated ? c.tokens.toLocaleString() : '—'}</div></div>
    </div>
    <div class="pk-card"><div class="pk-card-head"><h3 class="pk-card-title">Context variables</h3></div>
      ${varsTable(data.context_vars)}</div>
    ${(data.descendants_context_vars || []).length ? html`
      <div class="pk-stack"><h3 class="pk-card-title">Sub-sessions</h3>${descendants(data.descendants_context_vars)}</div>` : ''}`);
}

view.addEventListener('click', (event) => {
  if (!event.target.closest('[data-act="follow"]')) return;
  navigate(location.pathname);  // the shell forgets the pinned path, a reload keeps following
  location.replace(location.pathname);
});
document.addEventListener('refresh', load);
if (!pinned) session.onChange(load);
load();
