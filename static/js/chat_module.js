// Chat module extracted from main.js - exposes a simple API on window.chatModule
(function (global) {
  const chatModule = {};

  // Backend now handles all formatting via plugins
  // Frontend displays content as-is

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function formatTextWithLineBreaks(text) {
    // Escape HTML first, then convert newlines to <br>
    const escaped = escapeHtml(text);
    return escaped.replace(/\n/g, '<br>');
  }

  function formatContent(content, format) {
    // If format is explicitly 'html', return as-is (already sanitized by backend)
    if (format === 'html') {
      return content;
    }
    // Otherwise, escape and convert line breaks
    return formatTextWithLineBreaks(content);
  }

  function scrollBottom() {
    requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));
  }

  function addUser(chatContainer, text, images = []) {
    // Ensure text is always a string
    const displayText = typeof text === 'string' ? text : String(text);
    const row = document.createElement('div');
    row.className = 'row';
    
    const msgDiv = document.createElement('div');
    msgDiv.className = 'msg user';
    
    // Add text content
    const textSpan = document.createElement('div');
    textSpan.innerHTML = formatTextWithLineBreaks(displayText);
    msgDiv.appendChild(textSpan);
    
    // Add image previews if any
    if (images && images.length > 0) {
      const previewContainer = document.createElement('div');
      previewContainer.className = 'user-image-previews';
      
      images.forEach(file => {
        const img = document.createElement('img');
        img.src = URL.createObjectURL(file);
        img.alt = file.name;
        img.title = file.name;
        
        // Revoke object URL after image loads to free memory
        img.onload = () => URL.revokeObjectURL(img.src);
        
        // Optional: click to view full size
        img.onclick = () => {
          window.open(img.src, '_blank');
        };
        
        previewContainer.appendChild(img);
      });
      
      msgDiv.appendChild(previewContainer);
    }
    
    row.appendChild(msgDiv);
    chatContainer.appendChild(row);
    scrollBottom();
  }

  function addAssistantBlock(chatContainer) {
    const row = document.createElement('div');
    row.className = 'row';
    const box = document.createElement('div');
    box.className = 'msg assistant';
    box.style.position = 'relative'; // Enable absolute positioning for request ID
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
              <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
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
              <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
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
        // Right (0deg) when collapsed, down (90deg) when expanded
        arrow.style.transform = (body.style.display === 'none') ? 'rotate(0deg)' : 'rotate(90deg)';
      }

      header.addEventListener('click', () => {
        const body = header.nextElementSibling;
        if (body && body.classList.contains('container-body')) {
          const isHidden = body.style.display === 'none';
          body.style.display = isHidden ? 'block' : 'none';
          const arrow = header.querySelector('.toggle-arrow svg');
          if (arrow) {
            // rotate to down when expanded
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
  const treeNodes = new Map(); // requestId -> { element, parentId, depth, children:Set }
  const pendingChildren = new Map(); // parentId -> [{elementInfo}]

  function toggleTreeNode(requestId) {
    const node = treeNodes.get(requestId);
    if (!node) return;
    const el = node.element;
    if (!el) return;
    const expanded = el.getAttribute('data-expanded') === 'true';
    const newState = !expanded;
    el.setAttribute('data-expanded', newState ? 'true' : 'false');
  const icon = el.querySelector('.tree-expand-btn .expand-icon');
  if (icon) icon.style.transform = newState ? 'rotate(90deg)' : 'rotate(0deg)';
    setDescendantsVisibility(requestId, newState);
  }

  function setDescendantsVisibility(rootId, rootVisible) {
    const queue = [...(treeNodes.get(rootId)?.children || [])];
    while (queue.length) {
      const cid = queue.shift();
      const cn = treeNodes.get(cid);
      if (!cn) continue;
      
      // If we're collapsing (rootVisible = false), hide all descendants
      // If we're expanding (rootVisible = true), only show if all ancestors are expanded
      const shouldBeVisible = rootVisible ? isAllAncestorsExpanded(cid) : false;
      cn.element.style.display = shouldBeVisible ? 'block' : 'none';
      
      // Always traverse deeper to hide/show all descendants
      queue.push(...cn.children);
    }
  }

  function isAllAncestorsExpanded(requestId) {
    let current = treeNodes.get(requestId);
    while (current && current.parentId) {
      const parent = treeNodes.get(current.parentId);
      if (!parent) return false;
      if (parent.element.getAttribute('data-expanded') !== 'true') return false;
      current = parent;
    }
    return true;
  }
  
  function updateParentExpandButton(requestId) {
    const node = treeNodes.get(requestId);
    if (!node) return;
    const btn = node.element.querySelector('.tree-expand-btn');
    if (!btn) return;
    if (node.children.size > 0) {
      btn.style.display = 'inline-block';
      const icon = btn.querySelector('.expand-icon');
      if (icon) icon.style.transform = node.element.getAttribute('data-expanded') === 'true' ? 'rotate(90deg)' : 'rotate(0deg)';
    } else {
      btn.style.display = 'none';
    }
  }

  function registerNode(requestId, parentId, element, depthLevel) {
    treeNodes.set(requestId, { element, parentId, depthLevel, children: new Set() });
    if (parentId) {
      const parentNode = treeNodes.get(parentId);
      if (parentNode) {
        parentNode.children.add(requestId);
        updateParentExpandButton(parentId);
        // Check full ancestor chain to determine visibility
        const shouldBeVisible = isAllAncestorsExpanded(requestId);
        element.style.display = shouldBeVisible ? 'block' : 'none';
      } else {
        // Queue child until parent arrives
        element.style.display = 'none';
        if (!pendingChildren.has(parentId)) pendingChildren.set(parentId, []);
        pendingChildren.get(parentId).push({ requestId, element, depthLevel });
      }
    }
    attachPendingChildren(requestId);
  }

  function attachPendingChildren(parentId) {
    const waiting = pendingChildren.get(parentId);
    if (!waiting) return;
    const parentNode = treeNodes.get(parentId);
    if (!parentNode) return;
    for (const child of waiting) {
      parentNode.children.add(child.requestId);
      updateParentExpandButton(parentId);
      // Check full ancestor chain to determine visibility
      const shouldBeVisible = isAllAncestorsExpanded(child.requestId);
      child.element.style.display = shouldBeVisible ? 'block' : 'none';
    }
    pendingChildren.delete(parentId);
  }

  function createTreeOperationDiv(operationKey, ev, depthLevel, parentId) {
    const operationDiv = document.createElement('div');
    operationDiv.className = 'operation-progress';
    operationDiv.setAttribute('data-operation', operationKey);
    operationDiv.setAttribute('data-request-id', ev.request_id || '');
    operationDiv.setAttribute('data-depth', depthLevel);
    operationDiv.setAttribute('data-expanded', 'true'); // Default to expanded
    
    const reqSpan = ev.request_id ? `<span class="operation-request-id">${escapeHtml(ev.request_id)}</span>` : '';
    
    // Tree connector/expand button - unified approach with same SVG arrow as section headers
    const arrowSvg = `<svg class="expand-icon" width="10" height="10" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    let treeIndicator = '';
    if (depthLevel > 0) {
      treeIndicator = `
        <span class="tree-indicator">
          <span class="tree-expand-btn" data-request-id="${ev.request_id || ''}" style="display: none;">
            ${arrowSvg}
          </span>
          <span class="tree-connector">└─</span>
        </span>`;
    } else {
      treeIndicator = `
        <span class="tree-indicator">
          <span class="tree-expand-btn" data-request-id="${ev.request_id || ''}" style="display: none;">
            ${arrowSvg}
          </span>
        </span>`;
    }
    
    operationDiv.innerHTML = `
      <div class="progress-line" style="padding-left: ${depthLevel * 16}px;">
        ${treeIndicator}
        <span class="progress-icon"><div class="spinner"></div></span>
        <span class="progress-time">${formatTime(ev.timestamp)}</span>
        ${reqSpan}
        <span class="progress-server">${escapeHtml(ev.server || 'Unknown')}</span>
        <span class="progress-message">${escapeHtml(ev.message || (ev.phase === 'start' ? 'Starting...' : 'In progress...'))}</span>
      </div>
    `;
    
    // Add click handler for expand/collapse
    const expandBtn = operationDiv.querySelector('.tree-expand-btn');
    if (expandBtn) {
      expandBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleTreeNode(ev.request_id || '');
      });
    }
    
    return operationDiv;
  }

  function insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel) {
    if (!parentId) {
      // Root level - append at end
      container.appendChild(operationDiv);
      return;
    }
    
    // Find parent element
    const parentNode = treeNodes.get(parentId);
    if (parentNode && parentNode.element) {
      // Collect all children with their request IDs for sorting
      const childElements = [];
      for (const childId of parentNode.children) {
        const childNode = treeNodes.get(childId);
        if (childNode && childNode.element && container.contains(childNode.element)) {
          childElements.push({ id: childId, element: childNode.element });
        }
      }
      
      // Sort children by request ID (preserves chronological order from backend)
      childElements.sort((a, b) => {
        // Extract sequence numbers from request IDs for comparison
        // e.g., "rbwytmgxyt_006_015" -> compare last segment (015)
        const aSeq = a.id.split('_').pop();
        const bSeq = b.id.split('_').pop();
        return aSeq.localeCompare(bSeq);
      });
      
      // Find correct insertion position among sorted siblings
      let insertAfter = parentNode.element;
      const newSeq = requestId.split('_').pop();
      
      for (const child of childElements) {
        const childSeq = child.id.split('_').pop();
        if (childSeq.localeCompare(newSeq) < 0) {
          insertAfter = child.element;
        } else {
          break; // Found first sibling that should come after new element
        }
      }
      
      // Insert after the determined position
      if (insertAfter.nextSibling) {
        container.insertBefore(operationDiv, insertAfter.nextSibling);
      } else {
        container.appendChild(operationDiv);
      }
    } else {
      // Parent not found, append at end
      container.appendChild(operationDiv);
    }
  }

  function addStatusEvent(container, ev) {
    if (!container || !ev) return;
    const statusSection = container.closest('.container-section');
    if (statusSection && statusSection.style.display === 'none') {
      statusSection.style.display = 'block';
    }
    
    // Use request_id for hierarchical operations if available
    const requestId = ev.request_id && ev.request_id !== 'default' ? ev.request_id : null;
    const operationKey = requestId || ev.server;
    
    // Get tree hierarchy metadata
    const treeInfo = ev.tree || { parent_id: null, depth_level: 0, child_count: 0, is_leaf: true };
    let depthLevel = treeInfo.depth_level || 0;
    let parentId = treeInfo.parent_id;

    // Treat special/placeholder parent ids (e.g. backend using a label instead of null) as null roots
    if (parentId && !treeNodes.has(parentId) && parentId.indexOf('_') === -1 && depthLevel === 1) {
      // If backend gives parent like "main" for first real root, normalize to null so it shows
      parentId = null;
      depthLevel = 0;
    }
    
    // Generic request_id parsing if backend does not supply tree metadata
    if (!parentId && requestId && requestId.includes('_')) {
      const parts = requestId.split('_');
      if (parts.length >= 2) {
        // For multi-level IDs like q2f381f3v6_006_015_016:
        // - parts = ['q2f381f3v6', '006', '015', '016']
        // - parentId should be 'q2f381f3v6_006_015' (all parts except last)
        // For two-level IDs like q2f381f3v6_006:
        // - parts = ['q2f381f3v6', '006']
        // - parentId should be 'q2f381f3v6' (first part only, which is the root)
        parentId = parts.slice(0, -1).join('_');
        // Depth: q2f381f3v6 = 0, q2f381f3v6_006 = 1, q2f381f3v6_006_015 = 2, etc.
        depthLevel = parts.length - 1;
        
        // Auto-create virtual parent node if it doesn't exist yet
        if (!treeNodes.has(parentId)) {
          const virtualParent = document.createElement('div');
          virtualParent.className = 'operation-progress virtual-parent';
          virtualParent.setAttribute('data-operation', parentId);
          virtualParent.setAttribute('data-request-id', parentId);
          virtualParent.setAttribute('data-depth', depthLevel - 1);
          virtualParent.setAttribute('data-expanded', 'true');
          virtualParent.style.display = 'none'; // Hidden by default, will be made visible if needed
          
          // Recursively determine this parent's parent for proper hierarchy
          let grandParentId = null;
          if (parentId.includes('_')) {
            const parentParts = parentId.split('_');
            if (parentParts.length >= 2) {
              grandParentId = parentParts.slice(0, -1).join('_');
            }
          }
          
          container.appendChild(virtualParent);
          registerNode(parentId, grandParentId, virtualParent, depthLevel - 1);
        }
      }
    }
    
    // Prefer server-provided sequence number for ordering when available
    const seq = ev.meta && ev.meta._seq ? ev.meta._seq : null;
    if (ev.phase === 'start') {
      if (activeOperations.has(operationKey)) {
        const existing = activeOperations.get(operationKey);
        const msg = existing.querySelector('.progress-message');
        const time = existing.querySelector('.progress-time');
        if (msg) msg.textContent = ev.message || 'Starting...';
        if (time) time.textContent = formatTime(ev.timestamp);
      } else {
        const operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        
        // Insert at correct hierarchical position
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        
        activeOperations.set(operationKey, operationDiv);
        // Register node with requestId (if available) for tree structure
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
    } else if (ev.phase === 'progress') {
      let operationDiv = activeOperations.get(operationKey);
      // If progress arrives before start, create a row from this progress event
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        
        // Insert at correct hierarchical position
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        
        activeOperations.set(operationKey, operationDiv);
        // Register node with requestId (if available) for tree structure
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      } else {
        const messageSpan = operationDiv.querySelector('.progress-message');
        const timeSpan = operationDiv.querySelector('.progress-time');
        if (messageSpan) messageSpan.textContent = ev.message || 'In progress...';
        if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
        // Update request id if present
        if (ev.request_id) {
          let req = operationDiv.querySelector('.operation-request-id');
          if (!req) {
            const span = document.createElement('span');
            span.className = 'operation-request-id';
            span.textContent = ev.request_id;
            const timeSpan = operationDiv.querySelector('.progress-time');
            if (timeSpan && timeSpan.parentNode) timeSpan.parentNode.insertBefore(span, timeSpan.nextSibling);
          } else {
            req.textContent = ev.request_id;
          }
        }
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
      // Keep tree structure intact for folding - don't clean up completed operations
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
      // Keep tree structure intact for folding - don't clean up errored operations
    }
  }

  function formatTime(ts) {
    try {
      return new Date(ts).toLocaleTimeString();
    } catch (e) {
      return ts;
    }
  }

  // Event source tracking (shared across init calls and cleanup)
  let currentEventSource = null;
  let currentStatusEventSource = null;
  
  // Streaming state tracking
  let currentStreamingContent = '';
  let currentStreamingStep = null;
  
  // Session and request tracking (shared across init and event listeners)
  let currentRequestId = null;
  let currentSessionId = null;

  // Public init function that wires the chat form behavior
  chatModule.init = function (opts) {
    const chatForm = document.getElementById('f');
    const taskInput = document.getElementById('task');
    const runBtn = document.getElementById('runBtn');
    const stopBtn = document.getElementById('stopBtn');
    const chatContainer = document.getElementById('chat');
    
    // Helper functions to update UI displays
    function updateHeaderSessionId() {
      console.log('updateHeaderSessionId called, currentSessionId:', currentSessionId);
      const sessionElement = document.getElementById('headerSessionId');
      if (sessionElement) {
        const sessionId = currentSessionId || '';
        // No 'Session:' prefix per design; leave empty when no session
        sessionElement.textContent = sessionId;
        const container = document.querySelector('.session-id-bottom');
        if (container) {
          container.style.display = currentSessionId ? 'block' : 'none';
          // set title to full id so users can hover to see it
          container.title = sessionId || '';
        }
      } else {
        console.warn('headerSessionId element not found');
      }
    }
    
    function updateRequestId() {
      console.log('updateRequestId called, currentRequestId:', currentRequestId);

      if (currentRequestId) {
        // Find the latest assistant message
        const latestAssistant = document.querySelector('.chat .row:last-child .msg.assistant');
        if (latestAssistant) {
          // Avoid inserting duplicate request id elements
          let existing = latestAssistant.querySelector('.message-request-id');
          if (!existing) {
            const requestIdElement = document.createElement('div');
            requestIdElement.className = 'message-request-id';
            requestIdElement.innerHTML = `Request: <span>${currentRequestId}</span>`;
            requestIdElement.title = `Request ID: ${currentRequestId}`;
            latestAssistant.appendChild(requestIdElement);
          } else {
            existing.innerHTML = `Request: <span>${currentRequestId}</span>`;
            existing.title = `Request ID: ${currentRequestId}`;
          }
        }
      }

      // Also update the global request display (keep for compatibility)
      const requestElement = document.getElementById('currentRequestId');
      const requestContainer = document.getElementById('requestIdDisplay');
      if (requestElement && requestContainer) {
        requestElement.textContent = currentRequestId || '--';
        requestContainer.style.display = 'none'; // Hide the global one, we use per-message now
      }
    }
    
    // Expose current session id for other modules (fallback for UI)
    chatModule.getCurrentSessionId = function() { return currentSessionId; };
    Object.defineProperty(chatModule, 'currentSessionId', {
      get: function() { return currentSessionId; }
    });
    // Also export to global window for older modules
    try { global.currentSessionId = currentSessionId; } catch (e) { /* ignore */ }
    
    // Event sources are now declared at module level (above init function)

    if (!chatForm || !taskInput || !runBtn || !stopBtn || !chatContainer) {
      console.warn('Chat form elements not found');
      return;
    }

    // Initialize UI displays
    updateHeaderSessionId();
    updateRequestId();

    // Stop button event listener
    stopBtn.addEventListener('click', async function() {
      if (currentRequestId) {
        // Sofortiges Feedback geben: preserve icon, update accessible label and tooltip
        stopBtn.setAttribute('title', 'Canceling');
        stopBtn.setAttribute('aria-label', 'Canceling');
        stopBtn.disabled = true;
        stopBtn.classList.add('cancelling');

        try {
          const response = await fetch(`/cancel/${currentRequestId}`, { method: 'POST' });
          const result = await response.json();
          console.log('Cancel request result:', result);

          // Kurze Verzögerung für besseres UX-Feedback
          setTimeout(() => {
            if (result.status === 'cancelled') {
              stopBtn.setAttribute('title', 'Done');
              stopBtn.setAttribute('aria-label', 'Done');
              stopBtn.classList.remove('cancelling');
              stopBtn.classList.add('cancelled');
            } else {
              stopBtn.setAttribute('title', 'Failed');
              stopBtn.setAttribute('aria-label', 'Failed');
              stopBtn.classList.remove('cancelling');
              stopBtn.classList.add('cancel-failed');
            }
          }, 500);

        } catch (error) {
          console.error('Failed to cancel request:', error);
          stopBtn.setAttribute('title', 'Failed');
          stopBtn.setAttribute('aria-label', 'Failed');
          stopBtn.classList.remove('cancelling');
          stopBtn.classList.add('cancel-failed');
        }

        // Nach 2 Sekunden wieder zurücksetzen (falls Anfrage noch läuft)
        setTimeout(() => {
          if (stopBtn.style.display !== 'none') { // Nur zurücksetzen wenn Button noch sichtbar
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          }
        }, 2000);
      }
    });

    chatForm.addEventListener('submit', async function(e) {
      e.preventDefault();
      const task = taskInput.value.trim();
      
      // Check if we have files to upload
      const hasFiles = window.fileUploadModule && window.fileUploadModule.hasValidFiles();
      const files = hasFiles ? window.fileUploadModule.getFiles() : [];
      
      // Require either task text or files
      if (!task && !hasFiles) return;
      
      // Add user message to chat (with file indicator if files present)
      let displayText = task || '(Image upload)';
      if (files.length > 0) {
        if (task) {
          displayText += ` [${files.length} image${files.length > 1 ? 's' : ''}]`;
        } else {
          displayText = `[${files.length} image${files.length > 1 ? 's' : ''}]`;
        }
      }
      addUser(chatContainer, displayText, files);
      taskInput.value = '';
      // Trigger input event so auto-resize logic recalculates height immediately
      try {
        const ev = new Event('input', { bubbles: true, cancelable: false });
        taskInput.dispatchEvent(ev);
      } catch (e) {
        // Older browsers fallback
        taskInput.dispatchEvent(document.createEvent('Event'));
      }

      // If there's an active request, append the user message to it
      // Note: Multimodal append not yet supported, only text append
      if (currentRequestId && currentEventSource && !hasFiles) {
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
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks('Failed to append message: ' + txt)}</div>`;
            return;
          }

          // appended successfully; the running agent will pick it up and continue
          return;

        } catch (err) {
          console.error('Failed to append to active request:', err);
          // fall through to starting a new request
        }
      }

      // No active request or has files: start a new request
      const blk = addAssistantBlock(chatContainer);
      runBtn.style.display = 'none'; // Hide run button
      stopBtn.style.display = 'block'; // Show stop button
      currentRequestId = null; // Will be set when SSE 'start' event arrives

      // Shared SSE event handler for both EventSource and manual fetch() parsing
      const handleSSEEvent = (data, blk) => {
        switch (data.type) {
          case 'start':
            currentRequestId = data.request_id;
            currentSessionId = data.session_id;
            // update exported values
            try { global.currentSessionId = currentSessionId; } catch (e) {}
            console.log('Request started with ID:', currentRequestId, 'Session ID:', currentSessionId);
            
            // Notify session manager about new/updated session
            if (window.sessionManager && typeof window.sessionManager.onSessionUpdated === 'function') {
              window.sessionManager.onSessionUpdated(currentSessionId);
            }
            
            // Update header session ID display
            updateHeaderSessionId();
            
            // Update request ID display  
            updateRequestId();
            break;
          case 'cancelled':
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text cancelled">Request cancelled at step ${data.step}</div>`;
            break;
          case 'thinking_delta':
            // Real-time token streaming from LLM
            if (data.step !== currentStreamingStep) {
              // New step - reset accumulator and add typing indicator
              currentStreamingContent = '';
              currentStreamingStep = data.step;
              
              if (!blk.think) {
                blk.think = document.createElement('pre');
                blk.think.className = 'think-section streaming';
                blk.r.appendChild(blk.think);
              }
              
              blk.think.textContent = `💭 Step ${data.step}: `;
              blk.think.classList.add('streaming');
              showSection(blk.think);
            }
            
            // Update with accumulated content + cursor
            currentStreamingContent = data.accumulated || '';
            if (blk.think) {
              blk.think.innerHTML = `💭 Step ${data.step}: ${escapeHtml(currentStreamingContent)}<span class="typing-cursor">|</span>`;
              // Auto-scroll to keep cursor visible
              blk.think.scrollIntoView({ behavior: 'smooth', block: 'end' });
            }
            break;
          case 'thinking':
            // Complete thinking event (also handles backward compatibility)
            // Clear streaming state when we get complete thinking event
            currentStreamingContent = '';
            currentStreamingStep = null;
            if (blk.think) {
              blk.think.classList.remove('streaming');
              // Remove typing cursor if present
              const cursor = blk.think.querySelector('.typing-cursor');
              if (cursor) cursor.remove();
            }
            
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
          case 'status':
            // Status events are now delivered through /events stream
            if (blk && blk.status) {
              addStatusEvent(blk.status, data);
            }
            break;
          case 'final':
            const content = data.summary || data.content || '';
            const contentFormat = data.content_format || 'text';
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text">${formatContent(content, contentFormat)}</div>`;
            
            // Apply Prism.js syntax highlighting if available and content is HTML
            if (contentFormat === 'html' && typeof Prism !== 'undefined') {
              Prism.highlightAllUnder(blk.t);
            }
            break;
          case 'end':
            if (currentEventSource) {
              currentEventSource.close();
              currentEventSource = null;
            }
            // Don't close statusEs here - let status updates continue after response completion
            runBtn.style.display = 'block'; // Show run button
            stopBtn.style.display = 'none'; // Hide stop button
            // Reset stop button state
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
            
            // Reload sessions after conversation completes
            if (window.sessionManager && typeof window.sessionManager.loadSessions === 'function') {
              window.sessionManager.loadSessions();
            }
            break;
          case 'error':
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(data.message)}</div>`;
            if (currentEventSource) {
              currentEventSource.close();
              currentEventSource = null;
            }
            if (currentStatusEventSource) {
              currentStatusEventSource.close();
              currentStatusEventSource = null;
            }
            runBtn.style.display = 'block'; // Show run button
            stopBtn.style.display = 'none'; // Hide stop button
            // Reset stop button state
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
            break;
        }
        scrollBottom();
      };

      // Use FormData for all requests (supports both text-only and multimodal)
      if (hasFiles) {
        // Build FormData for multimodal request
        const formData = new FormData();
        formData.append('task', task);
        files.forEach(file => {
          formData.append('files', file);
        });

        // Add agent and LLM profile selections if available
        const selectedAgent = window.selectorModule && window.selectorModule.getCurrentAgent ? window.selectorModule.getCurrentAgent() : null;
        const selectedLLMProfile = window.selectorModule && window.selectorModule.getCurrentLLMProfile ? window.selectorModule.getCurrentLLMProfile() : null;
        
        if (selectedAgent) {
          formData.append('agent_name', selectedAgent);
        }
        if (selectedLLMProfile) {
          formData.append('llm_profile', selectedLLMProfile);
        }
        
        // Add current session ID if exists (to continue existing session)
        if (currentSessionId) {
          formData.append('session_id', currentSessionId);
        }

        try {
          showSection(blk.t);
          blk.t.innerHTML = '<div class="response-text">Processing images...</div>';

          // Close any existing status event source before starting a new one
          if (currentStatusEventSource) {
            currentStatusEventSource.close();
            currentStatusEventSource = null;
          }
          
          // Status events now come through /events SSE stream - no separate connection needed

          // Stream SSE response from /run endpoint
          const response = await fetch('/run', {
            method: 'POST',
            body: formData
          });

          if (!response.ok) {
            const errorText = await response.text();
            let errorMsg = 'Request failed';
            try {
              const errorJson = JSON.parse(errorText);
              errorMsg = errorJson.detail || errorMsg;
            } catch (e) {
              errorMsg = errorText || errorMsg;
            }
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMsg)}</div>`;
            runBtn.style.display = 'block';
            stopBtn.style.display = 'none';
            return;
          }

                    // Response is SSE stream - parse it manually
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          let sseOk = false;

          while (true) {
            const {done, value} = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop(); // Keep incomplete line in buffer

            for (const line of lines) {
              if (line.startsWith(':')) {
                sseOk = true;
                continue;
              }
              if (line.startsWith('event:')) {
                continue;
              }
              if (line.startsWith('data:')) {
                const jsonStr = line.substring(5).trim();
                if (!jsonStr) continue;
                try {
                  const ev = JSON.parse(jsonStr);
                  handleSSEEvent(ev, blk);
                } catch (e) {
                  console.error('Failed to parse SSE data:', e);
                }
              }
            }
          }

          if (!sseOk) {
            showSection(blk.t);
            blk.t.innerHTML = '<div class="response-text error">SSE connection failed</div>';
          }

          // Clear files after successful send
          if (window.fileUploadModule) {
            window.fileUploadModule.clearFiles();
          }

        } catch (err) {
          showSection(blk.t);
          blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks('Request failed: ' + String(err))}</div>`;
          if (currentStatusEventSource) {
            currentStatusEventSource.close();
            currentStatusEventSource = null;
          }
        } finally {
          runBtn.style.display = 'block';
          stopBtn.style.display = 'none';
          currentRequestId = null;
          currentEventSource = null;
        }
        return;
      }

      // Text-only SSE-based request
      let sseOk = false;
      
      // Get current agent and LLM profile selections
      const selectedAgent = window.selectorModule && window.selectorModule.getCurrentAgent ? window.selectorModule.getCurrentAgent() : null;
      const selectedLLMProfile = window.selectorModule && window.selectorModule.getCurrentLLMProfile ? window.selectorModule.getCurrentLLMProfile() : null;
      
      // Build event URL with agent and profile parameters
      let eventUrl = `/events?task=${encodeURIComponent(task)}`;
      if (currentSessionId) {
        eventUrl += `&session_id=${encodeURIComponent(currentSessionId)}`;
      }
      if (selectedAgent) {
        eventUrl += `&agent_name=${encodeURIComponent(selectedAgent)}`;
      }
      if (selectedLLMProfile) {
        eventUrl += `&llm_profile=${encodeURIComponent(selectedLLMProfile)}`;
      }
      
      // Add authentication token as query parameter (EventSource doesn't support custom headers)
      const token = localStorage.getItem('token');
      if (token) {
        eventUrl += `&token=${encodeURIComponent(token)}`;
      }
      
      const es = new EventSource(eventUrl);
      currentEventSource = es; // Track current event source
      
      // Close any existing status event source before starting a new one
      if (currentStatusEventSource) {
        currentStatusEventSource.close();
        currentStatusEventSource = null;
      }
      
      // Status events now come through /events SSE stream - no separate connection needed

      es.onopen = () => { sseOk = true; };

      es.onmessage = ev => {
        try {
          const data = JSON.parse(ev.data);
          handleSSEEvent(data, blk);
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
            const contentFormat = j.content_format || 'text';
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text">${formatContent(content, contentFormat)}</div>`;
            if (contentFormat === 'html' && typeof Prism !== 'undefined') {
              Prism.highlightAllUnder(blk.t);
            }
          } catch (e) {
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks('Request failed: ' + String(e))}</div>`;
          }
          runBtn.style.display = 'block'; // Show run button
          stopBtn.style.display = 'none'; // Hide stop button
          // Reset stop button state
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');
          stopBtn.disabled = false;
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          // Keep currentRequestId and Request ID display visible
          currentEventSource = null;
          es.close();
          if (statusEs) {
            statusEs.close();
            currentStatusEventSource = null;
          }
        }
      }, 1500);

      es.onerror = () => {
        es.close();
        if (statusEs) {
          statusEs.close();
          currentStatusEventSource = null;
        }
        runBtn.style.display = 'block'; // Show run button
        stopBtn.style.display = 'none'; // Hide stop button
        // Reset stop button state
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        // Keep currentRequestId and Request ID display visible
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

  // Cleanup function to close all event sources
  function cleanup() {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
    }
    if (currentStatusEventSource) {
      currentStatusEventSource.close();
      currentStatusEventSource = null;
    }
  }
  
  // Expose functions for testing
  chatModule.addStatusEvent = addStatusEvent;
  chatModule.toggleTreeNode = toggleTreeNode;
  chatModule.cleanup = cleanup;
  
  // attach to global
  global.chatModule = chatModule;
  
  // Cleanup on page unload
  window.addEventListener('beforeunload', cleanup);
  
  // Listen for new conversation events
  window.addEventListener('session:new', () => {
    console.log('New conversation event received - clearing session');
    // Clear current session ID
    currentSessionId = null;
    try { global.currentSessionId = null; } catch (e) {}
    
    // Update header to clear session ID display
    if (typeof updateHeaderSessionId === 'function') {
      updateHeaderSessionId();
    }
  });
  
  // Listen for session load events
  window.addEventListener('session:loaded', (event) => {
    const { session } = event.detail;
    if (session && session.messages) {
      // Clear current chat
      const chatEl = document.getElementById('chat');
      if (chatEl) {
        chatEl.innerHTML = '';
      }
      
      // Restore messages
      session.messages.forEach(msg => {
        // Skip system messages and tool-related messages
        if (msg.role === 'system' || msg.role === 'tool') {
          return;
        }
        
        // Skip assistant messages with tool_calls (they're intermediate steps)
        if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
          return;
        }
        
        if (msg.role === 'user') {
          // Add user message
          const row = document.createElement('div');
          row.className = 'row';
          const msgDiv = document.createElement('div');
          msgDiv.className = 'msg user';
          const textSpan = document.createElement('div');
          textSpan.innerHTML = formatTextWithLineBreaks(msg.content || '');
          msgDiv.appendChild(textSpan);
          row.appendChild(msgDiv);
          chatEl.appendChild(row);
        } else if (msg.role === 'assistant' && msg.content) {
          const blk = addAssistantBlock(chatEl);
          showSection(blk.t);
          // Use formatContent to detect HTML vs plain text
          blk.t.innerHTML = `<div class="response-text">${formatContent(msg.content, msg.content_format)}</div>`;
          if (msg.content_format === 'html' && typeof Prism !== 'undefined') {
            Prism.highlightAllUnder(blk.t);
          }
        }
      });
      
      // Scroll to bottom
      scrollBottom();
      
      // Set current session ID for continuation
      currentSessionId = session.session_id;
      
      // Restore agent and LLM profile selectors
      if (session.agent_name && window.selectorModule) {
        window.selectorModule.setAgent(session.agent_name);
      }
      if (session.llm_profile && window.selectorModule) {
        window.selectorModule.setLLMProfile(session.llm_profile);
      }
      
      // Update session ID in header
      if (typeof updateHeaderSessionId === 'function') {
        updateHeaderSessionId(session.session_id);
      }
    }
  });

})(window);

