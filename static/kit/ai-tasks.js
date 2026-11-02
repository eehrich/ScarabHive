// AI tasks of a panel (agent_system/plugins/agent_tasks.py): where an agent may work, a person's choice per task --
// Person or AI, and which agent -- and the buttons that start it. The choices are a setting, not the panel's work:
// they sit in a dialog behind an "AI" button in the panel's toolbar, which says how many tasks an agent may do.
// A started agent is followed in the panel itself: a side sheet (not modal, the panel stays at hand) shows each run
// live -- what the agent says, the tools it calls and what they answer, its result -- and says when it is done.
// The chat stays one click away, never a must.
// Routes, relative to the plugin's base:
//   GET  {base}/ai                 -> {tasks: [{id, title, what, agent, default_agent, mode, target}], person_only: [...]}
//   POST {base}/ai/{task}          {mode?, agent?}
//   POST {base}/ai/{task}/run      {target?} -> {session_id, agent, state}
//   GET  {base}/ai/runs            -> {runs: [{session_id, agent, title, state, error, started, ended}]}
//   GET  {base}/ai/runs/{session}  -> the run with {steps: [{kind, name?, text}], answer}
//   POST {base}/ai/runs/{session}/reply  {text} -> the session goes on with a person's answer
// While a run works, the run events (run-events.js, GET /api/agent-runs/stream) say what it does now: its step in
// plain words, how long it has been at it, and when it has gone quiet. An open run shows its steps as a timeline
// (GET /api/agent-runs/{session}/events). Without the stream the sheet works as before, by asking.
import { api, confirm, errorText, html, icon, localTime, openSession, toast, update, withBusy } from '/static/kit/panel-kit.js';
import { FINAL, elapsed, followRuns, isLong, isSilent, liveRun, stepLabel } from '/static/kit/run-events.js';

const state = { tasks: new Map(), agents: [], base: '', box: null, button: null, onChange: null };
// the runs: the list, the run shown open, its last answer from the server, the poll timer
const runs = { list: [], open: '', shown: null, sheet: null, button: null, timer: 0, known: new Map(), drafts: new Map(), picks: new Map(),
  live: new Map(), timeline: null, stream: null, clock: 0 };
/** How often a running agent is asked after, in ms (tests set it shorter). */
export const POLL = { ms: 2000 };

