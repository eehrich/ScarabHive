// Client-side UI logic extracted from templates/index.html
const chat = document.getElementById('chat');
const form = document.getElementById('f');
const runBtn = document.getElementById('runBtn');
const statusMetrics = document.getElementById('statusMetrics');

// Status metrics update function
async function updateStatusMetrics() {
  try {
    const response = await fetch('/status/meta');
    if (response.ok) {
      const data = await response.json();
      const html = `
        <div class="metric-item">
          <span class="metric-label">Subscribers</span>
          <span class="metric-value">${data.subscribers || 0}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Published</span>
          <span class="metric-value">${data.publish_attempted || 0}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Delivered</span>
          <span class="metric-value">${data.delivered || 0}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Suppressed</span>
          <span class="metric-value">${(data.suppressed_rate || 0) + (data.suppressed_debounce || 0)}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Max RPS</span>
          <span class="metric-value">${data.config.AGENT_STATUS_MAX_RPS || 'Unlimited'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Debounce MS</span>
          <span class="metric-value">${data.config.AGENT_STATUS_DEBOUNCE_MS || 'None'}</span>
        </div>
      `;
      const sidebarMeta = document.getElementById('statusMetrics');
      const floatingMeta = document.getElementById('floatingStatusMetrics');
      if (sidebarMeta) sidebarMeta.innerHTML = html;
      if (floatingMeta) floatingMeta.innerHTML = html;
    }
  } catch (error) {
    if (statusMetrics) statusMetrics.innerHTML = `
      <div class="metric-item">
        <span class="metric-label">Status</span>
        <span class="metric-value">Error loading</span>
      </div>
    `;
  }
}

// Floating status panel
const statusPanel = document.createElement('div');
statusPanel.id = 'floatingStatusPanel';
statusPanel.className = 'floating-panel';
statusPanel.style.display = 'none';
statusPanel.innerHTML = `
  <div class="floating-panel-header" id="floatingStatusHeader">
    <span>Status & Metrics</span>
    <div style="margin-left:8px;flex:1"></div>
    <button id="floatingCloseBtn" title="Close" aria-label="Close">✕</button>
  </div>
  <div class="floating-panel-body" id="floatingStatusBody">
    <div class="status-metrics" id="floatingStatusMetrics">
      <div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div>
    </div>
  </div>
`;
document.body.appendChild(statusPanel);

const statusToggleBtn = document.getElementById('statusToggleBtn');
const floatingCloseBtn = document.getElementById('floatingCloseBtn');
const floatingStatusMetrics = document.getElementById('floatingStatusMetrics');

statusToggleBtn.addEventListener('click', () => {
  const shown = statusPanel.style.display !== 'none';
  statusPanel.style.display = shown ? 'none' : 'block';
  statusToggleBtn.setAttribute('aria-expanded', String(!shown));
  if (!shown) updateStatusMetrics().catch(() => {});
});

floatingCloseBtn.addEventListener('click', () => {
  statusPanel.style.display = 'none';
  statusToggleBtn.setAttribute('aria-expanded', 'false');
});

// Make panel draggable
(function makeDraggable(headerId, panel) {
  const header = document.getElementById(headerId);
  let isDragging = false;
  let startX = 0, startY = 0, origX = 0, origY = 0;
  header.addEventListener('pointerdown', (ev) => {
    try {
      if (ev.target && ev.target.closest && ev.target.closest('#floatingCloseBtn')) return;
    } catch (e) {}
    isDragging = true;
    startX = ev.clientX; startY = ev.clientY;
    const rect = panel.getBoundingClientRect();
    origX = rect.left; origY = rect.top;
    header.setPointerCapture(ev.pointerId);
  });
  window.addEventListener('pointermove', (ev) => {
    if (!isDragging) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    panel.style.left = (origX + dx) + 'px';
    panel.style.top = (origY + dy) + 'px';
    panel.style.right = 'auto';
  });
  window.addEventListener('pointerup', (ev) => { isDragging = false; });
})('floatingStatusHeader', statusPanel);

setInterval(updateStatusMetrics, 2000);
updateStatusMetrics();

function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;'}[c])); }
function formatTime(ts) { try { return new Date(ts).toLocaleTimeString(); } catch (e) { return ts; } }

const activeOperations = new Map();

