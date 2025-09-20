// Chat module extracted from main.js - exposes a simple API on window.chatModule
(function (global) {
  const chatModule = {};

  // Markdown to HTML conversion
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
    // Process ordered list blocks first (preserve numbering)
    html = html.replace(/(^|\n)((?:[ \t]*\d+\.\s+.+(?:\n|$))+)/gm, function(_, pre, block) {
      const lines = block.trim().split(/\r?\n/).filter(Boolean);
      const items = lines.map(l => l.replace(/^[ \t]*\d+\.\s+/, '').trim());
      return pre + '<ol>' + items.map(i => `<li>${i}</li>`).join('') + '</ol>';
    });

    // Then process unordered list blocks
    html = html.replace(/(^|\n)((?:[ \t]*[-*+]\s+.+(?:\n|$))+)/gm, function(_, pre, block) {
      const lines = block.trim().split(/\r?\n/).filter(Boolean);
      const items = lines.map(l => l.replace(/^[ \t]*[-*+]\s+/, '').trim());
      return pre + '<ul>' + items.map(i => `<li>${i}</li>`).join('') + '</ul>';
    });

    // Collapse multiple blank lines to paragraph separators and convert remaining newlines to <br>
    html = html.replace(/\n{2,}/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');

    // Ensure top-level block wrappers; avoid wrapping lists/tables/pre headers
    if (!html.match(/^<(h[1-6]|table|ul|ol|pre|div)/)) html = `<p>${html}</p>`;

  // Clean up common unwanted patterns inside lists (e.g., <p> or <br> within <li>)
  html = html.replace(/<li>\s*<p>(.*?)<\/p>\s*<\/li>/gs, '<li>$1</li>');
  html = html.replace(/<li>([\s\S]*?)<br>\s*<\/li>/g, '<li>$1</li>');
  html = html.replace(/<ul>\s*<br>\s*<li>/g, '<ul><li>');
  html = html.replace(/<li>\s*<br>\s*/g, '<li>');
  html = html.replace(/<\/li>\s*<br>\s*(<li>|<\/ul>|<\/ol>)/g, '</li>$1');
    return html;
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function scrollBottom() {
    requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));
  }

  function addUser(chatContainer, text) {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `<div class="msg user">${escapeHtml(text)}</div>`;
    chatContainer.appendChild(row);
    scrollBottom();
  }

  function addAssistantBlock(chatContainer) {
    const row = document.createElement('div');
    row.className = 'row';
    const box = document.createElement('div');
    box.className = 'msg assistant';
    box.innerHTML = `
      <div class="container-section">
        <div class="container-header" data-toggle="thinking">
          <span class="toggle-arrow">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span class="type-icon">🤔</span>
          <span class="container-label">Thinking...</span>
        </div>
        <div class="container-body" id="thinking" style="display: none;">
          <pre id="thinkingContent"></pre>
        </div>
      </div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="status">
          <span class="toggle-arrow">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span class="type-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">     
              <rect x="3" y="4" width="18" height="16" rx="2" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
              <path d="M7 9l2 2 4-4" stroke="#56d364" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </span>
          <span class="container-label">Status</span>
        </div>
        <div class="container-body" id="statusBody" style="display: block;"></div>
      </div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="response">
          <span class="toggle-arrow">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span class="type-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"> 
              <rect x="2" y="3" width="20" height="14" rx="3" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
              <circle cx="8.5" cy="9" r="1.1" fill="#cbd5e1" />
              <circle cx="15.5" cy="9" r="1.1" fill="#cbd5e1" />
              <path d="M7 13c1 0 2 0.8 3 0.8s2-0.8 3-0.8" stroke="#9fb8d9" stroke-width="0.9" stroke-linecap="round" stroke-linejoin="round" />
              <rect x="6" y="15.5" width="6" height="3" rx="0.8" fill="#071028" />
            </svg>
          </span>
          <span class="container-label">Response</span>
        </div>
        <div class="container-body" id="assistantText" style="display: block;"></div>
      </div>
    `;
    row.appendChild(box);
    chatContainer.appendChild(row);
    scrollBottom();

    const headers = box.querySelectorAll('.container-header');
    headers.forEach(header => {
      const body = header.nextElementSibling;
      const arrow = header.querySelector('.toggle-arrow svg');
      if (body && arrow) {
        arrow.style.transform = (body.style.display === 'none') ? 'rotate(0deg)' : 'rotate(90deg)';
      }

      header.addEventListener('click', () => {
        const body = header.nextElementSibling;
        if (body && body.classList.contains('container-body')) {
          const isHidden = body.style.display === 'none';
          body.style.display = isHidden ? 'block' : 'none';
          const arrow = header.querySelector('.toggle-arrow svg');
          if (arrow) {
            arrow.style.transform = isHidden ? 'rotate(90deg)' : 'rotate(0deg)';
          }
        }
      });
    });

    return {
      row,
      box,
      t: box.querySelector('#assistantText'),
      think: box.querySelector('#thinkingContent'),
      status: box.querySelector('#statusBody'),
      thinkingSection: box.querySelector('[data-toggle="thinking"]').parentElement,
      statusSection: box.querySelector('[data-toggle="status"]').parentElement,
      responseSection: box.querySelector('[data-toggle="response"]').parentElement
    };
  }

  const activeOperations = new Map();

  function addStatusEvent(container, ev) {
    if (!container || !ev) return;
    const statusSection = container.closest('.container-section');
    if (statusSection && statusSection.style.display === 'none') {
      statusSection.style.display = 'block';
    }
    const opIdPart = ev.request_id && ev.request_id !== 'default' ? ev.request_id : null;
    const operationKey = opIdPart ? `${ev.server}_${opIdPart}` : `${ev.server}`;
    if (ev.phase === 'start') {
      if (activeOperations.has(operationKey)) {
        const existing = activeOperations.get(operationKey);
        const msg = existing.querySelector('.progress-message');
        const time = existing.querySelector('.progress-time');
        if (msg) msg.textContent = ev.message || 'Starting...';
        if (time) time.textContent = formatTime(ev.timestamp);
      } else {
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
      }
    } else if (ev.phase === 'progress') {
      const operationDiv = activeOperations.get(operationKey);
      if (operationDiv) {
        const messageSpan = operationDiv.querySelector('.progress-message');
        const timeSpan = operationDiv.querySelector('.progress-time');
        if (messageSpan) messageSpan.textContent = ev.message || 'In progress...';
        if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
      }
    } else if (ev.phase === 'end') {
      const operationDiv = activeOperations.get(operationKey);
      if (operationDiv) {
        const iconSpan = operationDiv.querySelector('.progress-icon');
        const messageSpan = operationDiv.querySelector('.progress-message');
        const timeSpan = operationDiv.querySelector('.progress-time');
        // Respect backend hint to suppress the completion icon for internal helpers
        const suppressIcon = ev.meta && ev.meta.suppress_completion_icon;
        if (iconSpan) iconSpan.innerHTML = suppressIcon ? '' : '<div class="checkmark">✓</div>';
        if (messageSpan) messageSpan.textContent = ev.message || 'Completed';
        if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
        operationDiv.classList.add('completed');
      }
      activeOperations.delete(operationKey);
    } else if (ev.phase === 'error') {
      const operationDiv = activeOperations.get(operationKey);
      if (operationDiv) {
        const iconSpan = operationDiv.querySelector('.progress-icon');
        const messageSpan = operationDiv.querySelector('.progress-message');
        const timeSpan = operationDiv.querySelector('.progress-time');
        if (iconSpan) iconSpan.innerHTML = '<div class="error-mark">✕</div>';
        if (messageSpan) messageSpan.textContent = ev.message || 'Error occurred';
        if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
        operationDiv.classList.add('error');
      }
      activeOperations.delete(operationKey);
    }
  }

  function formatTime(ts) {
    try {
      return new Date(ts).toLocaleTimeString();
    } catch (e) {
      return ts;
    }
  }

  // Public init function that wires the chat form behavior
  chatModule.init = function (opts) {
    const chatForm = document.getElementById('f');
    const taskInput = document.getElementById('task');
    const runBtn = document.getElementById('runBtn');
    const stopBtn = document.getElementById('stopBtn');
    const chatContainer = document.getElementById('chat');

    // Track current request and session
    let currentRequestId = null;
    let currentSessionId = null;
    // Expose current session id for other modules (fallback for UI)
    chatModule.getCurrentSessionId = function() { return currentSessionId; };
    Object.defineProperty(chatModule, 'currentSessionId', {
      get: function() { return currentSessionId; }
    });
    // Also export to global window for older modules
    try { global.currentSessionId = currentSessionId; } catch (e) { /* ignore */ }
    let currentEventSource = null;

    if (!chatForm || !taskInput || !runBtn || !stopBtn || !chatContainer) {
      console.warn('Chat form elements not found');
      return;
    }

    // Stop button event listener
    stopBtn.addEventListener('click', async function() {
      if (currentRequestId) {
        // Sofortiges Feedback geben
        stopBtn.textContent = 'Canceling';
        stopBtn.disabled = true;
        stopBtn.classList.add('cancelling');

        try {
          const response = await fetch(`/cancel/${currentRequestId}`, { method: 'POST' });
          const result = await response.json();
          console.log('Cancel request result:', result);

          // Kurze Verzögerung für besseres UX-Feedback
          setTimeout(() => {
            if (result.status === 'cancelled') {
              stopBtn.textContent = 'Done';
              stopBtn.classList.remove('cancelling');
              stopBtn.classList.add('cancelled');
            } else {
              stopBtn.textContent = 'Failed';
              stopBtn.classList.remove('cancelling');
              stopBtn.classList.add('cancel-failed');
            }
          }, 500);

        } catch (error) {
          console.error('Failed to cancel request:', error);
          stopBtn.textContent = 'Failed';
          stopBtn.classList.remove('cancelling');
          stopBtn.classList.add('cancel-failed');
        }

        // Nach 2 Sekunden wieder zurücksetzen (falls Anfrage noch läuft)
        setTimeout(() => {
          if (stopBtn.style.display !== 'none') { // Nur zurücksetzen wenn Button noch sichtbar
            stopBtn.textContent = 'Stop';
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          }
        }, 2000);
      }
    });

    chatForm.addEventListener('submit', async function(e) {
      e.preventDefault();
      const task = taskInput.value.trim();
      if (!task) return;
      addUser(chatContainer, task);
      taskInput.value = '';

      // If there's an active request, append the user message to it
      if (currentRequestId && currentEventSource) {
        try {
          // immediate UX feedback: show thinking block if none
          const blk = addAssistantBlock(chatContainer);
          runBtn.style.display = 'none';
          stopBtn.style.display = 'block';

          const resp = await fetch(`/events/${encodeURIComponent(currentRequestId)}/append`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: task })
          });

          if (!resp.ok) {
            const txt = await resp.text();
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">Failed to append message: ${escapeHtml(txt)}</div>`;
            // don't change current request state
            return;
          }

          // appended successfully; the running agent will pick it up and continue
          return;

        } catch (err) {
          console.error('Failed to append to active request:', err);
          // fall through to starting a new request
        }
      }

      // No active request: start a new SSE-based request
      const blk = addAssistantBlock(chatContainer);
      runBtn.style.display = 'none'; // Hide run button
      stopBtn.style.display = 'block'; // Show stop button
      currentRequestId = null; // Reset request ID

      let sseOk = false;
      const eventUrl = currentSessionId 
        ? `/events?task=${encodeURIComponent(task)}&session_id=${encodeURIComponent(currentSessionId)}`
        : `/events?task=${encodeURIComponent(task)}`;
      const es = new EventSource(eventUrl);
      currentEventSource = es; // Track current event source
      let statusEs = null;

      // Store current request ID and event source
      currentRequestId = task;
      currentEventSource = es;

      try {
        statusEs = new EventSource('/status/stream');
        statusEs.onmessage = (ev) => {
          try {
            const statusData = JSON.parse(ev.data);
            addStatusEvent(blk.status, statusData);
          } catch (err) {
            // ignore JSON parse errors
          }
        };
      } catch (err) {
        console.warn('Status stream not available:', err);
      }

      es.onopen = () => { sseOk = true; };

      es.onmessage = ev => {
        try {
          const data = JSON.parse(ev.data);
          switch (data.type) {
            case 'start':
              currentRequestId = data.request_id;
              currentSessionId = data.session_id;
              // update exported values
              try { global.currentSessionId = currentSessionId; } catch (e) {}
              console.log('Request started with ID:', currentRequestId, 'Session ID:', currentSessionId);
              break;
            case 'cancelled':
              showSection(blk.t);
              blk.t.innerHTML = `<div class="response-text cancelled">Request cancelled at step ${data.step}</div>`;
              break;
            case 'thinking':
              if (data.assistant) {
                if (data.assistant.content) {
                  blk.think.textContent += `💭 Step ${data.step}: ${data.assistant.content}\n\n`;
                }
                if (data.assistant.tool_calls && data.assistant.tool_calls.length > 0) {
                  blk.think.textContent += `🧠 Step ${data.step}: Planning to call ${data.assistant.tool_calls.length} tool(s):\n`;
                  data.assistant.tool_calls.forEach((tc, i) => {
                    const func = tc.function || {};
                    blk.think.textContent += `  ${i + 1}. ${func.name || 'unknown'}\n`;
                  });
                  blk.think.textContent += '\n';
                }
              } else {
                blk.think.textContent += `🤔 Step ${data.step}: Analyzing task...\n`;
              }
              break;
            case 'final':
              const content = data.summary || data.content || '';
              showSection(blk.t);
              blk.t.innerHTML = `<div class="response-text">${markdownToHtml(content)}</div>`;
              break;
            case 'end':
              es.close();
              if (statusEs) statusEs.close();
              runBtn.style.display = 'block'; // Show run button
              stopBtn.style.display = 'none'; // Hide stop button
              // Reset stop button state
              stopBtn.textContent = 'Stop';
              stopBtn.disabled = false;
              stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
              currentRequestId = null; // Reset request tracking
              currentEventSource = null;
              break;
            case 'error':
              showSection(blk.t);
              blk.t.innerHTML = `<div class="response-text error">${escapeHtml(data.message)}</div>`;
              es.close();
              if (statusEs) statusEs.close();
              runBtn.style.display = 'block'; // Show run button
              stopBtn.style.display = 'none'; // Hide stop button
              // Reset stop button state
              stopBtn.textContent = 'Stop';
              stopBtn.disabled = false;
              stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
              currentRequestId = null; // Reset request tracking
              currentEventSource = null;
              break;
          }
          scrollBottom();
        } catch (err) {
          // ignore JSON parse errors
        }
      };

      // Fallback when SSE doesn't work
      setTimeout(async () => {
        if (!sseOk) {
          try {
            const r = await fetch('/run?task=' + encodeURIComponent(task), { method: 'POST' });
            const j = await r.json();
            const content = j.summary || JSON.stringify(j, null, 2);
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text">${markdownToHtml(content)}</div>`;
          } catch (e) {
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">Request failed: ${escapeHtml(String(e))}</div>`;
          }
          runBtn.style.display = 'block'; // Show run button
          stopBtn.style.display = 'none'; // Hide stop button
          // Reset stop button state
          stopBtn.textContent = 'Stop';
          stopBtn.disabled = false;
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          currentRequestId = null; // Reset request tracking
          currentEventSource = null;
          es.close();
          if (statusEs) statusEs.close();
        }
      }, 1500);

      es.onerror = () => {
        es.close();
        if (statusEs) statusEs.close();
        runBtn.style.display = 'block'; // Show run button
        stopBtn.style.display = 'none'; // Hide stop button
        // Reset stop button state
        stopBtn.textContent = 'Stop';
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        currentRequestId = null; // Reset request tracking
        currentEventSource = null;
      };
    });
  };

  function showSection(element) {
    const section = element.closest('.container-section');
    if (section && section.style.display === 'none') {
      section.style.display = 'block';
    }
  }

  // attach to global
  global.chatModule = chatModule;

})(window);