// The words, in the language the page declares (<html lang>), as the kit's own.
const WORDS = {
  en: {
    mayDo: (agent) => `${agent} may do this`, possible: 'AI possible',
    possibleTitle: 'An agent could do this; click to switch it on',
    changeTitle: (agent) => `${agent} may do this; click to change who does it`,
    start: (agent) => `Start ${agent}; follow it here`, with: (label, agent) => `${label} with ${agent}`,
    byDefault: ' (default)', missing: ' -- not available', allOpen: 'All open',
    intro: 'Per task: a person does it, or an AI agent may -- then its button appears where the work is.',
    who: (title) => `Who does ${title}`, agentMode: 'AI agent', personMode: 'Person', agent: 'Agent', personOnly: 'Person only',
    note: 'An agent runs as you, in a session of its own; you follow it here, under Agent runs. What it records is marked as AI work; a person confirms it.',
    title: 'AI assistance: where an agent may work', close: 'Close',
    count: (on, all) => `An agent may do ${on} of ${all} task(s); choose here`, choose: 'Choose where an AI agent may work',
    setAi: (title, agent) => `${title}: ${agent} may do it`, setPerson: (title) => `${title}: done by a person`,
    started: (agent) => `${agent} started; follow it here`,
    runs: 'Agent runs', runsTitle: 'What the agents started here say and do', runsCount: (n) => `${n} running`,
    noRuns: 'No agent was started here yet.', task: 'Task', result: 'Result', inChat: 'Open in the chat',
    inChatTitle: 'The same session in the chat, to answer the agent or go on with it',
    states: { running: 'running', done: 'done', failed: 'failed', refused: 'refused', cancelled: 'cancelled', interrupted: 'interrupted' },
    interruptedWhy: 'The server stopped while the agent was at work; what it did up to then is shown. Answer to let it go on.',
    you: 'You', reply: 'Answer', replyLabel: (agent) => `Answer to ${agent}`, replyHint: 'Answer the agent or give it more to do …',
    replied: (agent) => `${agent} goes on with your answer`,
    ended: (agent, title) => `${agent} is done: ${title}`, endedBadly: (agent, state) => `${agent}: ${state}`,
    waiting: 'The agent is starting …', nothingSaid: 'The agent said nothing.', since: (time) => `since ${time}`,
    show: 'Show', hide: 'Hide', sessionIs: (id) => `Session ${id}: open it in the chat of ScarabHive`,
    step: (n) => `Step ${n}`, stepOf: (n, total) => `Step ${n} of ${total}`, working: 'At work',
    runningFor: (time) => `for ${time}`, silent: 'No sign of life for 30 s -- it may hang',
    long: 'Taking longer than usual -- still at work', steps: 'Steps', now: 'now',
    liveTitle: (agent, what) => `${agent}: ${what}`,
    cancel: 'Stop', cancelTitle: 'Stop the agent: what it changed so far stays, the rest is not done',
    cancelAsk: (agent) => `Stop ${agent}? What it has done so far stays; the rest is not done.`, cancelHead: 'Stop the agent',
    cancelling: (agent) => `${agent} is being stopped`,
    needsYou: 'needs you', needsCount: (n) => `${n} need${n === 1 ? 's' : ''} you`,
    checkHead: 'Done -- please check it', checkWhere: (where) => `What the agent prepared is under "${where}" in this panel, marked as AI work. Look it over and confirm or change it there.`,
    checked: 'Checked', checkedTitle: 'You have looked over the result: the run is done',
    handoverHead: 'Handed over to you: the agent did not get on', handoverWhat: 'Answer it below to let it try again, or go on yourself.',
    seen: 'Seen', seenTitle: 'You have seen it: it no longer asks for you',
    endedCheck: (agent, title) => `${agent} has finished: ${title} -- please check`, thanks: 'Noted',
    changedHead: 'What was changed?', riskHead: 'What could go wrong?', checkedHead: 'How was it checked?',
    ask: 'Ask the agent', askTitle: 'Ask about the result before you confirm it; the agent answers here',
    questionHead: (agent) => `${agent} has a question for you`, ownWords: 'Or in your own words',
    yourAnswer: 'Your answer', answer: 'Answer', answered: (agent) => `${agent} goes on with your answer`,
    questionWait: (minutes) => `The agent waits up to ${minutes} min; without an answer it decides itself and says what it assumed.`,
    questionGone: 'The question no longer waits (the agent went on or was stopped).', hasQuestion: 'has a question',
  },
  de: {
    mayDo: (agent) => `${agent} darf das übernehmen`, possible: 'KI möglich',
    possibleTitle: 'Ein Agent könnte das übernehmen; klicken zum Einschalten',
    changeTitle: (agent) => `${agent} darf das übernehmen; klicken, um zu ändern, wer es erledigt`,
    start: (agent) => `${agent} starten; hier mitlesen`, with: (label, agent) => `${label} mit ${agent}`,
    byDefault: ' (Standard)', missing: ' -- nicht verfügbar', allOpen: 'Alle offenen',
    intro: 'Pro Aufgabe: eine Person erledigt sie, oder ein KI-Agent darf es -- dann erscheint sein Knopf dort, wo die Arbeit ist.',
    who: (title) => `Wer erledigt ${title}`, agentMode: 'KI-Agent', personMode: 'Person', agent: 'Agent', personOnly: 'Nur Person',
    note: 'Ein Agent arbeitet in deinem Namen, in einer eigenen Session; du verfolgst ihn hier unter Agenten-Läufe. Was er einträgt, ist als KI-Arbeit markiert; eine Person bestätigt es.',
    title: 'KI-Unterstützung: wo ein Agent arbeiten darf', close: 'Schließen',
    count: (on, all) => `Ein Agent darf ${on} von ${all} Aufgabe(n) übernehmen; hier wählen`, choose: 'Wählen, wo ein KI-Agent arbeiten darf',
    setAi: (title, agent) => `${title}: ${agent} darf es übernehmen`, setPerson: (title) => `${title}: erledigt eine Person`,
    started: (agent) => `${agent} gestartet; hier mitlesen`,
    runs: 'Agenten-Läufe', runsTitle: 'Was die hier gestarteten Agenten sagen und tun', runsCount: (n) => `${n} läuft`,
    noRuns: 'Hier wurde noch kein Agent gestartet.', task: 'Auftrag', result: 'Ergebnis', inChat: 'Im Chat öffnen',
    inChatTitle: 'Dieselbe Session im Chat, um dem Agenten zu antworten oder weiterzumachen',
    states: { running: 'läuft', done: 'fertig', failed: 'fehlgeschlagen', refused: 'abgelehnt', cancelled: 'abgebrochen', interrupted: 'unterbrochen' },
    interruptedWhy: 'Der Server wurde beendet, während der Agent arbeitete; bis dahin Getanes steht hier. Antworten lässt ihn weitermachen.',
    you: 'Du', reply: 'Antworten', replyLabel: (agent) => `Antwort an ${agent}`, replyHint: 'Dem Agenten antworten oder ihm mehr aufgeben …',
    replied: (agent) => `${agent} macht mit deiner Antwort weiter`,
    ended: (agent, title) => `${agent} ist fertig: ${title}`, endedBadly: (agent, state) => `${agent}: ${state}`,
    waiting: 'Der Agent startet …', nothingSaid: 'Der Agent hat nichts gesagt.', since: (time) => `seit ${time}`,
    show: 'Zeigen', hide: 'Ausblenden', sessionIs: (id) => `Session ${id}: im Chat von ScarabHive öffnen`,
    step: (n) => `Schritt ${n}`, stepOf: (n, total) => `Schritt ${n} von ${total}`, working: 'Arbeitet',
    runningFor: (time) => `seit ${time}`, silent: 'Seit 30 s kein Lebenszeichen -- er hängt vielleicht',
    long: 'Dauert länger als üblich -- arbeitet weiter', steps: 'Schritte', now: 'jetzt',
    liveTitle: (agent, what) => `${agent}: ${what}`,
    cancel: 'Anhalten', cancelTitle: 'Den Agenten anhalten: was er bisher geändert hat, bleibt; der Rest unterbleibt',
    cancelAsk: (agent) => `${agent} anhalten? Was er bisher getan hat, bleibt; der Rest unterbleibt.`, cancelHead: 'Agent anhalten',
    cancelling: (agent) => `${agent} wird angehalten`,
    needsYou: 'braucht dich', needsCount: (n) => `${n} braucht dich`,
    checkHead: 'Fertig -- bitte prüfen', checkWhere: (where) => `Was der Agent vorbereitet hat, steht unter „${where}“ in diesem Panel, als KI-Arbeit markiert. Sieh es dir an und bestätige oder ändere es dort.`,
    checked: 'Geprüft', checkedTitle: 'Du hast das Ergebnis angesehen: der Lauf ist erledigt',
    handoverHead: 'An dich übergeben: der Agent kam nicht weiter', handoverWhat: 'Antworte unten, damit er es noch einmal versucht, oder mach selbst weiter.',
    seen: 'Gesehen', seenTitle: 'Du hast es gesehen: er fragt nicht mehr nach dir',
    endedCheck: (agent, title) => `${agent} ist fertig: ${title} -- bitte prüfen`, thanks: 'Vermerkt',
    changedHead: 'Was wurde geändert?', riskHead: 'Was kann schiefgehen?', checkedHead: 'Wie wurde es geprüft?',
    ask: 'Frage an den Agenten', askTitle: 'Vor dem Bestätigen nachfragen; der Agent antwortet hier',
    questionHead: (agent) => `${agent} hat eine Frage an dich`, ownWords: 'Oder in eigenen Worten',
    yourAnswer: 'Deine Antwort', answer: 'Antworten', answered: (agent) => `${agent} macht mit deiner Antwort weiter`,
    questionWait: (minutes) => `Der Agent wartet bis zu ${minutes} Min.; ohne Antwort entscheidet er selbst und sagt, was er angenommen hat.`,
    questionGone: 'Die Frage wartet nicht mehr (der Agent ist weiter oder wurde angehalten).', hasQuestion: 'hat eine Frage',
  },
};
const word = (key, ...args) => {
  const found = (WORDS[document.documentElement.lang.slice(0, 2).toLowerCase()] || WORDS.en)[key];
  return typeof found === 'function' ? found(...args) : found;
};
const AI = () => (document.documentElement.lang.slice(0, 2).toLowerCase() === 'de' ? 'KI' : 'AI');

