// Lessons Learned: the lessons the agents keep, of every agent; edited, searched, consolidated and cleaned up here.
import { api, html, render, icon, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const PAGE = 50;
const FIELDS = ['agent_name', 'category', 'title', 'content', 'status', 'source_type', 'priority', 'confidence'];
const STATUSES = { active: 'ok', draft: 'warn', inactive: '', archived: '' };
const SOURCES = { manual: 'Manual', cross_agent: 'Cross-agent', reflection: 'Reflection', auto: 'Auto' };
/** What an answer that still did its job could not do: a lesson stored, but out of the search index. */
const warn = (answer) => (answer?.warnings || []).forEach((text) => toast(text, { kind: 'warn' }));

/** The lessons shown. */
let lessons = [];
/** The search the lessons shown answer, '' for the plain list. */
let searched = '';
let page = 0;
let lastPage = 0;
let load = 0;
let busy = false;
/** The lesson the editor saves, null for a new one. */
let editing = null;
/** The tags as the editor showed them: a tag holding a comma would come back split. */
let shownTags = '';
/** The lessons the cleanup preview showed. */
let matched = [];
let previews = 0;

const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value.toLocaleString()}</div></div>`;
const number = (input) => (Number.isNaN(input.valueAsNumber) ? null : input.valueAsNumber);
const lessonUrl = (id, rest = '') => `${BASE}lessons/${encodeURIComponent(id)}${rest}`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const mine = ++load;
  busy = true;
  const agent = $('agentFilter').value;
  const status = $('statusFilter').value;
  const category = $('categoryFilter').value;
  const query = searched;
  const listing = new URLSearchParams({ sort_by: $('sortFilter').value, limit: PAGE, offset: page * PAGE });
  Object.entries({ agent_name: agent, status, category }).forEach(([key, value]) => value && listing.set(key, value));
  let stats;
  let listed;
  try {
    [stats, listed] = await Promise.all([
      api(`${BASE}stats${agent ? `?agent_name=${encodeURIComponent(agent)}` : ''}`, { quiet: true }),
      query
        ? api(`${BASE}lessons/search`, { method: 'POST', quiet: true,
          json: { query, agent_name: agent || null, status: status || null, category: category || null } })
        : api(`${BASE}lessons?${listing}`, { quiet: true }),
    ]);
  } catch (error) {
    if (mine === load) {  // nothing shown before stays, as if it were the answer to this
      render($('stats'), empty('circle-alert', 'Lessons could not be loaded', error.message));
      render($('lessons'), '');
      $('pager').hidden = true;
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // other filters were asked for since
  if (!query && page > 0 && !listed.lessons.length) {  // the page ran out under a delete: the last one left instead
    page = Math.max(0, Math.ceil(listed.total / PAGE) - 1);
    refresh();
    return;
  }
  lessons = query ? listed.results : listed.lessons;
  drawStats(stats);
  drawLessons(query);
  drawPager(listed, query);
}

function drawStats(stats) {
  const count = (status) => stats.by_status[status] || 0;
  render($('stats'), [
    stat('total', 'Lessons', stats.total),
    stat('active', 'Active', count('active')),
    stat('draft', 'Draft', count('draft')),
    stat('inactive', 'Inactive', count('inactive')),
    stat('archived', 'Archived', count('archived')),
    stat('agents', 'Agents', stats.agent_count),
  ]);
  // what is filtered for stays offered, also once no lesson has it any more
  fill($('agentFilter'), 'Every agent', stats.agents, $('agents'));
  fill($('categoryFilter'), 'Every category', Object.keys(stats.by_category));
}

function fill(select, every, names, list = null) {
  const current = select.value;
  const all = [...new Set([...names, current])].filter(Boolean).sort();
  render(select, html`<option value="">${every}</option>${all.map((name) => html`<option value="${name}">${name}</option>`)}`);
  select.value = current;
  if (list) render(list, all.map((name) => html`<option value="${name}"></option>`));
}

// --------------------------------------------------------------------- lessons

function row(lesson, query) {
  const button = (act, name, label) =>
    html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="${act}" title="${label}" aria-label="${label}">${icon(name, { size: 'sm' })}</button>`;
  const level = lesson.confidence >= 0.7 ? 'll-high' : lesson.confidence < 0.4 ? 'll-low' : '';
  return html`<tr data-lesson="${lesson.lesson_id}" data-status="${lesson.status}">
    <td class="ll-lesson" data-sort-value="${lesson.title}">
      <div class="ll-title">${lesson.title}</div>
      <div class="ll-content">${lesson.content}</div>
      <div class="pk-row ll-meta"><span class="pk-mono pk-muted">${lesson.lesson_id}</span>${lesson.tags.map((tag) => badge('', tag))}</div>
    </td>
    <td>${lesson.agent_name}</td>
    <td>${lesson.category}</td>
    <td class="pk-num">${lesson.priority}</td>
    <td class="pk-num ${level}" data-cell="confidence">${lesson.confidence.toFixed(2)}</td>
    <td>${badge(STATUSES[lesson.status], lesson.status)}</td>
    <td class="pk-num">${lesson.evidence_count}</td>
    <td class="pk-num">${lesson.application_count}</td>
    <td>${SOURCES[lesson.source_type]}</td>
    ${query ? html`<td class="pk-num" data-cell="similarity">${lesson.similarity.toFixed(3)}</td>` : ''}
    <td class="ll-actions">
      ${lesson.status === 'draft' ? button('activate', 'check', 'Activate') : ''}
      ${button('edit', 'pencil', 'Edit')}
      ${button('delete', 'trash-2', 'Delete')}
    </td>
  </tr>`;
}

