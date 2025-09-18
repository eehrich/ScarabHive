/*
  status_functions.js — Status panel and metrics management for Agent System
*/

// Status metrics update function
export async function updateStatusMetrics() {
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
    const statusMetrics = document.getElementById('statusMetrics');
    if (statusMetrics) statusMetrics.innerHTML = `
      <div class="metric-item">
        <span class="metric-label">Status</span>
        <span class="metric-value">Error loading</span>
      </div>
    `;
  }
}

// Status event management for operation progress
const activeOperations = new Map();

export function addStatusEvent(container, ev) {
  // If request_id is missing/null use server-only key to avoid duplicate entries
  const opIdPart = ev.request_id && ev.request_id !== 'default' ? ev.request_id : null;
  const operationKey = opIdPart ? `${ev.server}_${opIdPart}` : `${ev.server}`;
  
  if (ev.phase === 'start') {
    // If we already have an active operation for this key, update it
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

// Helper functions
function escapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;'}[c]));
}

function formatTime(ts) {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch (e) {
    return ts;
  }
}