/** The task's setting, or undefined. */
export const aiTask = (id) => state.tasks.get(id);
/** True when a person chose to let an agent do the task. */
export const aiOn = (id) => state.tasks.get(id)?.mode === 'ai';

/** A small marker beside work an agent may do: "AI" when it is on, "AI possible" when a person does it. It is a
 * button: it opens the choices at this task, so the way to switch an agent on is where the work is, not only in the
 * toolbar. */
export function aiMark(id) {
  const task = state.tasks.get(id);
  if (!task) return '';
  return task.mode === 'ai'
    ? html`<button type="button" class="pk-badge pk-badge--accent ai-mark" data-ai-mark="${id}" title="${word('changeTitle', task.agent)}">${icon('brain', { size: 'sm' })} ${AI()}</button>`
    : html`<button type="button" class="pk-badge ai-mark" data-ai-mark="${id}" title="${word('possibleTitle')}">${icon('brain', { size: 'sm' })} ${word('possible')}</button>`;
}

/** The choices, open at task `id`: its row in view and its AI choice focused. */
function choose(id) {
  if (!state.box.open) state.box.showModal();
  const row = state.box.querySelector(`[data-ai-task="${CSS.escape(id)}"]`);
  row?.scrollIntoView({ block: 'nearest' });
  row?.querySelector('input[value="ai"]')?.focus();
}

/** The button that starts the task's agent on `target`; nothing while a person does the task. */
export function aiButton(id, target = '', label = '') {
  const task = state.tasks.get(id);
  if (!task || task.mode !== 'ai') return '';
  return html`<button type="button" class="pk-btn pk-btn--sm" data-ai-run="${id}" data-ai-target="${target}"
    title="${word('start', task.agent)}">${icon('brain', { size: 'sm' })} ${word('with', label || task.title, task.agent)}</button>`;
}

function agentOptions(task) {
  const names = [...new Set([task.default_agent, task.agent, ...state.agents])];
  return names.map((name) => html`<option value="${name}" ${name === task.agent ? 'selected' : ''}>${name}${name === task.default_agent ? word('byDefault') : ''}${state.agents.length && !state.agents.includes(name) ? word('missing') : ''}</option>`);
}

function card(data) {
  return html`<div class="ai-tasks">
    <p class="pk-muted">${word('intro')}</p>
    <div class="ai-rows">${data.tasks.map((task) => html`<div class="ai-row" data-ai-task="${task.id}">
      <div class="ai-what"><strong>${task.title}</strong><div class="pk-muted">${task.what}</div></div>
      <div class="pk-segmented" role="radiogroup" aria-label="${word('who', task.title)}">
        ${['person', 'ai'].map((mode) => html`<label><input type="radio" name="ai-mode-${task.id}" value="${mode}"
          ${task.mode === mode ? 'checked' : ''}> ${mode === 'ai' ? word('agentMode') : word('personMode')}</label>`)}
      </div>
      <label class="pk-field ai-agent"><span class="pk-label">${word('agent')}</span>
        <select class="pk-select" name="ai-agent" ${task.mode === 'ai' ? '' : 'disabled'}>${agentOptions(task)}</select></label>
      <div>${!task.target || task.batch ? aiButton(task.id, '', task.target ? word('allOpen') : '') : ''}</div>
    </div>`)}
    ${data.person_only.map((item) => html`<div class="ai-row ai-person-only" data-person-only="${item.id}">
      <div class="ai-what"><strong>${item.title}</strong><div class="pk-muted">${item.why}</div></div>
      <span class="pk-badge pk-badge--warn">${icon('lock', { size: 'sm' })} ${word('personOnly')}</span></div>`)}
    </div>
    <p class="pk-muted ai-note">${word('note')}</p>
  </div>`;
}

/** The dialog the toolbar button opens; it stays open while choices are made, Close or Escape ends it. */
function frame() {
  const dialog = document.createElement('dialog');
  dialog.className = 'pk-dialog pk-dialog--wide ai-dialog';
  dialog.setAttribute('aria-labelledby', 'aiDialogTitle');
  dialog.innerHTML = `<div class="pk-dialog-head"><h2 class="pk-dialog-title" id="aiDialogTitle">${word('title')}</h2></div>
    <div class="pk-dialog-body ai-body"></div>
    <div class="pk-dialog-actions"><button type="button" class="pk-btn" data-ai-close>${word('close')}</button></div>`;
  dialog.querySelector('[data-ai-close]').addEventListener('click', () => dialog.close());
  return dialog;
}