function addStatusEvent(container, ev) {
  const operationKey = `${ev.server}_${ev.request_id || 'default'}`;
  if (ev.phase === 'start') {
    const operationDiv = document.createElement('div');
    operationDiv.className = 'operation-progress';
    operationDiv.setAttribute('data-operation', operationKey);
    operationDiv.innerHTML = `
      <div class="progress-line">
        <span class="progress-icon"><div class="spinner"></div></span>
        <span class="progress-server">${escapeHtml(ev.server || 'Unknown')}</span>
        <span class="progress-message">${escapeHtml(ev.message || 'Starting...')}</span>
        <span class="progress-time">${formatTime(ev.timestamp)}</span>
      </div>
    `;
    container.appendChild(operationDiv);
    activeOperations.set(operationKey, operationDiv);
  } else if (ev.phase === 'progress') {
    const operationDiv = activeOperations.get(operationKey);
    if (operationDiv) {
      const messageSpan = operationDiv.querySelector('.progress-message');
      if (messageSpan) messageSpan.textContent = ev.message || 'In progress...';
    }
  } else if (ev.phase === 'end') {
    const operationDiv = activeOperations.get(operationKey);
    if (operationDiv) {
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      if (iconSpan) iconSpan.innerHTML = '<div class="checkmark">✓</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Completed';
      operationDiv.classList.add('completed');
    }
    activeOperations.delete(operationKey);
  } else if (ev.phase === 'error') {
    const operationDiv = activeOperations.get(operationKey);
    if (operationDiv) {
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      if (iconSpan) iconSpan.innerHTML = '<div class="error-mark">✕</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Error occurred';
      operationDiv.classList.add('error');
    }
    activeOperations.delete(operationKey);
  }
}

function markdownToHtml(md) {
  if (!md) return '';
  let html = md;
  html = escapeHtml(html);
  html = html.replace(/^### (.*$)/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.*$)/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.*$)/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/__(.*?)__/g, '<strong>$1</strong>');
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  html = html.replace(/_(.*?)_/g, '<em>$1</em>');
  html = html.replace(/```[\s\S]*?```/g, function(match) {
    const content = match.replace(/```.*?\n/, '').replace(/\n```$/, '');
    return `<pre><code>${content}</code></pre>`;
  });
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^\|(.+)\|$/gm, function(match, content) {
    const cells = content.split('|').map(cell => cell.trim());
    const cellTags = cells.map(cell => `<td>${cell}</td>`).join('');
    return `<tr>${cellTags}</tr>`;
  });
  html = html.replace(/(<tr>.*<\/tr>[\n\r]*)+/g, function(match) {
    const rows = match.trim().split(/[\n\r]+/);
    let tableContent = '';
    let inHeader = true;
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      if (row.includes('---') || row.includes('===')) { inHeader = false; continue; }
      if (inHeader && i === 0) {
        const headerRow = row.replace(/<td>/g, '<th>').replace(/<\/td>/g, '</th>');
        tableContent += `<thead>${headerRow}</thead><tbody>`;
        inHeader = false;
      } else { tableContent += row; }
    }
    if (!tableContent.includes('<tbody>')) tableContent = `<tbody>${tableContent}</tbody>`;
    else tableContent += '</tbody>';
    return `<table class="markdown-table">${tableContent}</table>`;
  });
  html = html.replace(/^[\s]*[-*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>[\n\r]*)+/g, '<ul>$&</ul>');
  html = html.replace(/^[\s]*\d+\. (.+)$/gm, '<li>$1</li>');
  html = html.replace(/\n\n+/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');
  if (!html.match(/^<(h[1-6]|table|ul|ol|pre|div)/)) html = `<p>${html}</p>`;
  html = html.replace(/<p><\/p>/g, '');
  html = html.replace(/<p><br><\/p>/g, '');
  return html;
}

function scrollBottom() { requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })); }

function addUser(text) {
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `<div class="msg user">${escapeHtml(text)}</div>`;
  chat.appendChild(row);
  scrollBottom();
}

function addAssistantBlock() {
  const row = document.createElement('div');
  row.className = 'row';
  const box = document.createElement('div');
  box.className = 'msg assistant';
  box.innerHTML = `
    <details id="thinkingBox"><summary>Thinking…</summary><pre id="thinking"></pre></details>
    <div id="conversationFlow" class="conversation-flow">
      <div id="statusContainer" class="status-container">
        <div class="status-header">
          <span class="response-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
              <path d="M7 9l2 2 4-4" stroke="#56d364" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </span>
          <span class="response-label">Status</span>
        </div>
        <div id="statusBody" class="status-body"></div>
      </div>
      <div id="assistantText" class="response-content"></div>
    </div>
  `;
  row.appendChild(box);
  chat.appendChild(row);
  scrollBottom();
  return {
    row, box, t: box.querySelector('#assistantText'), think: box.querySelector('#thinking'),
    status: box.querySelector('#statusBody'), thinkBox: box.querySelector('#thinkingBox'), conversationFlow: box.querySelector('#conversationFlow')
  };
}

form.addEventListener('submit', e => { e.preventDefault(); run(); });

