/**
 * Session Management Module
 * 
 * Handles UI for persistent conversation sessions:
 * - Session list sidebar
 * - Create/rename/delete sessions
 * - Load/restore sessions
 * - Auto-save current session
 */

export class SessionManager {
  constructor() {
    this.currentSessionId = null;
    this.sessions = [];          // root sessions (children load on expand)
    this.sessionById = new Map(); // flat lookup over everything loaded so far
    this.isAuthenticated = false;
    this.sidebarOpen = false;
    
    this.init();
  }

  async init() {
    // Check if user is authenticated
    await this.checkAuth();
    
    // Create UI elements
    this.createSidebar();
    this.createToggleButton();
    this.createRenameModal();
    
    // Load sessions (works for both authenticated and anonymous users)
    await this.loadSessions();
    
    // Restore last session from sessionStorage (tab-specific)
    // Delay slightly to ensure chat_module event listeners are registered
    setTimeout(async () => {
      // Don't auto-restore if a request is already active (user started new request quickly)
      if (this.isRequestActive()) {
        console.log('[SessionManager] Skipping auto-restore - request is active');
        return;
      }
      
      const lastSessionId = sessionStorage.getItem('lastSessionId');
      // Resolve server-side: the sidebar only holds root sessions, so a
      // sub-session would not be found by a client-side tree lookup.
      if (lastSessionId && await this.sessionExists(lastSessionId)) {
        // Load the session messages into the chat
        await this.loadSession(lastSessionId);
      }
    }, 100);
    
    // Set up event listeners
    this.setupEventListeners();
  }
  
  findSessionInHierarchy(sessionId) {
    // Sessions load lazily (roots up front, children on expand), so the flat
    // map of everything loaded is the authoritative lookup — walking
    // this.sessions would miss nodes that arrived via a child fetch.
    return (this.sessionById && this.sessionById.get(sessionId)) || null;
  }

  // Does this session exist server-side? Used for the auto-restore check, where
  // the id may point at a sub-session that the sidebar has not loaded.
  async sessionExists(sessionId) {
    const local = this.findSessionInHierarchy(sessionId);
    if (local) return true;
    try {
      const resp = await fetch(
        `/api/sessions/search?session_id=${encodeURIComponent(sessionId)}`,
        { credentials: 'include' }
      );
      if (!resp.ok) return false;
      const data = await resp.json();
      const found = (data.sessions || [])[0];
      if (found) this.rememberSessions([found]);
      return !!found;
    } catch (err) {
      console.error('Session lookup failed:', err);
      return false;
    }
  }

  async checkAuth() {
    try {
      const response = await fetch('/auth/me', {
        credentials: 'include'
      });
      this.isAuthenticated = response.ok;
    } catch (e) {
      this.isAuthenticated = false;
    }
  }

  createSidebar() {
    const sidebar = document.createElement('div');
    sidebar.className = 'sessions-sidebar';
    sidebar.id = 'sessionsSidebar';
    
    sidebar.innerHTML = `
      <div class="sessions-sidebar-header">
        <h2>Conversations</h2>
        <button class="sessions-close-btn" id="sessionsCloseBtn" aria-label="Close sidebar">×</button>
      </div>
      <div class="sessions-actions">
        <button class="sessions-new-btn" id="sessionsNewBtn">
          <svg viewBox="0 0 16 16" fill="currentColor">
            <path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
          New Conversation
        </button>
      </div>
      <div class="session-info-panel" id="sessionInfoPanel" style="display: none;">
        <div class="session-info-header">
          <span class="session-info-title">Session Info</span>
          <button class="session-info-close-btn" id="sessionInfoCloseBtn">×</button>
        </div>
        <div class="session-info-content" id="sessionInfoContent"></div>
      </div>
      <div class="sessions-list" id="sessionsList"></div>
    `;
    
    document.body.appendChild(sidebar);
  }