/** The toolbar button: "AI" with the number of tasks an agent may do, before the panel's other tools. */
function toolbarButton(dialog) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pk-btn pk-btn--ghost ai-open';
  button.dataset.aiOpen = '';
  button.addEventListener('click', () => dialog.showModal());
  const toolbar = document.querySelector('.pk-page-header .pk-toolbar');
  if (toolbar) toolbar.prepend(button);
  return button;
}

function label(data) {
  const on = data.tasks.filter((t) => t.mode === 'ai').length;
  update(state.button, html`${icon('brain', { size: 'sm' })} ${AI()}${on ? html` <span class="pk-badge pk-badge--accent">${on}</span>` : ''}`);
  state.button.title = on ? word('count', on, data.tasks.length) : word('choose');
}

async function save(id, change) {
  try {
    const task = await api(`${state.base}/ai/${id}`, { method: 'POST', json: change });
    state.tasks.set(id, task);
    toast(task.mode === 'ai' ? word('setAi', task.title, task.agent) : word('setPerson', task.title), { kind: 'ok' });
  } catch {
    // api() has shown the error
  }
  await load();
  state.onChange?.();
}

// ── the runs: a side sheet beside the panel's work ───────────────────────

const TONE = { running: 'accent', done: 'ok', failed: 'danger', refused: 'danger', cancelled: 'warn', interrupted: 'warn' };

/** A run the agent paused to ask its person something (``ask_user`` in a panel run): the question it waits with. */
const questionOf = (run) => {
  const live = liveOf(run);
  return live?.state === 'needs_input' && live.payload?.question_id ? live.payload : null;
};
/** Whether a run waits for its person: a question while it runs, a result to check or a failure once it ended. */
const waitsForYou = (run) => Boolean(questionOf(run)) || (run.state !== 'running' && Boolean(liveOf(run)?.needsYou));

function stateBadge(run) {
  const needs = waitsForYou(run);
  return html`<span class="pk-badge pk-badge--${needs ? 'warn' : TONE[run.state] || 'accent'}" data-run-state="${run.state}"${needs ? html` data-run-needs` : ''}>${
    run.state === 'running' && !needs ? html`<span class="pk-spinner ai-run-spin" aria-hidden="true"></span> ` : ''}${
    questionOf(run) ? word('hasQuestion') : needs ? word('needsYou') : word('states')[run.state] || run.state}</span>`;
}

/** The run's live record from the run events, or null. */
const liveOf = (run) => runs.live.get(run.session_id) || null;

/** The page's other views that follow the runs too (a work list of several plugins): one stream for the page. */
const liveListeners = new Set();
/** `fn(liveRuns)` after every change the stream brings; answers the function that stops it. */
export function onLiveRuns(fn) {
  liveListeners.add(fn);
  return () => liveListeners.delete(fn);
}
/** The person's runs as the stream tells them, by run id (any plugin's, not only this panel's). */
export const liveRuns = () => runs.live;
const tellListeners = () => { for (const fn of liveListeners) fn(runs.live); };

// The three parts a run whose work a person checks ends its answer with (agent_tasks.REVIEW_SECTIONS), as cards.
const SECTIONS = [['### Changed', 'changedHead', 'pencil'], ['### Could go wrong', 'riskHead', 'triangle-alert'],
  ['### Checked', 'checkedHead', 'circle-check']];

/** An answer split into what comes before the three parts and the parts themselves; null without them. */
export function reviewParts(answer) {
  const text = String(answer || '');
  const at = SECTIONS.map(([head]) => text.indexOf(head));
  if (at.some((index) => index < 0)) return null;
  const order = at.map((index, i) => [index, i]).sort((a, b) => a[0] - b[0]);
  const parts = {};
  order.forEach(([index, i], n) => {
    const end = n + 1 < order.length ? order[n + 1][0] : text.length;
    parts[SECTIONS[i][1]] = text.slice(index + SECTIONS[i][0].length, end).trim();
  });
  return { before: text.slice(0, order[0][0]).trim(), parts };
}

function reviewView(parts) {
  return html`<div class="ai-review" data-run-review>${SECTIONS.map(([, key, symbol]) => html`<section class="ai-review-part" data-review-part="${key}">
    <div class="ai-review-head">${icon(symbol, { size: 'sm' })} <strong>${word(key)}</strong></div>
    <p class="ai-step-text">${parts.parts[key] || '--'}</p></section>`)}</div>`;
}

/** The agent's question, where the person answers it: the options to pick, and words of their own. What is picked
 * and typed is kept across redraws (a heartbeat every few seconds redraws the sheet). */
function questionView(run, question) {
  const picked = runs.picks.get(question.question_id) || [];
  const kind = question.multi_select ? 'checkbox' : 'radio';
  return html`<form class="pk-callout pk-callout--warn ai-run-needs ai-question" data-run-question="${run.session_id}"
      data-question-id="${question.question_id}" data-answer-url="${question.answer_url}">
    <div class="pk-stack"><strong>${icon('message-square', { size: 'sm' })} ${word('questionHead', run.agent)}</strong>
      <p class="ai-step-text ai-question-text">${question.question}</p>
      ${(question.options || []).length ? html`<div class="pk-stack ai-question-options" role="group" aria-label="${question.question}">${
        question.options.map((option) => html`<label class="pk-choice"><input type="${kind}" name="choice" value="${option}"
          ${picked.includes(option) ? 'checked' : ''}> ${option}</label>`)}</div>` : ''}
      <label class="pk-field"><span class="pk-label">${(question.options || []).length ? word('ownWords') : word('yourAnswer')}</span>
        <textarea class="pk-textarea" name="text" rows="2" maxlength="4000" data-key="question-${question.question_id}">${runs.drafts.get(`q:${question.question_id}`) || ''}</textarea></label>
      ${question.wait_s ? html`<p class="pk-muted">${word('questionWait', Math.round(question.wait_s / 60))}</p>` : ''}</div>
    <div class="pk-row"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" name="send">${icon('send-horizontal', { size: 'sm' })} ${word('answer')}</button>
      <span class="pk-grow"></span><button type="button" class="pk-btn pk-btn--ghost pk-btn--sm" data-run-cancel="${run.session_id}"
        title="${word('cancelTitle')}">${icon('square', { size: 'sm' })} ${word('cancel')}</button></div>
  </form>`;
}