function drawLessons(query) {
  if (!lessons.length) {
    render($('lessons'), empty('graduation-cap', query ? 'No lesson matches the search' : 'No lessons found'));
    return;
  }
  // only the lessons found sort here: the list is paged, and sorted by the select, on the server
  render($('lessons'), html`<div class="pk-table-wrap"><table class="pk-table"${query ? html` data-pk-sort="found"` : ''}>
    <thead><tr>
      <th>Lesson</th><th>Agent</th><th>Category</th><th class="pk-num" title="Priority">Prio.</th>
      <th class="pk-num" title="Confidence">Conf.</th><th>Status</th><th class="pk-num" title="Evidence">Ev.</th>
      <th class="pk-num" title="Applications">App.</th><th>Source</th>${query ? html`<th class="pk-num" aria-sort="descending">Similarity</th>` : ''}<th></th>
    </tr></thead>
    <tbody>${lessons.map((lesson) => row(lesson, query))}</tbody>
  </table></div>`);
}

function drawPager(listed, query) {
  const paged = !query && listed.total > PAGE;
  lastPage = Math.max(0, Math.ceil(listed.total / PAGE) - 1);
  $('pager').hidden = !query && !paged;
  $('previous').hidden = !paged;
  $('next').hidden = !paged;
  $('previous').disabled = page === 0;
  $('next').disabled = (page + 1) * PAGE >= listed.total;
  $('range').textContent = query
    ? `${listed.count} found for “${query}”`
    : `${page * PAGE + 1}–${page * PAGE + lessons.length} of ${listed.total}`;
}

async function act(button) {
  const id = button.closest('tr').dataset.lesson;
  const action = button.dataset.act;
  if (action === 'edit') {
    const lesson = await api(lessonUrl(id)).catch(() => null);
    if (lesson) openEditor(lesson);
    else refresh();  // shown by api(): gone meanwhile, most likely
    return;
  }
  const lesson = lessons.find((one) => one.lesson_id === id);
  if (action === 'delete' && !await confirm(`Delete the lesson “${lesson.title}”?`,
    { title: 'Delete lesson', confirmLabel: 'Delete', danger: true })) return;
  button.disabled = true;  // until the row is drawn anew: a second click would be refused as out of date
  // api() shows a failure; loaded anew either way, as a refusal means the row was out of date
  await api(lessonUrl(id, action === 'activate' ? '/activate' : ''), { method: action === 'activate' ? 'POST' : 'DELETE' })
    .catch(() => {});
  refresh();
}

// ---------------------------------------------------------------------- editor

function openEditor(lesson = null) {
  const fields = $('editorForm').elements;
  editing = lesson?.lesson_id ?? null;
  $('editorTitle').textContent = lesson ? `Edit ${lesson.lesson_id}` : 'New lesson';
  const values = lesson ?? { agent_name: $('agentFilter').value, category: 'general', title: '', content: '',
    status: 'active', source_type: 'manual', priority: 5, confidence: 0.8, tags: [] };
  FIELDS.forEach((name) => { fields[name].value = values[name]; });
  fields.tags.value = values.tags.join(', ');
  shownTags = fields.tags.value;
  $('editor').showModal();
}

