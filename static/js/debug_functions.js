/*
  debug_functions.js — Debug panel and context management for Agent System
*/

// Persist expanded debug messages across auto-refreshes
const DEBUG_EXPANDED_KEY = 'debugExpandedMessages';
let debugExpandedSet = loadDebugExpanded();

// Debug context and messages update function
export async function updateDebugInfo() {
  try {
    const response = await fetch('/debug/context');
    if (response.ok) {
      const data = await response.json();

      // Update context statistics
      // Compute friendly displays: both absolute tokens and percentage
      const ctxWindow = Number(data.context_window || 0);

      // Prediction threshold: backend gives fraction (e.g., 0.9). Show tokens and percent.
      const predFrac = Number(data.prediction_threshold || 0);
      const predPercent = (predFrac * 100).toFixed(1) + "%";
      const predTokens = ctxWindow ? Math.round(ctxWindow * predFrac).toLocaleString() : 'N/A';

      // Summarization threshold: backend now returns absolute tokens. If it's <=1 assume it's a fraction.
      let sumTokensRaw = data.summarization_threshold;
      let sumTokens = 'N/A';
      let sumPercent = 'N/A';
      if (typeof sumTokensRaw === 'number') {
        if (sumTokensRaw > 1) {
          sumTokens = sumTokensRaw.toLocaleString();
          sumPercent = ctxWindow ? ((sumTokensRaw / ctxWindow) * 100).toFixed(1) + '%' : 'N/A';
        } else {
          // fraction provided (0..1)
          sumTokens = ctxWindow ? Math.round(ctxWindow * sumTokensRaw).toLocaleString() : 'N/A';
          sumPercent = (sumTokensRaw * 100).toFixed(1) + '%';
        }
      }

      const contextStatsHtml = `
        <div class="metric-item">
          <span class="metric-label">Context Window</span>
          <span class="metric-value">${ctxWindow ? ctxWindow.toLocaleString() : 'N/A'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Prediction Threshold</span>
          <span class="metric-value">${predTokens} (${predPercent})</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Summarization Threshold</span>
          <span class="metric-value">${sumTokens} (${sumPercent})</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Actual Total Tokens</span>
          <span class="metric-value">${(data.actual_usage?.total_tokens || 0).toLocaleString()}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Last Call Tokens</span>
          <span class="metric-value">${(data.actual_usage?.last_call_tokens || 0).toLocaleString()}</span>
        </div>
      `;

      // Update messages display
      let messagesHtml = '';
      if (data.messages && data.messages.length > 0) {
        messagesHtml = data.messages.map((msg, index) => {
            const fullContent = msg.content || '';
            const maxPreview = 200;
            const needsTruncate = fullContent.length > maxPreview;
            const preview = needsTruncate ? fullContent.substring(0, maxPreview) + '...' : fullContent;
            const msgId = `debug-msg-${index}`;
            const initiallyExpanded = debugExpandedSet.has(msgId);

            return `
              <div class="debug-message" id="${msgId}">
                <div class="debug-message-header">
                  <div class="debug-header-left">
                    <span class="debug-message-role ${needsTruncate ? 'clickable' : ''}" data-target="${needsTruncate ? msgId : ''}">${msg.role || 'unknown'}</span>
                    <span class="debug-message-index">#${index + 1}</span>
                  </div>
                  <div class="debug-header-right">
                    <span class="debug-message-tokens">${msg.estimated_tokens || '?'} tokens</span>
                    ${needsTruncate ? `<button class="debug-toggle" data-target="${msgId}" aria-expanded="${initiallyExpanded ? 'true' : 'false'}">${initiallyExpanded ? '⤡' : '⤢'}</button>` : `<span class="debug-toggle-placeholder" aria-hidden="true"></span>`}
                  </div>
                </div>
                <div class="debug-message-content">
                  <div class="debug-preview" style="display: ${initiallyExpanded ? 'none' : ''}">${escapeHtml(preview)}</div>
                  <div class="debug-full" style="display:${initiallyExpanded ? 'block' : 'none'}">${escapeHtml(fullContent)}</div>
                </div>
              </div>
            `;
        }).join('');
      } else {
        messagesHtml = '<div class="metric-item"><span class="metric-label">No messages in conversation</span></div>';
      }

      const debugContextStats = document.getElementById('debugContextStats');
      const debugMessages = document.getElementById('debugMessages');

      if (debugContextStats) debugContextStats.innerHTML = contextStatsHtml;
      if (debugMessages) debugMessages.innerHTML = messagesHtml;

          // Attach toggle handlers for expandable debug messages
          if (debugMessages) {
            // Handle both button clicks and role label clicks
            const toggles = debugMessages.querySelectorAll('.debug-toggle, .debug-message-role.clickable');
            toggles.forEach(elem => {
              elem.addEventListener('click', (ev) => {
                const targetId = elem.getAttribute('data-target');
                if (!targetId) return;

                const container = document.getElementById(targetId);
                if (!container) return;

                const preview = container.querySelector('.debug-preview');
                const full = container.querySelector('.debug-full');
                const button = container.querySelector('.debug-toggle');
                const roleLabel = container.querySelector('.debug-message-role.clickable');

                const currentlyExpanded = debugExpandedSet.has(targetId);

                if (currentlyExpanded) {
                  // collapse
                  if (full) full.style.display = 'none';
                  if (preview) preview.style.display = '';
                  if (button) {
                    button.textContent = '⤢';
                    button.setAttribute('aria-expanded', 'false');
                  }
                  if (roleLabel) roleLabel.classList.remove('expanded');
                  debugExpandedSet.delete(targetId);
                } else {
                  // expand
                  if (preview) preview.style.display = 'none';
                  if (full) full.style.display = 'block';
                  if (button) {
                    button.textContent = '⤡';
                    button.setAttribute('aria-expanded', 'true');
                  }
                  if (roleLabel) roleLabel.classList.add('expanded');
                  debugExpandedSet.add(targetId);
                }

                // persist expansion state
                saveDebugExpanded(debugExpandedSet);
              });
            });
          }

    } else {
      // Error response
      const debugContextStats = document.getElementById('debugContextStats');
      const debugMessages = document.getElementById('debugMessages');

      if (debugContextStats) {
        debugContextStats.innerHTML = `
          <div class="metric-item">
            <span class="metric-label">Error</span>
            <span class="metric-value">Failed to load debug info</span>
          </div>
        `;
      }
      if (debugMessages) {
        debugMessages.innerHTML = '<div class="metric-item"><span class="metric-label">Debug endpoint not available</span></div>';
      }
    }
  } catch (error) {
    console.error('Failed to update debug info:', error);
    const debugContextStats = document.getElementById('debugContextStats');
    const debugMessages = document.getElementById('debugMessages');

    if (debugContextStats) {
      debugContextStats.innerHTML = `
        <div class="metric-item">
          <span class="metric-label">Error</span>
          <span class="metric-value">Network error</span>
        </div>
      `;
    }
    if (debugMessages) {
      debugMessages.innerHTML = '<div class="metric-item"><span class="metric-label">Failed to load messages</span></div>';
    }
  }
}

// Debug expanded state persistence
export function loadDebugExpanded() {
  try {
    const raw = localStorage.getItem(DEBUG_EXPANDED_KEY);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (e) {
    return new Set();
  }
}

export function saveDebugExpanded(set) {
  try {
    localStorage.setItem(DEBUG_EXPANDED_KEY, JSON.stringify(Array.from(set)));
  } catch (e) {
    // ignore localStorage errors
  }
}

// Helper function to escape HTML
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}