/** A run that waits for its person: a result to check, or a failure handed over -- and the button that settles it. */
function needsView(run) {
  const live = liveOf(run);
  if (run.state === 'running' || !live?.needsYou) return '';
  if (live.state === 'review') {
    return html`<div class="pk-callout pk-callout--warn ai-run-needs" data-run-check>
      <div><strong>${icon('circle-check', { size: 'sm' })} ${word('checkHead')}</strong>
      <p class="ai-step-text">${word('checkWhere', live.payload?.check_in || run.title)}</p></div>
      <div class="pk-row"><button type="button" class="pk-btn pk-btn--sm pk-btn--primary" data-run-ack="${run.session_id}"
        title="${word('checkedTitle')}">${icon('check', { size: 'sm' })} ${word('checked')}</button>
        <button type="button" class="pk-btn pk-btn--sm" data-run-ask="${run.session_id}" title="${word('askTitle')}">${icon('message-square', { size: 'sm' })} ${word('ask')}</button></div></div>`;
  }
  return html`<div class="pk-callout pk-callout--warn ai-run-needs" data-run-handover>
    <div><strong>${icon('triangle-alert', { size: 'sm' })} ${word('handoverHead')}</strong>
    <p class="ai-step-text">${word('handoverWhat')}</p></div>
    <div class="pk-row"><button type="button" class="pk-btn pk-btn--sm" data-run-ack="${run.session_id}"
      title="${word('seenTitle')}">${icon('check', { size: 'sm' })} ${word('seen')}</button></div></div>`;
}

/** What a running run does now: its step in plain words, how long it has been at it (filled in by the clock, so a
 * tick redraws nothing) and a word when it went quiet or takes long. */
function liveLine(run) {
  const live = liveOf(run);
  const step = live?.step;
  const count = step?.index ? (step.total ? word('stepOf', step.index, step.total) : word('step', step.index)) : '';
  const silent = isSilent(live);
  const long = !silent && isLong({ state: 'working', started: live?.started || run.started });
  return html`<div class="ai-run-live" data-run-live="${run.session_id}">
    <div class="pk-row ai-run-now"><span class="ai-live-dot${silent ? ' ai-live-dot--quiet' : ''}" aria-hidden="true"></span>
      <span class="pk-grow" data-run-step aria-live="polite">${count ? html`<strong>${count}</strong> · ` : ''}${step ? stepLabel(step) : word('working')}</span>
      <span class="pk-muted ai-run-clock" data-run-clock="${live?.started || run.started || ''}"></span></div>
    ${step?.total ? html`<progress class="pk-progress" value="${Math.max(step.index - 1, 0)}" max="${step.total}"></progress>` : ''}
    ${silent ? html`<div class="pk-callout pk-callout--warn" data-run-silent>${icon('triangle-alert', { size: 'sm' })} ${word('silent')}</div>` : ''}
    ${long ? html`<div class="pk-muted ai-run-long" data-run-long>${icon('clock', { size: 'sm' })} ${word('long')}</div>` : ''}
    <div class="pk-row ai-run-stop"><span class="pk-grow"></span><button type="button" class="pk-btn pk-btn--ghost pk-btn--sm"
      data-run-cancel="${run.session_id}" title="${word('cancelTitle')}">${icon('square', { size: 'sm' })} ${word('cancel')}</button></div>
  </div>`;
}

/** The steps of the run's last turn, from its run events: the ones done ticked off, the one at work now marked. */
function timelineView(events, state) {
  const steps = [];
  for (const event of events || []) {
    if (event.type === 'run.started') steps.length = 0;
    if (event.type !== 'run.step' || !event.step) continue;
    if (steps.at(-1)?.index === event.step.index) continue;
    steps.push({ index: event.step.index, label: stepLabel(event.step), ts: event.ts });
  }
  if (!steps.length) return '';
  const last = steps.length - 1;
  return html`<div class="ai-timeline-box"><div class="pk-label">${word('steps')}</div><ol class="ai-timeline">${steps.map((step, i) => {
    const now = i === last && state === 'running';
    const broke = i === last && state !== 'running' && state !== 'done';
    return html`<li class="ai-timeline-step${now ? ' ai-timeline-step--now' : ''}" data-timeline-step="${step.index}">
      <span class="ai-timeline-mark" aria-hidden="true">${now ? html`<span class="pk-spinner ai-run-spin"></span>`
        : icon(broke ? 'circle-x' : 'check', { size: 'sm' })}</span>
      <span class="pk-grow">${step.label}</span>
      <span class="pk-muted ai-run-time" title="${localTime(step.ts, { seconds: true })}">${now ? word('now') : localTime(step.ts, { time: true, seconds: true })}</span></li>`; })}</ol></div>`;
}

function stepView(step) {
  if (step.kind === 'task') {
    return html`<details class="ai-step ai-step--task"><summary>${word('task')}</summary><p class="ai-step-text">${step.text}</p></details>`;
  }
  if (step.kind === 'text') return html`<p class="ai-step ai-step-text" data-step="text">${step.text}</p>`;
  if (step.kind === 'reply') {
    return html`<div class="ai-step ai-step--reply" data-step="reply"><strong>${word('you')}:</strong> <span class="ai-step-text">${step.text}</span></div>`;
  }
  if (step.kind === 'tool') {
    return html`<div class="ai-step ai-step--tool" data-step="tool">${icon('wrench', { size: 'sm' })} <strong>${step.name}</strong>
      ${step.text ? html`<span class="pk-muted">${step.text}</span>` : ''}</div>`;
  }
  return html`<div class="ai-step ai-step--${step.kind}" data-step="${step.kind}">${icon(step.kind === 'error' ? 'circle-x' : 'chevron-right', { size: 'sm' })}
    <span>${step.text}</span></div>`;
}