  createToggleButton() {
    const toggleBtn = document.createElement('button');
    toggleBtn.className = 'sessions-toggle-btn';
    toggleBtn.id = 'sessionsToggleBtn';
    toggleBtn.title = 'Toggle sessions sidebar';
    toggleBtn.setAttribute('aria-label', 'Toggle conversations');
    
    toggleBtn.innerHTML = `
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor">
        <path d="M6 4l4 4-4 4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `;
    
    document.body.appendChild(toggleBtn);
  }

  createRenameModal() {
    // Modal will be created dynamically when needed using the standard modal system
    // No need to create and append to DOM here
  }

  openRenameModal(sessionId, currentTitle) {
    this.renameSessionId = sessionId;
    
    // Create modal using exact User Management structure
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.id = 'sessionRenameModal';
    modal.style.display = 'block';  // Use 'block' not 'flex'!
    modal.innerHTML = `
      <div class="modal-content">
        <div class="modal-header">
          <h2>Rename Conversation</h2>
          <span class="close">&times;</span>
        </div>
        <form id="sessionRenameForm">
          <div class="form-group">
            <label for="sessionRenameInput">New Title</label>
            <input type="text" id="sessionRenameInput" name="title" value="${this.escapeHtml(currentTitle)}" required autofocus>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary modal-cancel">Cancel</button>
            <button type="submit" class="btn btn-primary">Save</button>
          </div>
        </form>
      </div>
    `;
    
    document.body.appendChild(modal);
    
    const input = modal.querySelector('#sessionRenameInput');
    const closeBtn = modal.querySelector('.close');
    const cancelBtn = modal.querySelector('.modal-cancel');
    const form = modal.querySelector('#sessionRenameForm');
    
    // Focus and select input
    setTimeout(() => {
      input.focus();
      input.select();
    }, 100);
    
    // Close handlers
    const closeModal = () => {
      modal.remove();
      this.renameSessionId = null;
    };
    
    closeBtn.addEventListener('click', closeModal);
    cancelBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeModal();
    });
    
    // Save handler via form submit
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const newTitle = input.value.trim();
      if (newTitle && newTitle !== currentTitle) {
        await this.renameSession(sessionId, newTitle);
      }
      closeModal();
    });
    
    // ESC key
    const escHandler = (e) => {
      if (e.key === 'Escape') {
        closeModal();
        document.removeEventListener('keydown', escHandler);
      }
    };
    document.addEventListener('keydown', escHandler);
  }

  openDeleteModal(sessionId, sessionTitle) {
    // Close any existing modal
    const existingModal = document.querySelector('#sessionDeleteModal');
    if (existingModal) {
      existingModal.remove();
    }
    
    // Create modal using exact same structure as Rename modal
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.id = 'sessionDeleteModal';
    modal.style.display = 'block';
    modal.innerHTML = `
      <div class="modal-content">
        <div class="modal-header">
          <h2>Delete Conversation</h2>
          <span class="close">&times;</span>
        </div>
        <div class="delete-modal-body">
          <p>Are you sure you want to delete this conversation?</p>
          <div class="session-title-preview">
            <strong>"${this.escapeHtml(sessionTitle)}"</strong>
          </div>
          <p class="warning-text">⚠️ This action cannot be undone.</p>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary modal-cancel">Cancel</button>
          <button type="button" class="btn btn-danger modal-confirm-delete">Delete</button>
        </div>
      </div>
    `;
    
    document.body.appendChild(modal);
    
    const closeBtn = modal.querySelector('.close');
    const cancelBtn = modal.querySelector('.modal-cancel');
    const deleteBtn = modal.querySelector('.modal-confirm-delete');
    
    // Focus cancel button (safer default)
    setTimeout(() => {
      cancelBtn.focus();
    }, 100);
    
    // Close handlers
    const closeModal = () => {
      modal.remove();
    };
    
    closeBtn.addEventListener('click', closeModal);
    cancelBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeModal();
    });
    
    // Delete handler
    deleteBtn.addEventListener('click', async () => {
      closeModal();
      await this.deleteSession(sessionId);
    });
    
    // ESC key
    const escHandler = (e) => {
      if (e.key === 'Escape') {
        closeModal();
        document.removeEventListener('keydown', escHandler);
      }
    };
    document.addEventListener('keydown', escHandler);
  }

  isRequestActive() {
    // Check if chat module has an active request
    // Access the EventSource from chat_module if available
    if (window.chatModule && window.chatModule.hasActiveRequest) {
      return window.chatModule.hasActiveRequest();
    }
    return false;
  }

  async showSwitchConfirmation(targetSessionId) {
    return new Promise((resolve) => {
      // Close any existing modal
      const existingModal = document.querySelector('#sessionSwitchModal');
      if (existingModal) {
        existingModal.remove();
      }
      
      // Create confirmation modal
      const modal = document.createElement('div');
      modal.className = 'modal';
      modal.id = 'sessionSwitchModal';
      modal.style.display = 'block';
      modal.innerHTML = `
        <div class="modal-content">
          <div class="modal-header">
            <h2>⚠️ Active Request Running</h2>
            <span class="close">&times;</span>
          </div>
          <div class="delete-modal-body">
            <p>A request is currently being processed.</p>
            <p>Switching sessions now will <strong>cancel</strong> the running request.</p>
            <p class="warning-text">Do you want to continue?</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary modal-cancel">Stay Here</button>
            <button type="button" class="btn btn-danger modal-confirm-switch">Switch Session</button>
          </div>
        </div>
      `;
      
      document.body.appendChild(modal);
      
      const closeBtn = modal.querySelector('.close');
      const cancelBtn = modal.querySelector('.modal-cancel');
      const confirmBtn = modal.querySelector('.modal-confirm-switch');
      
      // Close handlers - resolve(false)
      const closeModal = (confirmed) => {
        modal.remove();
        resolve(confirmed);
      };
      
      closeBtn.addEventListener('click', () => closeModal(false));
      cancelBtn.addEventListener('click', () => closeModal(false));
      confirmBtn.addEventListener('click', () => closeModal(true));
      
      // Click outside modal
      modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal(false);
      });
      
      // ESC key
      const escHandler = (e) => {
        if (e.key === 'Escape') {
          closeModal(false);
          document.removeEventListener('keydown', escHandler);
        }
      };
      document.addEventListener('keydown', escHandler);
    });
  }

  async showNewConversationConfirmation() {
    return new Promise((resolve) => {
      // Close any existing modal
      const existingModal = document.querySelector('#sessionNewModal');
      if (existingModal) {
        existingModal.remove();
      }
      
      // Create confirmation modal
      const modal = document.createElement('div');
      modal.className = 'modal';
      modal.id = 'sessionNewModal';
      modal.style.display = 'block';
      modal.innerHTML = `
        <div class="modal-content">
          <div class="modal-header">
            <h2>⚠️ Active Request Running</h2>
            <span class="close">&times;</span>
          </div>
          <div class="delete-modal-body">
            <p>A request is currently being processed.</p>
            <p>Starting a new conversation will <strong>cancel</strong> the running request.</p>
            <p class="warning-text">Do you want to continue?</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary modal-cancel">Stay Here</button>
            <button type="button" class="btn btn-danger modal-confirm-new">New Conversation</button>
          </div>
        </div>
      `;
      
      document.body.appendChild(modal);
      
      const closeBtn = modal.querySelector('.close');
      const cancelBtn = modal.querySelector('.modal-cancel');
      const confirmBtn = modal.querySelector('.modal-confirm-new');
      
      // Close handlers - resolve(false)
      const closeModal = (confirmed) => {
        modal.remove();
        resolve(confirmed);
      };
      
      closeBtn.addEventListener('click', () => closeModal(false));
      cancelBtn.addEventListener('click', () => closeModal(false));
      confirmBtn.addEventListener('click', () => closeModal(true));
      
      // Click outside modal
      modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal(false);
      });
      
      // ESC key
      const escHandler = (e) => {
        if (e.key === 'Escape') {
          closeModal(false);
          document.removeEventListener('keydown', escHandler);
        }
      };
      document.addEventListener('keydown', escHandler);
    });
  }

  setupEventListeners() {
    // Toggle sidebar
    document.getElementById('sessionsToggleBtn')?.addEventListener('click', () => {
      this.toggleSidebar();
    });
    
    document.getElementById('sessionsCloseBtn')?.addEventListener('click', () => {
      this.closeSidebar();
    });
    
    // New conversation
    document.getElementById('sessionsNewBtn')?.addEventListener('click', () => {
      this.newConversation();
    });
    
    // Session info panel close button
    document.getElementById('sessionInfoCloseBtn')?.addEventListener('click', () => {
      this.hideSessionInfoPanel();
    });
    
    // Note: Rename modal event listeners are now created dynamically in openRenameModal()
  }
  
  showSessionInfoPanel(session) {
    const panel = document.getElementById('sessionInfoPanel');
    const content = document.getElementById('sessionInfoContent');
    if (!panel || !content) return;
    
    // Build content HTML
    let html = `
      <div class="session-info-section">
        <div class="session-info-label">Session</div>
        <div class="session-info-value">${this.escapeHtml(session.title)}</div>
      </div>
      <div class="session-info-section">
        <div class="session-info-label">Agent</div>
        <div class="session-info-value">${session.agent_name}</div>
      </div>
    `;
    
    // Show context_vars if present
    const contextVars = session.context_vars || {};
    if (Object.keys(contextVars).length > 0) {
      html += `<div class="session-info-section">
        <div class="session-info-label">Context Variables</div>
        <div class="session-info-vars">`;
      
      // Highlight workflow_phase
      if (contextVars.workflow_phase) {
        const phaseColors = {
          'planning': '#569cd6',
          'characters': '#c586c0',
          'structure': '#4ec9b0',
          'content': '#dcdcaa',
          'review': '#ce9178'
        };
        const color = phaseColors[contextVars.workflow_phase] || '#858585';
        html += `<div class="session-info-var">
          <span class="var-name">workflow_phase:</span>
          <span class="var-value phase-value" style="color:${color}">${contextVars.workflow_phase}</span>
        </div>`;
        
        // Show phase-specific agents info
        html += this.renderPhaseAgentsInfo(contextVars.workflow_phase);
      }
      
      // Show other context vars
      for (const [key, value] of Object.entries(contextVars)) {
        if (key !== 'workflow_phase') {
          html += `<div class="session-info-var">
            <span class="var-name">${key}:</span>
            <span class="var-value">${this.escapeHtml(String(value))}</span>
          </div>`;
        }
      }
      
      html += `</div></div>`;
    }
    
    content.innerHTML = html;
    panel.style.display = 'block';
  }
  
  renderPhaseAgentsInfo(phase) {
    // Define phase -> agents mapping (same as in config)
    const phaseAgents = {
      'planning': ['story_designer', 'story_reviewer'],
      'characters': ['character_designer', 'character_reviewer'],
      'structure': ['structure_builder', 'technical_graph_validator', 'continuity_guardian'],
      'content': ['scene_writer', 'content_quality_reviewer', 'language_quality_reviewer', 'introduction_validator'],
      'review': ['quality_meta_reviewer', 'book_test_agent']
    };
    
    const agents = phaseAgents[phase];
    if (!agents) return '';
    
    return `
      <div class="session-info-var">
        <span class="var-name">Available Agents:</span>
        <div class="phase-agents-list">
          ${agents.map(a => `<span class="phase-agent-tag">${a}</span>`).join('')}
        </div>
      </div>
    `;
  }
  
  hideSessionInfoPanel() {
    const panel = document.getElementById('sessionInfoPanel');
    if (panel) panel.style.display = 'none';
  }

  toggleSidebar() {
    this.sidebarOpen = !this.sidebarOpen;
    const sidebar = document.getElementById('sessionsSidebar');
    
    if (this.sidebarOpen) {
      sidebar?.classList.add('open');
      // Refresh sessions when opening
      if (this.isAuthenticated) {
        this.loadSessions();
      }
    } else {
      sidebar?.classList.remove('open');
    }
  }

  closeSidebar() {
    this.sidebarOpen = false;
    document.getElementById('sessionsSidebar')?.classList.remove('open');
  }

  async loadSessions() {
    const listEl = document.getElementById('sessionsList');
    if (!listEl) {
      return;
    }
    
    listEl.innerHTML = '<div class="sessions-loading">Loading...</div>';
    
    try {
      const response = await fetch('/api/sessions/hierarchy', {
        credentials: 'include'
      });
      
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error('Failed to load sessions');
      }
      
      const data = await response.json();
      this.sessions = data.sessions; // Hierarchical structure
      this.renderSessions();
    } catch (error) {
      console.error('Error loading sessions:', error);
      listEl.innerHTML = `<div class="sessions-error">Failed to load conversations</div>`;
    }
  }

  renderSessions() {
    const listEl = document.getElementById('sessionsList');
    if (!listEl) return;
    
    if (this.sessions.length === 0) {
      listEl.innerHTML = '<div class="sessions-empty">No conversations yet<br>Start chatting to create one!</div>';
      return;
    }
    
    // A full re-render throws away the DOM of lazily loaded subtrees, so the
    // lookup map is rebuilt from the roots rather than accumulating stale nodes.
    this.sessionById = new Map();
    this.rememberSessions(this.sessions);
    listEl.innerHTML = this.sessions.map(session => this.renderSessionHierarchy(session, 0)).join('');
    this.attachSessionListeners(listEl);
  }

  // Sessions are loaded lazily (roots up front, children on expand), so the
  // rendered tree is no longer a complete in-memory structure. Keep a flat
  // id -> session map of everything loaded so lookups stay O(1) and also work
  // for nodes that arrived via a child fetch.
  rememberSessions(sessions) {
    if (!this.sessionById) this.sessionById = new Map();
    for (const s of sessions || []) {
      if (s && s.session_id) this.sessionById.set(s.session_id, s);
    }
  }

  // Wire up a freshly rendered subtree. Called for the whole list on initial
  // render and for each lazily loaded children container.
  attachSessionListeners(rootEl) {
    rootEl.querySelectorAll('.session-item').forEach(item => {
      if (item.dataset.wired === '1') return; // don't double-bind on re-entry
      item.dataset.wired = '1';
      const sessionId = item.dataset.sessionId;

      item.addEventListener('click', async (e) => {
        // Don't trigger if clicking on action buttons or toggle
        if (e.target.closest('.session-item-btn') || e.target.closest('.session-toggle-btn')) return;

        // Stop propagation to prevent parent session items from also firing
        e.stopPropagation();

        await this.loadSession(sessionId);
      });
    });

    rootEl.querySelectorAll('.session-toggle-btn').forEach(btn => {
      if (btn.dataset.wired === '1') return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleSessionChildren(btn);
      });
    });

    rootEl.querySelectorAll('.session-rename-btn').forEach(btn => {
      if (btn.dataset.wired === '1') return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.openRenameModal(btn.dataset.sessionId, btn.dataset.sessionTitle);
      });
    });

    rootEl.querySelectorAll('.session-delete-btn').forEach(btn => {
      if (btn.dataset.wired === '1') return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sessionTitle = btn.dataset.sessionTitle || 'Untitled';
        this.openDeleteModal(btn.dataset.sessionId, sessionTitle);
      });
    });

    // Info buttons - show session info panel
    rootEl.querySelectorAll('.session-info-btn').forEach(btn => {
      if (btn.dataset.wired === '1') return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sessionId = btn.dataset.sessionId;
        const session = this.findSessionInHierarchy(sessionId);
        if (session) {
          this.showSessionInfoPanel(session);
        }
      });
    });
  }

  async toggleSessionChildren(toggleBtn) {
    const sessionItem = toggleBtn.closest('.session-item');
    // ':scope >' is required: a nested descendant also has .session-children,
    // and a plain querySelector would grab the grandchild's container.
    const childrenContainer = sessionItem.querySelector(':scope > .session-children');
    if (!childrenContainer) return;
    const isExpanded = sessionItem.classList.contains('expanded');

    if (isExpanded) {
      sessionItem.classList.remove('expanded');
      childrenContainer.style.display = 'none';
      toggleBtn.innerHTML = `
        <svg viewBox="0 0 12 12" fill="currentColor">
          <path d="M4 3L8 6L4 9Z"/>
        </svg>
      `;
      return;
    }

    // Expanding: the sidebar only ships root sessions, so a node's children are
    // fetched the first time it is opened (one sub-index read server-side).
    if (childrenContainer.dataset.loaded !== '1') {
      const sessionId = sessionItem.dataset.sessionId;
      const childDepth = parseInt(sessionItem.dataset.depth || '0', 10) + 1;
      childrenContainer.innerHTML = '<div class="sessions-loading">Loading...</div>';
      childrenContainer.style.display = 'block';
      try {
        const resp = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}/children`,
          { credentials: 'include' }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const children = data.sessions || [];
        this.rememberSessions(children);
        childrenContainer.innerHTML =
          children.map(c => this.renderSessionHierarchy(c, childDepth)).join('');
        childrenContainer.dataset.loaded = '1';
        this.attachSessionListeners(childrenContainer);
      } catch (err) {
        console.error('Failed to load sub-sessions:', err);
        childrenContainer.innerHTML =
          '<div class="sessions-error">Failed to load sub-sessions</div>';
      }
    }

    sessionItem.classList.add('expanded');
    childrenContainer.style.display = 'block';
    toggleBtn.innerHTML = `
      <svg viewBox="0 0 12 12" fill="currentColor">
        <path d="M3 4L6 8L9 4Z"/>
      </svg>
    `;
  }
  
  renderSessionHierarchy(session, depth = 0) {
    // has_children comes from the server (the parent's sub-index exists);
    // children[] is only filled for subtrees that were already fetched.
    const loadedChildren = !!(session.children && session.children.length > 0);
    const hasChildren = !!session.has_children || loadedChildren;
    const isActive = session.session_id === this.currentSessionId;
    const isSubAgent = depth > 0; // Sub-agents are at depth > 0
    const date = new Date(session.updated_at);
    const dateStr = this.formatDate(date);
    const indent = depth * 20; // pixels
    
    let html = `
      <div class="session-item ${isActive ? 'active' : ''} ${hasChildren ? 'has-children' : ''} ${isSubAgent ? 'sub-agent-session' : ''}" 
           data-session-id="${session.session_id}" 
           data-depth="${depth}"
           style="padding-left: ${indent + 12}px;">
        <div class="session-item-header">
          ${hasChildren ? `
            <button class="session-toggle-btn" title="Toggle sub-sessions">
              <svg viewBox="0 0 12 12" fill="currentColor">
                <path d="M4 3L8 6L4 9Z"/>
              </svg>
            </button>
          ` : '<span class="session-toggle-spacer"></span>'}
          <div class="session-item-title" title="${this.escapeHtml(session.title)}">
            ${isSubAgent ? '<span class="sub-agent-indicator" title="Sub-agent session (may be read-only)">🔹</span>' : ''}
            ${this.escapeHtml(session.title)}
          </div>
          <div class="session-item-actions">
            ${session.context_vars && Object.keys(session.context_vars).length > 0 ? `
              <button class="session-item-btn session-info-btn" 
                      data-session-id="${session.session_id}"
                      title="Session Info">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor">
                  <circle cx="8" cy="8" r="6" stroke-width="1.5"/>
                  <path d="M8 5v1M8 8v4" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              </button>
            ` : ''}
            <button class="session-item-btn session-rename-btn" 
                    data-session-id="${session.session_id}"
                    data-session-title="${this.escapeHtml(session.title)}"
                    title="Rename">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor">
                <path d="M11 2L14 5L5 14H2V11L11 2Z" stroke-width="1.5"/>
              </svg>
            </button>
            <button class="session-item-btn session-delete-btn" 
                    data-session-id="${session.session_id}"
                    data-session-title="${this.escapeHtml(session.title)}"
                    title="Delete">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor">
                <path d="M3 4h10M5 4V3h6v1M6 7v5M10 7v5M4 4l1 10h6l1-10" stroke-width="1.5"/>
              </svg>
            </button>
          </div>
        </div>
        <div class="session-item-meta">
          <span class="session-item-agent">${session.agent_name}</span>
          ${this.renderContextVarsBadges(session.context_vars)}
          <span class="session-item-date">${dateStr}</span>
          <span class="session-item-count">${session.message_count} msgs</span>
        </div>
        ${hasChildren ? `
          <div class="session-children" style="display: none;"${loadedChildren ? ' data-loaded="1"' : ''}>
            ${loadedChildren ? session.children.map(child => this.renderSessionHierarchy(child, depth + 1)).join('') : ''}
          </div>
        ` : ''}
      </div>
    `;
    
    return html;
  }
  
  renderContextVarsBadges(contextVars) {
    if (!contextVars || Object.keys(contextVars).length === 0) {
      return '';
    }
    
    let badges = '';
    
    // Show workflow_phase as special badge
    if (contextVars.workflow_phase) {
      const phaseColors = {
        'planning': '#569cd6',    // blue
        'characters': '#c586c0',  // purple
        'structure': '#4ec9b0',   // teal
        'content': '#dcdcaa',     // yellow
        'review': '#ce9178'       // orange
      };
      const color = phaseColors[contextVars.workflow_phase] || '#858585';
      badges += `<span class="session-phase-badge" style="background:${color}20;color:${color};border:1px solid ${color}40;" title="Workflow Phase">${contextVars.workflow_phase}</span>`;
    }
    
    // Show book_id if present
    if (contextVars.book_id) {
      badges += `<span class="session-context-badge" title="Book ID: ${contextVars.book_id}">📚${contextVars.book_id}</span>`;
    }
    
    return badges;
  }

  formatDate(date) {
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);
    
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    
    return date.toLocaleDateString();
  }

  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  async newConversation() {
    // Check if a request is currently active
    if (this.isRequestActive()) {
      // Show confirmation dialog
      const confirmed = await this.showNewConversationConfirmation();
      if (!confirmed) {
        return; // User cancelled
      }
    }
    
    // Clear current session
    this.currentSessionId = null;
    sessionStorage.removeItem('lastSessionId');
    
    // Notify Session Info panel of session change
    if (window.AgentSystem.SessionInfo) {
      window.AgentSystem.SessionInfo.onSessionChange(null);
    }
    
    // Clear chat UI
    const chatEl = document.getElementById('chat');
    if (chatEl) {
      chatEl.innerHTML = '';
    }
    
    // Update header
    this.updateSessionDisplay();
    
    // Close sidebar
    this.closeSidebar();
    
    // Dispatch event for other modules
    window.dispatchEvent(new CustomEvent('session:new'));
  }

  async loadSession(sessionId, force = false) {
    // Check if a request is currently active
    if (!force && this.isRequestActive()) {
      // Show confirmation dialog
      const confirmed = await this.showSwitchConfirmation(sessionId);
      if (!confirmed) {
        return; // User cancelled
      }
    }
    
    try {
      const response = await fetch(`/api/sessions/${sessionId}`, {
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error('Failed to load session');
      }
      
      const session = await response.json();
      this.currentSessionId = sessionId;
      
      // Notify Session Info panel of session change
      if (window.AgentSystem.SessionInfo) {
        window.AgentSystem.SessionInfo.onSessionChange(sessionId);
      }
      
      // Save to sessionStorage for page reload restoration (tab-specific)
      sessionStorage.setItem('lastSessionId', sessionId);
      
      // Update UI - use session.title or session.name
      const sessionName = session.title || session.name;
      this.updateSessionDisplay(sessionId, sessionName);
      this.renderSessions(); // Re-render to update active state
      
      // Check if this is a sub-agent session with unavailable agent
      const isSubAgent = session.depth && session.depth > 0;
      const agentSelect = document.getElementById('agentSelector');
      const agentAvailable = agentSelect && Array.from(agentSelect.options).some(opt => opt.value === session.agent_name);
      const isReadOnly = isSubAgent && !agentAvailable;
      
      // Dispatch event for chat module to restore messages
      window.dispatchEvent(new CustomEvent('session:loaded', {
        detail: { 
          session,
          readOnly: isReadOnly,
          reason: isReadOnly ? `Sub-agent "${session.agent_name}" is not available in the agent selector` : null
        }
      }));
      
      this.closeSidebar();
    } catch (error) {
      console.error('Failed to load session:', error);
      alert('Failed to load conversation');
    }
  }

  async renameSession(sessionId, newTitle) {
    try {
      const response = await fetch(`/api/sessions/${sessionId}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json'
        },
        credentials: 'include',
        body: JSON.stringify({ title: newTitle })
      });
      
      if (!response.ok) {
        throw new Error('Failed to rename session');
      }
      
      // Reload sessions to reflect the change
      await this.loadSessions();
    } catch (error) {
      console.error('Failed to rename session:', error);
      alert('Failed to rename conversation');
    }
  }

  async deleteSession(sessionId) {
    try {
      const response = await fetch(`/api/sessions/${sessionId}`, {
        method: 'DELETE',
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error('Failed to delete session');
      }
      
      // If deleted current session, clear it
      if (sessionId === this.currentSessionId) {
        this.newConversation();
      }
      
      // Reload sessions
      await this.loadSessions();
    } catch (error) {
      console.error('Failed to delete session:', error);
      alert('Failed to delete conversation');
    }
  }

  setCurrentSession(sessionId) {
    this.currentSessionId = sessionId;
    this.updateSessionDisplay(sessionId);
    
    // Persist to sessionStorage for page refresh (tab-specific)
    if (sessionId) {
      sessionStorage.setItem('lastSessionId', sessionId);
    } else {
      sessionStorage.removeItem('lastSessionId');
    }
    
    // Update active state in list
    if (this.sidebarOpen) {
      this.renderSessions();
    }
  }

  updateSessionDisplay(sessionId = null, sessionName = null) {
    const headerEl = document.getElementById('headerSessionId');
    if (headerEl) {
      headerEl.textContent = sessionId ? sessionId.substring(0, 8) : '--';
    }
    
    // Update browser tab title
    if (sessionName) {
      document.title = `${sessionName} - Agent System`;
    } else if (sessionId) {
      document.title = `${sessionId.substring(0, 8)} - Agent System`;
    } else {
      document.title = 'Agent System (MCP)';
    }
  }

  // Called by chat module when a session is created/updated
  onSessionUpdated(sessionId) {
    if (sessionId !== this.currentSessionId) {
      this.setCurrentSession(sessionId);
    }
    
    // Refresh session list if sidebar is open
    if (this.sidebarOpen && this.isAuthenticated) {
      this.loadSessions();
    }
  }
  
  getCurrentSessionId() {
    return this.currentSessionId;
  }
}

// Initialize on load
let sessionManager;
document.addEventListener('DOMContentLoaded', () => {
  sessionManager = new SessionManager();
  
  // Make available globally for other modules
  window.sessionManager = sessionManager;
});

export default sessionManager;