async function run() {
  const taskInput = document.getElementById('task');
  const task = taskInput.value.trim();
  if (!task) return;
  addUser(task); taskInput.value = '';
  const blk = addAssistantBlock(); runBtn.disabled = true;
  let sseOk = false;
  const es = new EventSource(`/events?task=${encodeURIComponent(task)}`);
  let statusEs = null;
  try {
    statusEs = new EventSource('/status/stream');
    statusEs.onmessage = (ev) => { try { const statusData = JSON.parse(ev.data); addStatusEvent(blk.status, statusData); } catch (err) {} };
  } catch (err) { console.warn('Status stream not available:', err); }
  es.onopen = () => { console.info('SSE connected'); sseOk = true; };
  es.onmessage = ev => {
    try {
      const data = JSON.parse(ev.data);
      switch (data.type) {
        case 'thinking':
          if (data.assistant) {
            if (data.assistant.content) blk.think.textContent += `💭 Step ${data.step}: ${data.assistant.content}\n\n`;
            if (data.assistant.tool_calls && data.assistant.tool_calls.length > 0) {
              blk.think.textContent += `🧠 Step ${data.step}: Planning to call ${data.assistant.tool_calls.length} tool(s):\n`;
              data.assistant.tool_calls.forEach((tc, i) => { const func = tc.function || {}; blk.think.textContent += `  ${i + 1}. ${func.name || 'unknown'}\n`; });
              blk.think.textContent += '\n';
            }
          } else { blk.think.textContent += `🤔 Step ${data.step}: Analyzing task...\n`; }
          break;
        case 'final':
          const content = data.summary || data.content || '';
          blk.t.innerHTML = `
            <div class="response-header">
              <span class="response-icon">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <rect x="2" y="3" width="20" height="14" rx="3" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
                  <circle cx="8.5" cy="9" r="1.1" fill="#cbd5e1" />
                  <circle cx="15.5" cy="9" r="1.1" fill="#cbd5e1" />
                  <path d="M7 13c1 0 2 0.8 3 0.8s2-0.8 3-0.8" stroke="#9fb8d9" stroke-width="0.9" stroke-linecap="round" stroke-linejoin="round" />
                  <rect x="6" y="15.5" width="6" height="3" rx="0.8" fill="#071028" />
                </svg>
              </span>
              <span class="response-label">Response</span>
            </div>
            <div class="response-text">${markdownToHtml(content)}</div>
          `;
          blk.thinkBox.open = false;
          break;
        case 'end':
          es.close(); if (statusEs) statusEs.close(); runBtn.disabled = false; break;
        case 'error':
          blk.t.innerHTML = `
            <div class="response-header error">
              <span class="response-icon">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <path d="M12 2L2 20h20L12 2z" fill="#2b0505" stroke="#f85149" stroke-width="0.8" />
                  <rect x="11" y="8" width="2" height="6" fill="#f85149" />
                  <rect x="11" y="16" width="2" height="2" fill="#f85149" />
                </svg>
              </span>
              <span class="response-label">Error</span>
            </div>
            <div class="response-text">${escapeHtml(data.message)}</div>
          `; break;
      }
      scrollBottom();
    } catch (err) {}
  };

  setTimeout(async () => {
    if (!sseOk) {
      try {
        const r = await fetch('/run?task=' + encodeURIComponent(task), { method: 'POST' });
        const j = await r.json();
        const content = j.summary || JSON.stringify(j, null, 2);
        blk.t.innerHTML = `
          <div class="response-header">
            <span class="response-icon">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <rect x="2" y="3" width="20" height="14" rx="3" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
                <circle cx="8.5" cy="9" r="1.1" fill="#cbd5e1" />
                <circle cx="15.5" cy="9" r="1.1" fill="#cbd5e1" />
                <path d="M7 13c1 0 2 0.8 3 0.8s2-0.8 3-0.8" stroke="#9fb8d9" stroke-width="0.9" stroke-linecap="round" stroke-linejoin="round" />
                <rect x="6" y="15.5" width="6" height="3" rx="0.8" fill="#071028" />
              </svg>
            </span>
            <span class="response-label">Response</span>
          </div>
          <div class="response-text">${markdownToHtml(content)}</div>
        `;
      } catch (e) {
        blk.t.innerHTML = `
          <div class="response-header error">
            <span class="response-icon">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <path d="M12 2L2 20h20L12 2z" fill="#2b0505" stroke="#f85149" stroke-width="0.8" />
                <rect x="11" y="8" width="2" height="6" fill="#f85149" />
                <rect x="11" y="16" width="2" height="2" fill="#f85149" />
              </svg>
            </span>
            <span class="response-label">Error</span>
          </div>
          <div class="response-text">Request failed: ${escapeHtml(String(e))}</div>
        `;
      }
      runBtn.disabled = false; es.close(); if (statusEs) statusEs.close();
    }
  }, 1500);

  es.onerror = () => { es.close(); if (statusEs) statusEs.close(); runBtn.disabled = false; };
}

// MCP connected indicator
(function manageMcpIndicator() {
  const indicator = document.getElementById('mcpConnected');
  if (!indicator) return;
  function setConnected(v) {
    if (v) { indicator.classList.add('connected'); indicator.setAttribute('title', 'Connected'); indicator.setAttribute('aria-hidden', 'false'); }
    else { indicator.classList.remove('connected'); indicator.setAttribute('title', 'Disconnected'); indicator.setAttribute('aria-hidden', 'true'); }
  }
  let sse = null;
  function connect() {
    try {
      sse = new EventSource('/status/stream');
      sse.onopen = () => setConnected(true);
      sse.onmessage = () => {};
      sse.onerror = () => { setConnected(false); try { sse.close(); } catch (e) {} sse = null; setTimeout(connect, 3000); };
    } catch (e) { setConnected(false); setTimeout(connect, 3000); }
  }
  connect();
})();