function runDetail(run) {
  const steps = run.steps || [];
  return html`<div class="ai-run-detail">
    ${run.error ? html`<div class="pk-callout pk-callout--danger">${icon('circle-alert', { size: 'sm' })} ${run.error}</div>` : ''}
    ${run.state === 'interrupted' ? html`<div class="pk-callout pk-callout--warn">${icon('triangle-alert', { size: 'sm' })} ${word('interruptedWhy')}</div>` : ''}
    ${runs.timeline?.id === run.session_id ? timelineView(runs.timeline.events, run.state) : ''}
    ${steps.length ? steps.map(stepView) : html`<p class="pk-muted">${run.state === 'running' ? word('waiting') : word('nothingSaid')}</p>`}
    ${(() => {
      const review = reviewParts(run.answer);
      if (review) {
        return html`${review.before ? html`<div class="pk-callout pk-callout--ok ai-run-answer"><strong>${word('result')}</strong>
          <p class="ai-step-text">${review.before}</p></div>` : ''}${reviewView(review)}`;
      }
      return run.answer ? html`<div class="pk-callout pk-callout--ok ai-run-answer"><strong>${word('result')}</strong>
        <p class="ai-step-text">${run.answer}</p></div>` : '';
    })()}
  </div>`;
}

function runCard(run) {
  const open = run.session_id === runs.open;
  return html`<section class="ai-run${open ? ' ai-run--open' : ''}" data-run="${run.session_id}">
    <div class="pk-row ai-run-head">
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm ai-run-toggle" data-run-toggle="${run.session_id}"
        aria-expanded="${open ? 'true' : 'false'}" title="${open ? word('hide') : word('show')}">${icon(open ? 'chevron-down' : 'chevron-right', { size: 'sm' })}</button>
      <div class="pk-grow"><strong>${run.agent}</strong> · ${run.title}
        <div class="pk-muted ai-run-time">${localTime(run.started)}</div></div>
      ${stateBadge(run)}</div>
    ${questionOf(run) ? questionView(run, questionOf(run)) : run.state === 'running' ? liveLine(run) : needsView(run)}
    ${open && runs.shown?.session_id === run.session_id ? runDetail({ ...run, ...runs.shown, state: run.state, error: run.error }) : ''}
    ${open && run.state !== 'running' ? html`<form class="pk-form ai-reply" data-run-reply="${run.session_id}">
      <textarea class="pk-textarea" name="text" rows="2" maxlength="8000" required data-key="reply-${run.session_id}"
        aria-label="${word('replyLabel', run.agent)}" placeholder="${word('replyHint')}">${runs.drafts.get(run.session_id) || ''}</textarea>
      <div class="pk-row"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" name="send">${icon('send-horizontal', { size: 'sm' })} ${word('reply')}</button>
        <span class="pk-grow"></span>
        <button type="button" class="pk-btn pk-btn--sm" data-run-chat="${run.session_id}" title="${word('inChatTitle')}">${icon('messages-square', { size: 'sm' })} ${word('inChat')}</button></div>
    </form>` : ''}
    ${open && run.state === 'running' ? html`<div class="pk-row"><button type="button" class="pk-btn pk-btn--sm" data-run-chat="${run.session_id}"
      title="${word('inChatTitle')}">${icon('messages-square', { size: 'sm' })} ${word('inChat')}</button></div>` : ''}
  </section>`;
}

function drawRuns() {
  if (!runs.sheet) return;
  const body = runs.sheet.querySelector('.ai-runs-body');
  // a reader scrolled to the end keeps following; one reading further up is not pulled down
  const following = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  const drawn = update(body, runs.list.length ? html`${runs.list.map(runCard)}` : html`<p class="pk-muted">${word('noRuns')}</p>`);
  // an answer being typed survives the redraw (its text is kept in drafts, the focus by data-key): the caret to its end
  const typing = drawn && body.contains(document.activeElement) && document.activeElement.matches('.ai-reply textarea');
  if (typing) document.activeElement.setSelectionRange(document.activeElement.value.length, document.activeElement.value.length);
  if (following && !typing) body.scrollTop = body.scrollHeight;
  const working = runs.list.filter((run) => run.state === 'running');
  const running = working.length;
  runs.button.hidden = !runs.list.length;
  const needs = runs.list.filter(waitsForYou).length;
  update(runs.button, html`${running ? html`<span class="ai-live-dot" aria-hidden="true"></span> ` : icon('activity', { size: 'sm' })} ${word('runs')}${running
    ? html` <span class="pk-badge pk-badge--accent" data-runs-running="${running}">${word('runsCount', running)}</span>` : ''}${needs
    ? html` <span class="pk-badge pk-badge--warn" data-runs-needs="${needs}">${word('needsCount', needs)}</span>` : ''}`);
  const first = working[0];
  const step = first && liveOf(first)?.step;
  runs.button.title = step ? word('liveTitle', first.agent, stepLabel(step)) : word('runsTitle');
  runs.flags = flags();
  tickClocks();
  keepClock();
}

/** Which running runs are silent or long now: when this changes, the sheet is drawn again. */
const flags = () => runs.list.filter((run) => run.state === 'running').map((run) => {
  const live = liveOf(run);
  return `${run.session_id}:${isSilent(live) ? 's' : ''}${isLong({ state: 'working', started: live?.started || run.started }) ? 'l' : ''}`;
}).join(',');

/** The running times, written into their places: a tick redraws nothing else. */
function tickClocks() {
  for (const clock of document.querySelectorAll('.ai-runs [data-run-clock]')) {
    const time = elapsed(clock.dataset.runClock);
    clock.textContent = time ? word('runningFor', time) : '';
  }
}