async function save(event) {
  event.preventDefault();
  const fields = event.target.elements;
  const lesson = Object.fromEntries(FIELDS.map((name) => [name, fields[name].value]));  // the server takes numbers as text
  if (fields.tags.value !== shownTags) lesson.tags = fields.tags.value.split(',');  // trimmed and emptied out by the store
  $('save').disabled = true;
  try {
    warn(await api(editing ? lessonUrl(editing) : `${BASE}lessons`, { method: editing ? 'PUT' : 'POST', json: lesson }));
    $('editor').close();
    refresh();
  } catch {
    // shown by api(); the editor stays open with what was typed
  } finally {
    $('save').disabled = false;
  }
}

// ----------------------------------------------------------------- consolidate

function openConsolidator() {
  $('consolidateForm').reset();
  $('consolidateForm').elements.agent_name.value = $('agentFilter').value;
  render($('consolidateResult'), '');
  $('consolidator').showModal();
}

function progress(message, percent) {
  $('consolidatePhase').textContent = message;
  $('consolidatePercent').textContent = `${percent}%`;
  $('consolidateBar').value = percent;
}

/** The last line of the stream (the result or the error), after showing the progress before it. */
async function outcome(response) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return null;
    const lines = (buffer + value).split('\n');
    buffer = lines.pop();
    for (const line of lines.filter((one) => one.trim())) {
      const message = JSON.parse(line);
      if (message.type !== 'progress') return message;
      progress(message.message, message.percent);
    }
  }
}

function consolidated(result) {
  const ids = (list) => html`<span class="pk-mono">${list.join(', ')}</span>`;
  const detail = (item) => {
    if (item.action === 'merged' || item.action === 'would_merge') {
      const merged = item.action === 'merged';
      return html`<li>${badge(merged ? 'ok' : 'info', merged ? 'Merged' : 'Would merge')} <strong>${item.merged_title}</strong>:
        keeps ${ids([item.primary_id])}, ${merged ? 'deleted' : 'would delete'} ${ids(item.deleted)}
        ${(item.warnings || []).map((text) => html`<div>${badge('warn', 'Not re-indexed')} ${text}</div>`)}</li>`;
    }
    if (item.action === 'skipped') return html`<li>${badge('warn', 'Skipped')} ${ids(item.lessons)}: ${item.reason}</li>`;
    return html`<li>${badge('', 'Kept separate')} ${ids(item.lessons)}</li>`;
  };
  // agents whose lessons could not be compared: "no similar lessons" would be a verdict nobody made
  const unscanned = (result.warnings || []).map((text) => html`<li>${badge('warn', 'Not scanned')} ${text}</li>`);
  return html`<div class="pk-stack">
    <dl class="pk-kv">
      <dt>Run</dt><dd>${result.dry_run ? 'Dry run, nothing changed' : 'Merged'}</dd>
      <dt>Agents</dt><dd>${result.agents_processed}</dd>
      <dt>Clusters found</dt><dd>${result.clusters_found}</dd>
      <dt>${result.dry_run ? 'Lessons that would go' : 'Lessons merged away'}</dt><dd data-cell="merged">${result.lessons_merged}</dd>
      <dt>Clusters skipped</dt><dd>${result.clusters_skipped}</dd>
    </dl>
    ${result.details.length || unscanned.length ? html`<ul class="ll-details">${unscanned}${result.details.map(detail)}</ul>`
    : html`<div class="pk-muted">No similar lessons found.</div>`}
  </div>`;
}

async function consolidate(event) {
  event.preventDefault();
  const fields = event.target.elements;
  const body = { agent_name: fields.agent_name.value.trim() || null,
    similarity_threshold: number(fields.similarity_threshold), dry_run: fields.dry_run.checked };
  $('runConsolidate').disabled = true;
  render($('consolidateResult'), '');
  progress('Starting', 0);
  $('consolidateProgress').hidden = false;
  let last;
  try {
    last = await outcome(await api(`${BASE}consolidate`, { method: 'POST', json: body, raw: true }));
  } catch (error) {
    last = { type: 'error', message: error.message };
  } finally {
    $('runConsolidate').disabled = false;
    $('consolidateProgress').hidden = true;
  }
  if (last?.type === 'result') {
    render($('consolidateResult'), consolidated(last.data));
  } else {
    render($('consolidateResult'), html`<div class="pk-error">${last ? last.message : 'The run ended without a result.'}</div>`);
  }
  if (!body.dry_run) refresh();  // a merge that failed on a later group has changed the earlier ones
}

// --------------------------------------------------------------------- cleanup

