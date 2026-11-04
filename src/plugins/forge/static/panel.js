// The Forge panel: the repositories and why one is left out, the policy, the sessions the webhook's news goes to,
// and what became of each webhook event. Every user's -- the route is for admins; a user sees their own with /forge.
import { api, copyText, html, icon, localTime, openSession, pluginBase, render, stat, toast } from '/static/kit/panel-kit.js';

const BASE = pluginBase(import.meta.url);
const VIEWER = document.querySelector('[data-viewer]')?.dataset.viewer || '';
const $ = (id) => document.getElementById(id);

const when = (utc) => localTime(utc, { relative: true });  // stored UTC without a zone, as the kit reads it
const yes = (on, label) => html`<span class="pk-badge ${on ? 'pk-badge--ok' : ''}">${label}${on ? '' : ': off'}</span>`;
const none = (text) => html`<p class="pk-muted">${text}</p>`;

const OUTCOME_KIND = [[/^error/, 'pk-badge--danger'], [/^(dropped|not started|waits)/, 'pk-badge--warn']];
function outcomeBadge(outcome) {
  const kind = OUTCOME_KIND.find(([pattern]) => pattern.test(outcome))?.[1];
  return kind ? html`<span class="pk-badge ${kind}">${outcome}</span>` : outcome;
}

function table(head, rows) {
  return html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr>${head.map((h) => html`<th>${h}</th>`)}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

// A session opens in the chat only for its own user: the chat shows only the viewer's sessions.
function sessionCell(user, session) {
  if (!session) return '';
  const own = !VIEWER || user === VIEWER;
  return html`<span class="pk-mono">${session}</span>
    ${own ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-session="${session}"
      title="Open the session in the chat" aria-label="Open the session in the chat">${icon('message-square', { size: 'sm' })}</button>` : ''}`;
}

function draw(s) {
  const hook = s.webhook;
  render($('stats'), [
    stat('Repositories', s.repos.length),
    stat('Left out', s.left_out.length),
    stat('Sessions at work', !hook ? '–' : hook.error ? '?' : s.work.length),
    stat('Started · woken (1 h)', !hook ? 'off' : hook.error ? '?' : `${hook.last_hour.new} · ${hook.last_hour.wake}`),
  ]);

  render($('repos'), s.repos.length || s.left_out.length ? table(['Name', 'Platform', 'Project', 'Merge', 'CI required', ''], [
    ...s.repos.map((r) => html`<tr><td>${r.name}</td><td>${r.provider} · ${r.host}</td><td class="pk-mono">${r.project}</td>
      <td>${yes(r.allow_merge, 'merge')}</td><td>${yes(r.require_ci, 'CI')}</td>
      <td>${r.tls_verify ? '' : html`<span class="pk-badge pk-badge--danger" title="Certificate checks are off for this host">TLS off</span>`}</td></tr>`),
    ...s.left_out.map(({ repo, why }) => html`<tr><td>${repo}</td><td colspan="5"><span class="pk-badge pk-badge--warn">left out</span> ${why}</td></tr>`),
  ]) : none('No repository configured: hosts and repos go under plugins.servers.forge in config/plugins.yaml, the tokens into config/secrets.env.'));

  render($('policy'), html`<dl class="pk-kv">
    <dt>Allowed users</dt><dd>${s.allowed_users.length ? s.allowed_users.join(', ')
      : html`<span class="pk-badge pk-badge--warn">nobody</span> -- forge refuses every user (allowed_users)`}</dd>
    <dt>Branches</dt><dd>pushes only under <span class="pk-mono">${s.branch_prefix}</span>, never forced</dd>
    <dt>Merge method</dt><dd>${s.merge_method}</dd>
    <dt>Webhook</dt><dd>${hook ? html`${yes(hook.route, 'route')} new work runs as <b>${hook.user}</b> with agent ${hook.agent};
      ${hook.merge ? 'it may merge' : 'a person merges'}; at most ${hook.max_new_per_hour} new sessions and
      ${hook.max_wakes_per_hour} wakes an hour` : 'off (no webhook.user)'}</dd></dl>`);

  if (!hook) {
    render($('work'), none('The webhook is off: no session is bound to a ticket.'));
    render($('outcomes'), none('The webhook is off.'));
    return;
  }
  if (hook.error) {  // the store could not be read: say so, never "nothing happened"
    const broken = html`<p><span class="pk-badge pk-badge--danger">store unreadable</span> ${hook.error}</p>`;
    render($('work'), broken);
    render($('outcomes'), broken);
    return;
  }
  render($('work'), s.work.length ? table(['Works on', 'User', 'Session', 'Waiting', 'Since'], s.work.map((w) => html`<tr>
    <td>${w.ref}</td><td>${w.user_id}</td><td>${sessionCell(w.user_id, w.session_id)}</td>
    <td class="pk-num">${w.waiting ? html`<span class="pk-badge pk-badge--info" title="News the session takes with its next run">${w.waiting}</span>` : '0'}</td>
    <td>${when(w.since)}</td></tr>`)) : none('No session works on a ticket, request or branch yet.'));
  render($('outcomes'), s.outcomes.length ? table(['When', 'Event', 'By', 'Outcome', 'Session'], s.outcomes.map((o) => html`<tr>
    <td>${when(o.at)}</td><td>${o.event}</td><td>${o.by ? `@${o.by}` : ''}</td>
    <td>${outcomeBadge(o.outcome)}</td>
    <td>${sessionCell(o.user_id, o.session_id)}</td></tr>`)) : none('No webhook event arrived yet.'));
}

async function load() {
  draw(await api(`${BASE}/status`));
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-open-session]');
  if (!button) return;
  const id = button.dataset.openSession;
  if (!openSession(id)) {  // a tab of its own: no chat beside it
    copyText(id).then((copied) => { if (copied) toast('No chat in this tab: pick the copied session in the chat.'); });
  }
});
document.addEventListener('refresh', load);
load();