/** A clock every second while a run works: the times go on, and a run that went quiet or takes long is redrawn. */
function keepClock() {
  const running = runs.list.some((run) => run.state === 'running');
  if (running && !runs.clock) {
    runs.clock = setInterval(() => {
      if (!runs.list.some((run) => run.state === 'running')) {
        clearInterval(runs.clock);
        runs.clock = 0;
        return;
      }
      if (flags() !== runs.flags) drawRuns(); else tickClocks();
    }, 1000);
  }
}

/** The run's events for its timeline; a run the events do not know (yet) keeps what is shown. */
async function loadTimeline(id) {
  try {
    const answer = await api(`/api/agent-runs/${encodeURIComponent(id)}/events`, { quiet: true, latest: 'ai-run-events' });
    runs.timeline = { id, events: answer.events || [] };
  } catch {
    if (runs.timeline?.id !== id) runs.timeline = null;
  }
}

/** Follow the run events: a step is shown the moment it is said, an end is told at once (the list is asked again). */
function followStream() {
  runs.stream?.close();
  runs.stream = followRuns({
    onSnapshot: (list) => {
      for (const row of list) runs.live.set(row.run_id, liveRun(row, runs.live.get(row.run_id)));
      drawRuns();
      tellListeners();
    },
    onEvent: (event) => {
      runs.live.set(event.run_id, liveRun(event, runs.live.get(event.run_id)));
      tellListeners();
      if (!runs.list.some((run) => run.session_id === event.run_id)) return;
      if (FINAL.has(event.state)) {
        pollRuns();
        return;
      }
      if (event.type === 'run.step' && event.run_id === runs.open && runs.sheet?.open) loadTimeline(event.run_id).then(drawRuns);
      else drawRuns();
    },
  });
}

/** Ask for the runs (and the open one's steps); a run that ended since the last look is told and redraws the panel. */
async function pollRuns() {
  clearTimeout(runs.timer);
  try {
    const answer = await api(`${state.base}/ai/runs`, { quiet: true, latest: 'ai-runs' });
    runs.list = answer.runs || [];
    let ended = false;
    for (const run of runs.list) {
      const before = runs.known.get(run.session_id);
      if (before === 'running' && run.state !== 'running') {
        ended = true;
        toast(run.state === 'done' ? (liveOf(run)?.state === 'review' ? word('endedCheck', run.agent, run.title) : word('ended', run.agent, run.title))
          : word('endedBadly', run.agent, word('states')[run.state] || run.state),
          { kind: run.state === 'done' ? 'ok' : 'error' });
      }
      runs.known.set(run.session_id, run.state);
    }
    if (runs.open && runs.sheet?.open) {
      runs.shown = await api(`${state.base}/ai/runs/${encodeURIComponent(runs.open)}`, { quiet: true, latest: 'ai-run' });
      await loadTimeline(runs.open);
    }
    drawRuns();
    if (ended) state.onChange?.();
  } catch {
    // a missed look is retried at the next one
  }
  if (runs.list.some((run) => run.state === 'running')) runs.timer = setTimeout(pollRuns, POLL.ms);
}

/** Show a run in the side sheet, open beside the panel. */
export async function showRun(sessionId) {
  runs.open = sessionId;
  runs.shown = null;
  if (!runs.sheet.open) runs.sheet.show();
  await pollRuns();
}

/** Send the person's answer to the route of the plugin that asked (ask_user's); the run says it goes on. */
async function answerQuestion(form) {
  const id = form.dataset.questionId;
  const choices = [...form.querySelectorAll('[name="choice"]:checked')].map((box) => box.value);
  const text = form.elements.text.value.trim();
  if (!choices.length && !text) {
    form.elements.text.focus();
    return;
  }
  const run = runs.list.find((one) => one.session_id === form.dataset.runQuestion);
  await withBusy([form.elements.send], async () => {
    try {
      await api(form.dataset.answerUrl, { method: 'POST', json: { question_id: id, choices, text }, quiet: true });
      runs.drafts.delete(`q:${id}`);
      runs.picks.delete(id);
      toast(word('answered', run?.agent || ''), { kind: 'ok' });
    } catch (error) {
      // what was picked and typed stays for another try
      toast(error?.status === 404 ? word('questionGone') : errorText(error), { kind: error?.status === 404 ? 'warn' : 'error' });
    }
  });
}