function openCleaner() {
  $('cleanupForm').reset();
  $('cleanupForm').elements.agent_name.value = $('agentFilter').value;
  resetPreview();
  $('cleaner').showModal();
}

function resetPreview() {
  previews++;  // an answer still on its way belongs to filters no longer set
  $('deleteMatched').disabled = true;
  render($('cleanupResult'), '');
}

function criteria() {
  const fields = $('cleanupForm').elements;
  return { agent_name: fields.agent_name.value.trim() || null, status: fields.status.value || null,
    older_than_days: number(fields.older_than_days), max_evidence_count: number(fields.max_evidence_count),
    max_confidence: number(fields.max_confidence) };
}

function cleaned(result) {
  const count = result.dry_run ? result.matched_count : result.deleted_count;
  const said = `${count} ${count === 1 ? 'lesson' : 'lessons'} ${result.dry_run ? 'match' : 'deleted'}`;
  return html`<div class="pk-stack">
    <strong data-cell="count">${said}</strong>
    ${result.lessons.length ? html`<ul class="ll-details">${result.lessons.map((lesson) => html`<li data-lesson="${lesson.lesson_id}">
      <span class="pk-mono">${lesson.lesson_id}</span> ${lesson.title}
      <span class="pk-muted">(${lesson.agent_name}, ${lesson.status}, evidence ${lesson.evidence_count}, confidence ${lesson.confidence.toFixed(2)})</span></li>`)}</ul>` : ''}
  </div>`;
}

async function preview(event) {
  event.preventDefault();
  const mine = ++previews;
  $('preview').disabled = true;
  try {
    const found = await api(`${BASE}cleanup`, { method: 'POST', json: { ...criteria(), dry_run: true } });
    if (mine !== previews) return;
    matched = found.lessons.map((lesson) => lesson.lesson_id);
    render($('cleanupResult'), cleaned(found));
    $('deleteMatched').disabled = !matched.length;
  } catch {
    // shown by api()
  } finally {
    $('preview').disabled = false;
  }
}

async function deleteMatched() {
  // no more than the preview showed: a lesson that came to match since was never seen
  const body = { ...criteria(), dry_run: false, lesson_ids: matched };
  if (!await confirm(`Delete the ${matched.length === 1 ? 'lesson' : `${matched.length} lessons`} previewed?`,
    { title: 'Clean up lessons', confirmLabel: 'Delete', danger: true })) return;
  $('deleteMatched').disabled = true;
  try {
    const done = await api(`${BASE}cleanup`, { method: 'POST', json: body });
    resetPreview();
    render($('cleanupResult'), cleaned(done));
    refresh();
  } catch {
    $('deleteMatched').disabled = false;  // shown by api()
  }
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
['agentFilter', 'statusFilter', 'categoryFilter', 'sortFilter'].forEach((id) => $(id).addEventListener('change', () => {
  page = 0;
  refresh();
}));
$('searchForm').addEventListener('submit', (event) => {
  event.preventDefault();
  searched = $('query').value.trim();
  $('clearSearch').hidden = !searched;
  page = 0;
  refresh();
});
$('clearSearch').addEventListener('click', () => {
  $('query').value = '';
  searched = '';
  $('clearSearch').hidden = true;
  refresh();
});
const turn = (by) => (event) => {
  if (event.detail > 1) return;  // a double click turns one page
  page = Math.min(Math.max(page + by, 0), lastPage);  // clicks before the pager is drawn anew stay within the pages
  refresh();
};
$('previous').addEventListener('click', turn(-1));
$('next').addEventListener('click', turn(1));
$('lessons').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-act]');
  // not the second click of a double click: on a table drawn anew in between it would hit another row's button
  if (button && event.detail < 2) act(button);
});
$('create').addEventListener('click', () => openEditor());
$('editorForm').addEventListener('submit', save);
$('consolidate').addEventListener('click', openConsolidator);
$('consolidateForm').addEventListener('submit', consolidate);
$('cleanup').addEventListener('click', openCleaner);
$('cleanupForm').addEventListener('submit', preview);
$('cleanupForm').addEventListener('input', resetPreview);
$('deleteMatched').addEventListener('click', deleteMatched);
document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));

api(`${BASE}categories`)
  .then(({ categories }) => render($('categories'),
    [...new Set([...categories.map((one) => one.name), 'general'])].map((name) => html`<option value="${name}"></option>`)))
  .catch(() => {});  // shown by api(); the category is typed instead of picked
refresh();
