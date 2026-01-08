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

  function addUser(chatContainer, text, images = [], audioFiles = [], textFiles = []) {
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
    
    // Add audio previews if any
    if (audioFiles && audioFiles.length > 0) {
      const audioContainer = document.createElement('div');
      audioContainer.className = 'user-audio-previews';
      audioContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
      
      audioFiles.forEach((file, index) => {
        const audioWrapper = document.createElement('div');
        audioWrapper.style.cssText = 'display: flex; align-items: center; gap: 8px;';
        
        // Create object URL for the audio file
        const audioUrl = URL.createObjectURL(file);
        
        // Create play button
        const playBtn = document.createElement('button');
        playBtn.textContent = '▶️ ' + file.name;
        playBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em;';
        playBtn.title = 'Click to play';
        
        // Create hidden audio element
        const audioEl = document.createElement('audio');
        audioEl.src = audioUrl;
        audioEl.style.display = 'none';
        
        let isPlaying = false;
        playBtn.onclick = () => {
          if (isPlaying) {
            audioEl.pause();
            playBtn.textContent = '▶️ ' + file.name;
            isPlaying = false;
          } else {
            audioEl.play();
            playBtn.textContent = '⏸️ ' + file.name;
            isPlaying = true;
          }
        };
        
        audioEl.onended = () => {
          playBtn.textContent = '▶️ ' + file.name;
          isPlaying = false;
        };
        
        audioWrapper.appendChild(playBtn);
        audioWrapper.appendChild(audioEl);
        audioContainer.appendChild(audioWrapper);
      });
      
      msgDiv.appendChild(audioContainer);
    }
    
    // Add text file previews if any
    if (textFiles && textFiles.length > 0) {
      const textContainer = document.createElement('div');
      textContainer.className = 'user-text-file-previews';
      textContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
      
      textFiles.forEach((file, index) => {
        const textWrapper = document.createElement('div');
        textWrapper.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
        
        // Create view button
        const viewBtn = document.createElement('button');
        viewBtn.textContent = '📄 ' + file.name;
        viewBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em; text-align: left;';
        viewBtn.title = 'Click to view';
        
        // Create hidden content div
        const contentDiv = document.createElement('pre');
        contentDiv.style.cssText = 'display: none; margin: 0; padding: 10px; background: #2a2a2a; border: 1px solid #444; border-radius: 4px; max-height: 300px; overflow: auto; font-size: 0.85em; white-space: pre-wrap; word-wrap: break-word;';
        
        viewBtn.onclick = () => {
          // Toggle content display
          if (contentDiv.style.display === 'none') {
            contentDiv.style.display = 'block';
            viewBtn.textContent = '📄 ' + file.name + ' ▼';
          } else {
            contentDiv.style.display = 'none';
            viewBtn.textContent = '📄 ' + file.name;
          }
        };
        
        // Read file content
        const reader = new FileReader();
        reader.onload = (e) => {
          contentDiv.textContent = e.target.result;
        };
        reader.readAsText(file);
        
        textWrapper.appendChild(viewBtn);
        textWrapper.appendChild(contentDiv);
        textContainer.appendChild(textWrapper);
      });
      
      msgDiv.appendChild(textContainer);
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
      // If parent doesn't exist yet, assume it's expanded (will be created later)
      if (!parent) return true;
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
      // Root level - insert in sorted order by request ID
      if (!requestId) {
        container.appendChild(operationDiv);
        return;
      }
      
      // Find all root-level operations (those with same depth)
      const rootElements = [];
      for (const [nodeId, nodeInfo] of treeNodes.entries()) {
        if (nodeInfo.depth === depthLevel && !nodeInfo.parent && container.contains(nodeInfo.element)) {
          rootElements.push({ id: nodeId, element: nodeInfo.element });
        }
      }
      
      // Sort by request ID
      rootElements.sort((a, b) => {
        const aSeq = a.id.split('_').pop();
        const bSeq = b.id.split('_').pop();
        return aSeq.localeCompare(bSeq);
      });
      
      // Find insertion position
      const newSeq = requestId.split('_').pop();
      let insertBefore = null;
      
      for (const root of rootElements) {
        const rootSeq = root.id.split('_').pop();
        if (rootSeq.localeCompare(newSeq) > 0) {
          insertBefore = root.element;
          break;
        }
      }
      
      // Insert at correct position
      if (insertBefore) {
        container.insertBefore(operationDiv, insertBefore);
      } else {
        container.appendChild(operationDiv);
      }
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
    
    // Get tree hierarchy metadata from backend (already calculated correctly)
    const treeInfo = ev.tree || { parent_id: null, depth_level: 0, child_count: 0, is_leaf: true };
    const depthLevel = treeInfo.depth_level || 0;
    const parentId = treeInfo.parent_id || null;
    
    // Auto-create virtual parent if needed (parent_id given but not yet in tree)
    if (parentId && !treeNodes.has(parentId)) {
      const virtualParent = document.createElement('div');
      virtualParent.className = 'operation-progress virtual-parent';
      virtualParent.setAttribute('data-operation', parentId);
      virtualParent.setAttribute('data-request-id', parentId);
      virtualParent.setAttribute('data-depth', depthLevel - 1);
      virtualParent.setAttribute('data-expanded', 'true');
      virtualParent.style.display = 'block'; // Visible so children can be displayed
      
      container.appendChild(virtualParent);
      // Register without a grandparent - will be filled in when parent's parent arrives
      registerNode(parentId, null, virtualParent, depthLevel - 1);
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
      let operationDiv = activeOperations.get(operationKey);
      // If END arrives before START was processed, create the operation now
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
      
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      const timeSpan = operationDiv.querySelector('.progress-time');
      // Respect backend hint to suppress the completion icon for internal helpers
      const suppressIcon = ev.meta && ev.meta.suppress_completion_icon;
      if (iconSpan) iconSpan.innerHTML = suppressIcon ? '' : '<div class="checkmark">✓</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Completed';
      if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
      operationDiv.classList.add('completed');
      activeOperations.delete(operationKey);
      // Keep tree structure intact for folding - don't clean up completed operations
    } else if (ev.phase === 'error') {
      let operationDiv = activeOperations.get(operationKey);
      // If ERROR arrives before START was processed, create the operation now
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
      
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      const timeSpan = operationDiv.querySelector('.progress-time');
      if (iconSpan) iconSpan.innerHTML = '<div class="error-mark">✕</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Error occurred';
      if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
      operationDiv.classList.add('error');
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
  let closeEventSourceTimer = null; // Timer to delay closing EventSource after final/end
  
  // SSE Reconnection state for long-running requests
  let sseReconnectAttempts = 0;
  const SSE_MAX_RECONNECT_ATTEMPTS = 5;
  const SSE_BASE_RECONNECT_DELAY_MS = 1000; // Start with 1s, doubles each retry
  let sseReconnectTimer = null;
  let sseReceivedFinalOrEnd = false; // Track if we've completed normally
  
  // Streaming state tracking
  let currentStreamingContent = '';
  let currentStreamingStep = null;
  
  // Session and request tracking (shared across init and event listeners)
  let currentRequestId = null;
  // Initialize from sessionStorage to handle page refresh before session:loaded event fires
  let currentSessionId = sessionStorage.getItem('lastSessionId') || null;
  if (currentSessionId) {
    console.log('[chat_module] Initialized currentSessionId from sessionStorage:', currentSessionId);
  }

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

        // Timeout: Nach 60 Sekunden automatisch zurücksetzen falls Backend nicht antwortet
        const timeoutId = setTimeout(() => {
          console.warn('Cancel request timeout after 60 seconds');
          stopBtn.setAttribute('title', 'Timeout');
          stopBtn.setAttribute('aria-label', 'Timeout');
          stopBtn.classList.remove('cancelling');
          stopBtn.classList.add('cancel-failed');
          
          // Nach weiteren 2 Sekunden komplett zurücksetzen und UI wiederherstellen
          setTimeout(() => {
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
            
            // Clear any pending close timer
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
            
            // UI zurücksetzen: Run-Button anzeigen, Stop-Button verstecken
            runBtn.style.display = 'block';
            stopBtn.style.display = 'none';
            currentRequestId = null;
            currentEventSource = null;
          }, 2000);
        }, 60000); // 60 Sekunden

        try {
          const response = await fetch(`/cancel/${currentRequestId}`, { method: 'POST' });
          const result = await response.json();
          console.log('Cancel request result:', result);
          
          // Timeout abbrechen da Antwort erhalten
          clearTimeout(timeoutId);

          // Wenn Request nicht gefunden wurde (z.B. nach Server-Neustart), State clearen
          if (result.status === 'not_found') {
            console.warn('Request not found - clearing stale state (possible server restart)');
            // Clear any pending close timer
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
            currentRequestId = null;
            currentEventSource = null;
            // UI zurücksetzen
            runBtn.style.display = 'block';
            stopBtn.style.display = 'none';
            stopBtn.classList.remove('cancelling');
            stopBtn.disabled = false;
            return; // Frühzeitig beenden
          }

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
          // Timeout abbrechen da Fehler erhalten
          clearTimeout(timeoutId);
          
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
      
      // Add user message to chat
      let displayText = task || '';
      // Get file breakdown by type from file upload module
      const filesByType = window.fileUploadModule ? window.fileUploadModule.getFilesByType() : { images: [], audio: [], text: [] };
      
      // Pass images, audio and text files separately to addUser
      addUser(chatContainer, displayText, filesByType.images, filesByType.audio, filesByType.text);
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
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          stopBtn.disabled = false;
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');

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
      stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
      stopBtn.disabled = false;
      stopBtn.setAttribute('title', 'Stop');
      stopBtn.setAttribute('aria-label', 'Stop');
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
          case 'heartbeat':
            // Keep-alive heartbeat during long LLM calls - ignore but log in debug mode
            if (window.DEBUG_MODE) {
              console.log('Heartbeat received (step', data.step, ')');
            }
            break;
          case 'cancelled':
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text cancelled">Request cancelled at step ${data.step}</div>`;
            break;
          case 'thinking_delta':
            // Real-time token streaming from LLM - stream directly to response box
            if (data.step !== currentStreamingStep) {
              // New step - reset accumulator
              currentStreamingContent = '';
              currentStreamingStep = data.step;
            }
            
            // Update with accumulated content + cursor directly in response box
            currentStreamingContent = data.accumulated || '';
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text streaming">${formatTextWithLineBreaks(currentStreamingContent)}<span class="typing-cursor">|</span></div>`;
            
            // Auto-scroll to keep cursor visible
            blk.t.scrollIntoView({ behavior: 'smooth', block: 'end' });
            break;
          case 'thinking_complete':
            // Final thinking event from streaming - remove cursor, keep content
            currentStreamingContent = '';
            currentStreamingStep = null;
            
            if (data.assistant && data.assistant.content) {
              // Content was already displayed via thinking_delta
              // Now show final formatted content (HTML from format_output hook)
              const contentFormat = data.content_format || 'text';
              showSection(blk.t);
              blk.t.innerHTML = `<div class="response-text">${formatContent(data.assistant.content, contentFormat)}</div>`;
              
              // Apply Prism.js syntax highlighting if available and content is HTML
              if (contentFormat === 'html' && typeof Prism !== 'undefined') {
                Prism.highlightAllUnder(blk.t);
              }
            }
            
            // Note: Tool calls display is handled by the 'thinking' event to avoid duplicates
            break;
          case 'thinking':
            // Complete thinking event (also handles backward compatibility)
            // Only clear streaming state if this has actual content (final thinking event)
            if (data.assistant) {
              // Final thinking event with content - clear streaming state
              currentStreamingContent = '';
              currentStreamingStep = null;
              if (blk.think) {
                blk.think.classList.remove('streaming');
                // Remove typing cursor if present
                const cursor = blk.think.querySelector('.typing-cursor');
                if (cursor) cursor.remove();
              }
              
              // Create think section if not exists
              if (!blk.think) {
                blk.think = document.createElement('pre');
                blk.think.className = 'think-section';
                blk.r.appendChild(blk.think);
              }
              
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
              showSection(blk.think);
            } else {
              // Step marker event (before LLM call) - don't interfere with streaming
              // Just ensure think section exists
              if (!blk.think) {
                blk.think = document.createElement('pre');
                blk.think.className = 'think-section';
                blk.r.appendChild(blk.think);
              }
            }
            break;
          case 'status':
            // Status events are now delivered through /events stream
            // Show status events for this request AND all hierarchical children (sub-agents)
            // e.g., if currentRequestId is "abc123", also show "abc123_sub_001", "abc123_001_sub_002", etc.
            if (blk && blk.status) {
              const eventRequestId = data.request_id || '';
              // Check if this event belongs to current request hierarchy
              // Either exact match OR starts with current request_id followed by underscore (child operation)
              const matches = eventRequestId === currentRequestId || 
                  (eventRequestId && currentRequestId && eventRequestId.startsWith(currentRequestId + '_'));
              
              if (matches) {
                addStatusEvent(blk.status, data);
              }
              // Otherwise silently ignore status from other requests/sessions
            }
            break;
          case 'status_batch':
            // Batched status events for efficiency (multiple events in one SSE message)
            if (blk && blk.status && data.events && Array.isArray(data.events)) {
              data.events.forEach(statusEvent => {
                const eventRequestId = statusEvent.request_id || '';
                const matches = eventRequestId === currentRequestId || 
                    (eventRequestId && currentRequestId && eventRequestId.startsWith(currentRequestId + '_'));
                
                if (matches) {
                  addStatusEvent(blk.status, statusEvent);
                }
              });
            }
            break;
          case 'final':
            // Mark completion for reconnect logic
            sseReceivedFinalOrEnd = true;
            sseReconnectAttempts = 0;
            
            // Only show final if content box is still empty (no streaming happened)
            // or if it's a different format
            const content = data.summary || data.content || '';
            const contentFormat = data.content_format || 'text';
            
            if (!blk.t.innerHTML || blk.t.innerHTML.trim() === '') {
              // No streaming happened, show final content
              showSection(blk.t);
              blk.t.innerHTML = `<div class="response-text">${formatContent(content, contentFormat)}</div>`;
              
              // Apply Prism.js syntax highlighting if available and content is HTML
              if (contentFormat === 'html' && typeof Prism !== 'undefined') {
                Prism.highlightAllUnder(blk.t);
              }
            }
            // If streaming already filled the content, skip this (content already there)
            break;
          case 'end':
            // Mark completion for reconnect logic
            sseReceivedFinalOrEnd = true;
            sseReconnectAttempts = 0;
            
            // Close EventSource immediately to prevent auto-reconnect attempts
            // EventSource will try to reconnect if the server closes the connection,
            // which causes spurious "Connection failed" errors in the onerror handler
            if (currentEventSource) {
              currentEventSource.close();
              currentEventSource = null;
            }
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
            
            // Clear any pending reconnect timer
            if (sseReconnectTimer) {
              clearTimeout(sseReconnectTimer);
              sseReconnectTimer = null;
            }
            
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
            // Clear any pending close timer
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
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
          case 'cancelled':
            // Request was cancelled - clean up and reset UI
            console.log('Request cancelled:', data.request_id, 'at step', data.step);
            // Clear any pending close timer
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
            if (currentEventSource) {
              currentEventSource.close();
              currentEventSource = null;
            }
            if (currentStatusEventSource) {
              currentStatusEventSource.close();
              currentStatusEventSource = null;
            }
            // Show cancelled status with step number
            showSection(blk.t);
            const stepInfo = data.step ? ` at step ${data.step}` : '';
            blk.t.innerHTML = `<div class="response-text" style="opacity: 0.6;">Request cancelled${stepInfo}</div>`;
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
        // Fallback to sessionStorage if currentSessionId not yet set (race condition on page load)
        const effectiveSessionId = currentSessionId || sessionStorage.getItem('lastSessionId');
        if (effectiveSessionId) {
          formData.append('session_id', effectiveSessionId);
          console.log('[chat_module] Using session_id:', effectiveSessionId, currentSessionId ? '(from memory)' : '(from sessionStorage fallback)');
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
            // Reset stop button state
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
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
          // Clear any pending close timer
          if (closeEventSourceTimer) {
            clearTimeout(closeEventSourceTimer);
            closeEventSourceTimer = null;
          }
          if (currentStatusEventSource) {
            currentStatusEventSource.close();
            currentStatusEventSource = null;
          }
        } finally {
          // Clear any pending close timer
          if (closeEventSourceTimer) {
            clearTimeout(closeEventSourceTimer);
            closeEventSourceTimer = null;
          }
          runBtn.style.display = 'block';
          stopBtn.style.display = 'none';
          // Reset stop button state
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');
          stopBtn.disabled = false;
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          currentRequestId = null;
          currentEventSource = null;
        }
        return;
      }

      // Text-only SSE-based request
      let sseOk = false;
      
      // Reset reconnect state for new request
      sseReceivedFinalOrEnd = false;
      sseReconnectAttempts = 0;
      if (sseReconnectTimer) {
        clearTimeout(sseReconnectTimer);
        sseReconnectTimer = null;
      }
      
      // Get current agent and LLM profile selections
      const selectedAgent = window.selectorModule && window.selectorModule.getCurrentAgent ? window.selectorModule.getCurrentAgent() : null;
      const selectedLLMProfile = window.selectorModule && window.selectorModule.getCurrentLLMProfile ? window.selectorModule.getCurrentLLMProfile() : null;
      
      // Fallback to sessionStorage if currentSessionId not yet set (race condition on page load)
      const effectiveSessionId = currentSessionId || sessionStorage.getItem('lastSessionId');
      
      // Build event URL with agent and profile parameters
      let eventUrl = `/events?task=${encodeURIComponent(task)}`;
      if (effectiveSessionId) {
        eventUrl += `&session_id=${encodeURIComponent(effectiveSessionId)}`;
        console.log('[chat_module] SSE using session_id:', effectiveSessionId, currentSessionId ? '(from memory)' : '(from sessionStorage fallback)');
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
          // Clear any pending close timer
          if (closeEventSourceTimer) {
            clearTimeout(closeEventSourceTimer);
            closeEventSourceTimer = null;
          }
          currentEventSource = null;
          es.close();
          if (currentStatusEventSource) {
            currentStatusEventSource.close();
            currentStatusEventSource = null;
          }
        }
      }, 1500);

      es.onerror = (event) => {
        // Log connection error with available state information
        const readyStateNames = ['CONNECTING', 'OPEN', 'CLOSED'];
        const readyState = readyStateNames[es.readyState] || es.readyState;
        console.warn('[SSE] Connection error, readyState:', readyState, 'event:', event, 'attempts:', sseReconnectAttempts);
        
        // Close current connection
        es.close();
        if (currentStatusEventSource) {
          currentStatusEventSource.close();
          currentStatusEventSource = null;
        }
        
        // Clear any pending close timer
        if (closeEventSourceTimer) {
          clearTimeout(closeEventSourceTimer);
          closeEventSourceTimer = null;
        }
        
        // If we've already received final/end, this is just cleanup - don't show error
        if (sseReceivedFinalOrEnd) {
          console.log('[SSE] Connection closed after completion, ignoring error');
          currentEventSource = null;
          return;
        }
        
        // Check if we should attempt reconnection (for long-running batch jobs)
        // Only reconnect if:
        // 1. We haven't exceeded max attempts
        // 2. We have an active request ID (something is still running)
        // 3. Connection was lost mid-stream (not initial connection failure)
        const shouldReconnect = sseReconnectAttempts < SSE_MAX_RECONNECT_ATTEMPTS 
            && currentRequestId 
            && sseOk; // sseOk means we received at least one event
        
        if (shouldReconnect) {
          sseReconnectAttempts++;
          const delay = SSE_BASE_RECONNECT_DELAY_MS * Math.pow(2, sseReconnectAttempts - 1);
          console.log(`[SSE] Will attempt reconnect #${sseReconnectAttempts} in ${delay}ms`);
          
          // Show reconnecting status
          if (blk && blk.status) {
            addStatusEvent(blk.status, {
              type: 'status',
              message: `Connection lost, reconnecting (attempt ${sseReconnectAttempts}/${SSE_MAX_RECONNECT_ATTEMPTS})...`,
              request_id: currentRequestId,
              timestamp: new Date().toISOString()
            });
          }
          
          // Schedule reconnect
          sseReconnectTimer = setTimeout(() => {
            if (!currentRequestId || sseReceivedFinalOrEnd) {
              console.log('[SSE] Reconnect cancelled - request completed or cancelled');
              return;
            }
            
            console.log(`[SSE] Attempting reconnect #${sseReconnectAttempts}`);
            
            // Create new EventSource to status endpoint for this request
            // Note: We can't resume the original stream, but we can poll for completion
            const statusUrl = `/request/${currentRequestId}/status`;
            fetch(statusUrl)
              .then(r => r.json())
              .then(status => {
                if (status.completed) {
                  // Request completed while we were disconnected
                  console.log('[SSE] Request completed during reconnect');
                  sseReceivedFinalOrEnd = true;
                  if (status.result) {
                    showSection(blk.t);
                    const content = status.result.summary || status.result.content || JSON.stringify(status.result);
                    const contentFormat = status.result.content_format || 'text';
                    blk.t.innerHTML = `<div class="response-text">${formatContent(content, contentFormat)}</div>`;
                  }
                  runBtn.style.display = 'block';
                  stopBtn.style.display = 'none';
                  sseReconnectAttempts = 0;
                } else if (status.error) {
                  // Request failed
                  console.warn('[SSE] Request failed:', status.error);
                  showSection(blk.t);
                  blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(status.error)}</div>`;
                  runBtn.style.display = 'block';
                  stopBtn.style.display = 'none';
                  sseReconnectAttempts = 0;
                } else {
                  // Request still running - schedule another check
                  console.log('[SSE] Request still running, scheduling next poll');
                  if (sseReconnectAttempts < SSE_MAX_RECONNECT_ATTEMPTS) {
                    sseReconnectTimer = setTimeout(() => {
                      // Trigger another onerror to continue polling
                      es.onerror(event);
                    }, SSE_BASE_RECONNECT_DELAY_MS * 2); // Poll every 2s while waiting
                  }
                }
              })
              .catch(err => {
                console.warn('[SSE] Status poll failed:', err);
                // Try again if we have attempts left
                if (sseReconnectAttempts < SSE_MAX_RECONNECT_ATTEMPTS) {
                  es.onerror(event);
                } else {
                  // Give up - show final error
                  showSection(blk.t);
                  const currentContent = blk.t.textContent || '';
                  if (!currentContent.trim() || currentContent.includes('Thinking')) {
                    blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks('Connection lost after ' + SSE_MAX_RECONNECT_ATTEMPTS + ' reconnect attempts')}</div>`;
                  } else {
                    const errorNotice = document.createElement('div');
                    errorNotice.className = 'response-text error';
                    errorNotice.innerHTML = formatTextWithLineBreaks('\n\n⚠️ Connection lost - please check batch status');
                    blk.t.appendChild(errorNotice);
                  }
                  runBtn.style.display = 'block';
                  stopBtn.style.display = 'none';
                  sseReconnectAttempts = 0;
                }
              });
          }, delay);
          
          currentEventSource = null;
          return; // Don't show error yet, we're reconnecting
        }
        
        // No reconnect possible - show error and reset UI
        const errorMessage = sseOk 
          ? 'Connection lost - possible timeout or network issue'
          : 'Connection failed - server may be unreachable';
        
        showSection(blk.t);
        const currentContent = blk.t.textContent || '';
        if (!currentContent.trim() || currentContent.includes('Thinking')) {
          blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMessage)}</div>`;
        } else {
          const errorNotice = document.createElement('div');
          errorNotice.className = 'response-text error';
          errorNotice.innerHTML = formatTextWithLineBreaks('\n\n⚠️ ' + errorMessage);
          blk.t.appendChild(errorNotice);
        }
        
        runBtn.style.display = 'block';
        stopBtn.style.display = 'none';
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        currentEventSource = null;
        sseReconnectAttempts = 0;
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
  
  // Also attach to AgentSystem namespace for consistency with other modules
  global.AgentSystem = global.AgentSystem || {};
  global.AgentSystem.ChatModule = chatModule;
  
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
    const { session, readOnly, reason } = event.detail;
    
    // CRITICAL: Don't override chat if an SSE request is currently streaming!
    // This prevents race condition where session restore overwrites live streaming output.
    if (currentEventSource) {
      console.warn('[session:loaded] Ignoring session load - SSE stream is active');
      return;
    }
    
    if (session && session.messages) {
      // Clear current chat
      const chatEl = document.getElementById('chat');
      if (chatEl) {
        chatEl.innerHTML = '';
      }
      
      // Restore messages
      // First pass: find the last assistant message to determine if we need a placeholder
      let lastAssistantMsg = null;
      for (let i = session.messages.length - 1; i >= 0; i--) {
        if (session.messages[i].role === 'assistant') {
          lastAssistantMsg = session.messages[i];
          break;
        }
      }
      
      session.messages.forEach((msg, index) => {
        // Skip system messages and tool-related messages
        if (msg.role === 'system' || msg.role === 'tool') {
          return;
        }
        
        if (msg.role === 'user') {
          // Add user message
          const row = document.createElement('div');
          row.className = 'row';
          const msgDiv = document.createElement('div');
          msgDiv.className = 'msg user';
          
          // Handle multimodal content (array) or simple string content
          let displayText = '';
          let images = [];
          let audioFiles = [];
          let textFiles = [];
          
          if (Array.isArray(msg.content)) {
            // Parse multimodal content array
            const textParts = [];
            msg.content.forEach(item => {
              if (item.type === 'text') {
                textParts.push(item.text);
              } else if (item.type === 'image_url' || item.type === 'image') {
                images.push(item);
              } else if (item.type === 'audio') {
                audioFiles.push(item);
              } else if (item.type === 'text_file') {
                textFiles.push(item);
              }
            });
            displayText = textParts.join(' ') || '(File upload)';
          } else {
            displayText = msg.content || '';
          }
          
          const textSpan = document.createElement('div');
          textSpan.innerHTML = formatTextWithLineBreaks(displayText);
          msgDiv.appendChild(textSpan);
          
          // Add image previews if any
          if (images.length > 0) {
            const previewContainer = document.createElement('div');
            previewContainer.className = 'user-image-previews';
            
            images.forEach(item => {
              const img = document.createElement('img');
              const imageUrl = item.image_url?.url || item.url;
              img.src = imageUrl;
              img.alt = 'Uploaded image';
              img.title = 'Click to view full size';
              
              // Optional: click to view full size
              img.onclick = () => {
                window.open(imageUrl, '_blank');
              };
              
              previewContainer.appendChild(img);
            });
            
            msgDiv.appendChild(previewContainer);
          }
          
          // Add audio previews if any
          if (audioFiles.length > 0) {
            const audioContainer = document.createElement('div');
            audioContainer.className = 'user-audio-previews';
            audioContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
            
            audioFiles.forEach((item, index) => {
              const audioWrapper = document.createElement('div');
              audioWrapper.style.cssText = 'display: flex; align-items: center; gap: 8px;';
              
              // Extract audio data URL - handle different formats
              // Format 1: item.audio_url (from session storage)
              // Format 2: item.audio.data (alternative format)
              // Format 3: item.data (fallback)
              const audioData = item.audio_url || item.audio?.data || item.data;
              const mediaType = item.audio?.media_type || item.media_type || 'audio/flac';
              const audioName = item.name || `Audio ${index + 1}`;
              
              if (audioData) {
                // Create play button that plays audio directly
                const playBtn = document.createElement('button');
                playBtn.textContent = '▶️ ' + audioName;
                playBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em;';
                playBtn.title = 'Click to play';
                
                // Create hidden audio element
                const audioEl = document.createElement('audio');
                audioEl.src = audioData;
                audioEl.style.display = 'none';
                
                let isPlaying = false;
                playBtn.onclick = () => {
                  if (isPlaying) {
                    audioEl.pause();
                    playBtn.textContent = '▶️ ' + audioName;
                    isPlaying = false;
                  } else {
                    audioEl.play();
                    playBtn.textContent = '⏸️ ' + audioName;
                    isPlaying = true;
                  }
                };
                
                audioEl.onended = () => {
                  playBtn.textContent = '▶️ ' + audioName;
                  isPlaying = false;
                };
                
                audioWrapper.appendChild(playBtn);
                audioWrapper.appendChild(audioEl);
              } else {
                // Fallback: Show audio indicator if data not found
                const indicator = document.createElement('span');
                indicator.textContent = `🔊 Audio ${index + 1}`;
                indicator.style.cssText = 'color: #888; font-size: 0.95em;';
                audioWrapper.appendChild(indicator);
              }
              
              audioContainer.appendChild(audioWrapper);
            });
            
            msgDiv.appendChild(audioContainer);
          }
          
          // Add text file previews if any
          if (textFiles.length > 0) {
            const textContainer = document.createElement('div');
            textContainer.className = 'user-text-file-previews';
            textContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
            
            textFiles.forEach((item, index) => {
              const textWrapper = document.createElement('div');
              textWrapper.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
              
              const fileName = item.name || `File ${index + 1}`;
              const fileContent = item.content || '';
              
              // Create view button
              const viewBtn = document.createElement('button');
              viewBtn.textContent = '📄 ' + fileName;
              viewBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em; width: fit-content;';
              viewBtn.title = 'Click to view';
              
              // Create hidden content div
              const contentDiv = document.createElement('pre');
              contentDiv.style.cssText = 'display: none; margin: 0; padding: 10px; background: #2a2a2a; border: 1px solid #444; border-radius: 4px; max-height: 300px; overflow: auto; font-size: 0.85em; white-space: pre-wrap;';
              contentDiv.textContent = fileContent;
              
              viewBtn.onclick = () => {
                // Toggle content display
                if (contentDiv.style.display === 'none') {
                  contentDiv.style.display = 'block';
                  viewBtn.textContent = '📄 ' + fileName + ' ▼';
                } else {
                  contentDiv.style.display = 'none';
                  viewBtn.textContent = '📄 ' + fileName;
                }
              };
              
              textWrapper.appendChild(viewBtn);
              textWrapper.appendChild(contentDiv);
              textContainer.appendChild(textWrapper);
            });
            
            msgDiv.appendChild(textContainer);
          }
          
          row.appendChild(msgDiv);
          chatEl.appendChild(row);
        } else if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
          // Only show placeholder for the LAST assistant message with tool_calls
          if (msg === lastAssistantMsg && !msg.content) {
            const blk = addAssistantBlock(chatEl);
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text"><em style="color: #888;">⚙️ Tool calls in progress...</em></div>`;
          }
          // Skip all other tool-call-only messages (they're intermediate steps)
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
    
    // Handle read-only mode AFTER rendering messages
    if (readOnly) {
      showReadOnlyBanner(reason);
      disableInput();
    } else {
      removeReadOnlyBanner();
      enableInput();
    }
  });
  
  // Helper functions for read-only mode
  function showReadOnlyBanner(reason) {
    removeReadOnlyBanner(); // Remove existing banner if any
    
    const banner = document.createElement('div');
    banner.id = 'readOnlyBanner';
    banner.className = 'read-only-banner';
    banner.innerHTML = `
      <div class="banner-content">
        <span class="banner-icon">🔒</span>
        <div class="banner-text">
          <strong>Read-Only Session</strong>
          <p>${reason || 'This session cannot be edited.'}</p>
        </div>
      </div>
    `;
    
    const chatEl = document.getElementById('chat');
    if (chatEl) {
      // Insert as first child of chat element (not before it)
      chatEl.insertBefore(banner, chatEl.firstChild);
    }
  }
  
  function removeReadOnlyBanner() {
    const banner = document.getElementById('readOnlyBanner');
    if (banner) {
      banner.remove();
    }
  }
  
  function disableInput() {
    const taskInput = document.getElementById('task');
    const runBtn = document.getElementById('runBtn');
    const fileInput = document.getElementById('fileInput');
    const fileUploadBtn = document.querySelector('.file-upload-btn');
    
    if (taskInput) {
      taskInput.disabled = true;
      taskInput.placeholder = 'This session is read-only';
      taskInput.style.opacity = '0.5';
    }
    if (runBtn) {
      runBtn.disabled = true;
      runBtn.style.opacity = '0.5';
    }
    if (fileInput) {
      fileInput.disabled = true;
    }
    if (fileUploadBtn) {
      fileUploadBtn.style.opacity = '0.5';
      fileUploadBtn.style.pointerEvents = 'none';
    }
  }
  
  function enableInput() {
    const taskInput = document.getElementById('task');
    const runBtn = document.getElementById('runBtn');
    const fileInput = document.getElementById('fileInput');
    const fileUploadBtn = document.querySelector('.file-upload-btn');
    
    if (taskInput) {
      taskInput.disabled = false;
      taskInput.placeholder = 'Ask the agent…';
      taskInput.style.opacity = '1';
    }
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.style.opacity = '1';
    }
    if (fileInput) {
      fileInput.disabled = false;
    }
    if (fileUploadBtn) {
      fileUploadBtn.style.opacity = '1';
      fileUploadBtn.style.pointerEvents = 'auto';
    }
  }

  // Public method to check if a request is active
  chatModule.hasActiveRequest = function() {
    return currentEventSource !== null || currentStatusEventSource !== null;
  };

  // Export chatModule to window
  global.chatModule = chatModule;

})(window);