function runSheet() {
  const sheet = document.createElement('dialog');
  sheet.className = 'pk-dialog pk-drawer ai-runs';
  sheet.setAttribute('aria-labelledby', 'aiRunsTitle');
  sheet.innerHTML = `<div class="pk-dialog-head"><h2 class="pk-dialog-title pk-grow" id="aiRunsTitle">${word('runs')}</h2>
    <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm" data-runs-close aria-label="${word('close')}">${icon('x', { size: 'sm' })}</button></div>
    <div class="pk-dialog-body ai-runs-body"></div>`;
  sheet.querySelector('[data-runs-close]').addEventListener('click', () => sheet.close());
  sheet.addEventListener('keydown', (event) => { if (event.key === 'Escape') sheet.close(); });
  sheet.addEventListener('input', (event) => {
    const form = event.target.closest('[data-run-reply]');
    if (form) runs.drafts.set(form.dataset.runReply, event.target.value);
    const asked = event.target.closest('[data-run-question]');
    if (asked && event.target.name === 'text') runs.drafts.set(`q:${asked.dataset.questionId}`, event.target.value);
  });
  sheet.addEventListener('change', (event) => {
    const asked = event.target.closest('[data-run-question]');
    if (asked && event.target.name === 'choice') {
      runs.picks.set(asked.dataset.questionId, [...asked.querySelectorAll('[name="choice"]:checked')].map((box) => box.value));
    }
  });
  sheet.addEventListener('submit', async (event) => {
    const asked = event.target.closest('[data-run-question]');
    if (asked) {
      event.preventDefault();
      await answerQuestion(asked);
      return;
    }
    const form = event.target.closest('[data-run-reply]');
    if (!form) return;
    event.preventDefault();
    const id = form.dataset.runReply;
    const text = form.elements.text.value.trim();
    if (!text) return;
    await withBusy([form.elements.send], async () => {
      try {
        const run = await api(`${state.base}/ai/runs/${encodeURIComponent(id)}/reply`, { method: 'POST', json: { text } });
        runs.drafts.delete(id);
        runs.known.set(id, 'running');
        toast(word('replied', run.agent), { kind: 'ok' });
        await pollRuns();
      } catch {
        // api() has shown the refusal; the answer stays in the field
      }
    });
  });
  sheet.addEventListener('click', async (event) => {
    const toggle = event.target.closest('[data-run-toggle]');
    if (toggle) {
      const id = toggle.dataset.runToggle;
      runs.open = runs.open === id ? '' : id;
      runs.shown = null;
      drawRuns();
      if (runs.open) await pollRuns();
      return;
    }
    const ack = event.target.closest('[data-run-ack]');
    if (ack) {
      await withBusy([ack], async () => {
        try {
          const answer = await api(`/api/agent-runs/${encodeURIComponent(ack.dataset.runAck)}/ack`, { method: 'POST' });
          runs.live.set(answer.run.run_id, liveRun(answer.run, runs.live.get(answer.run.run_id)));
          toast(word('thanks'), { kind: 'ok' });
          drawRuns();
          tellListeners();
        } catch {
          // api() has shown the refusal
        }
      });
      return;
    }
    const askAgent = event.target.closest('[data-run-ask]');
    if (askAgent) {
      runs.open = askAgent.dataset.runAsk;
      await pollRuns();
      runs.sheet.querySelector(`form[data-run-reply="${CSS.escape(runs.open)}"] textarea`)?.focus();
      return;
    }
    const stop = event.target.closest('[data-run-cancel]');
    if (stop) {
      const run = runs.list.find((one) => one.session_id === stop.dataset.runCancel);
      if (!run || !await confirm(word('cancelAsk', run.agent), { title: word('cancelHead'), confirmLabel: word('cancel'), danger: true })) return;
      try {
        await api(`/api/agent-runs/${encodeURIComponent(run.session_id)}/cancel`, { method: 'POST' });
        toast(word('cancelling', run.agent), { kind: 'warn' });
      } catch {
        // api() has shown the refusal
      }
      return;
    }
    const chat = event.target.closest('[data-run-chat]');
    if (chat && !openSession(chat.dataset.runChat)) toast(word('sessionIs', chat.dataset.runChat));
  });
  return sheet;
}

function runsButton(sheet) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pk-btn pk-btn--ghost ai-runs-open';
  button.dataset.aiRuns = '';
  button.title = word('runsTitle');
  button.hidden = true;
  button.addEventListener('click', async () => {
    if (sheet.open) { sheet.close(); return; }
    sheet.show();
    if (!runs.open && runs.list.length) runs.open = runs.list[0].session_id;
    await pollRuns();
  });
  state.button.after(button);
  return button;
}

async function load() {
  const data = await api(`${state.base}/ai`, { quiet: true });
  state.tasks = new Map(data.tasks.map((task) => [task.id, task]));
  update(state.box.querySelector('.ai-body'), card(data));
  label(data);
  state.button.hidden = !data.tasks.length && !data.person_only.length;
  return data;
}

/** The AI choices of the panel: a dialog behind an "AI" button in the toolbar, kept live (the dialog and the runs' sheet
 * are put on the page's body; `host` is emptied);
 * `onChange` redraws what depends on the choices. */
export async function initAiTasks(base, host, { onChange } = {}) {
  const box = frame();
  const sheet = runSheet();
  // on the page itself, not in `host`: a host in a tab not shown (hidden) would hide them too -- a modal dialog open
  // but unseen locks the whole panel
  host.replaceChildren();
  document.body.append(box, sheet);
  Object.assign(state, { base, box, onChange, button: toolbarButton(box) });
  state.button.hidden = true;
  Object.assign(runs, { sheet, list: [], open: '', shown: null, known: new Map(), drafts: new Map(), picks: new Map(), live: new Map(),
    timeline: null });
  runs.button = runsButton(sheet);
  followStream();
  try {
    const answer = await api('/agents', { quiet: true });
    state.agents = answer.agents || [];
  } catch {
    state.agents = [];   // the list only helps choose; the run itself says when an agent is missing
  }
  box.addEventListener('change', (event) => {
    const row = event.target.closest('[data-ai-task]');
    if (!row) return;
    const id = row.dataset.aiTask;
    if (event.target.name === `ai-mode-${id}`) save(id, { mode: event.target.value });
    if (event.target.name === 'ai-agent') save(id, { agent: event.target.value });
  });
  document.addEventListener('click', (event) => {
    const mark = event.target.closest('[data-ai-mark]');
    if (mark) choose(mark.dataset.aiMark);
  });
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-ai-run]');
    if (!button) return;
    await withBusy([button], async () => {
      try {
        const run = await api(`${state.base}/ai/${button.dataset.aiRun}/run`, {
          method: 'POST', json: { target: button.dataset.aiTarget || '' } });
        state.box.close();
        runs.known.set(run.session_id, 'running');   // a run done within the start's wait is told as well
        toast(word('started', run.agent), { kind: 'ok' });
        await showRun(run.session_id);
      } catch {
        // api() has shown the refusal
      }
    });
  });
  try {
    await load();
  } catch {
    state.button.hidden = true;
  }
  await pollRuns();   // runs started before a reload are there again, and are followed while they run
}
