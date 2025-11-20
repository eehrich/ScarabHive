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
    this.sessions = [];
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
    
    // Restore last session from localStorage
    // Delay slightly to ensure chat_module event listeners are registered
    setTimeout(async () => {
      const lastSessionId = localStorage.getItem('lastSessionId');
      if (lastSessionId && this.findSessionInHierarchy(lastSessionId)) {
        // Load the session messages into the chat
        await this.loadSession(lastSessionId);
      }
    }, 100);
    
    // Set up event listeners
    this.setupEventListeners();
  }
  
  findSessionInHierarchy(sessionId) {
    // Recursively search for session in hierarchical structure
    const search = (sessions) => {
      for (const session of sessions) {
        if (session.session_id === sessionId) {
          return session;
        }
        if (session.children && session.children.length > 0) {
          const found = search(session.children);
          if (found) return found;
        }
      }
      return null;
    };
    return search(this.sessions);
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
    
    // Note: Rename modal event listeners are now created dynamically in openRenameModal()
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
    
    listEl.innerHTML = this.sessions.map(session => this.renderSessionHierarchy(session, 0)).join('');
    
    // Attach event listeners to session items
    listEl.querySelectorAll('.session-item').forEach(item => {
      const sessionId = item.dataset.sessionId;
      
      item.addEventListener('click', (e) => {
        // Don't trigger if clicking on action buttons or toggle
        if (e.target.closest('.session-item-btn') || e.target.closest('.session-toggle-btn')) return;
        
        // Stop propagation to prevent parent session items from also firing
        e.stopPropagation();
        
        this.loadSession(sessionId);
      });
    });
    
    // Attach event listeners to toggle buttons
    listEl.querySelectorAll('.session-toggle-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleSessionChildren(btn);
      });
    });
    
    // Attach event listeners to action buttons
    listEl.querySelectorAll('.session-rename-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.openRenameModal(btn.dataset.sessionId, btn.dataset.sessionTitle);
      });
    });
    
    listEl.querySelectorAll('.session-delete-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sessionTitle = btn.dataset.sessionTitle || 'Untitled';
        this.openDeleteModal(btn.dataset.sessionId, sessionTitle);
      });
    });
  }
  
  toggleSessionChildren(toggleBtn) {
    const sessionItem = toggleBtn.closest('.session-item');
    const childrenContainer = sessionItem.querySelector('.session-children');
    const isExpanded = sessionItem.classList.contains('expanded');
    
    if (isExpanded) {
      sessionItem.classList.remove('expanded');
      childrenContainer.style.display = 'none';
      toggleBtn.innerHTML = `
        <svg viewBox="0 0 12 12" fill="currentColor">
          <path d="M4 3L8 6L4 9Z"/>
        </svg>
      `;
    } else {
      sessionItem.classList.add('expanded');
      childrenContainer.style.display = 'block';
      toggleBtn.innerHTML = `
        <svg viewBox="0 0 12 12" fill="currentColor">
          <path d="M3 4L6 8L9 4Z"/>
        </svg>
      `;
    }
  }
  
  renderSessionHierarchy(session, depth = 0) {
    const hasChildren = session.children && session.children.length > 0;
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
          <span class="session-item-date">${dateStr}</span>
          <span class="session-item-count">${session.message_count} msgs</span>
        </div>
        ${hasChildren ? `
          <div class="session-children" style="display: none;">
            ${session.children.map(child => this.renderSessionHierarchy(child, depth + 1)).join('')}
          </div>
        ` : ''}
      </div>
    `;
    
    return html;
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

  newConversation() {
    // Clear current session
    this.currentSessionId = null;
    localStorage.removeItem('lastSessionId');
    
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

  async loadSession(sessionId) {
    try {
      const response = await fetch(`/api/sessions/${sessionId}`, {
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error('Failed to load session');
      }
      
      const session = await response.json();
      this.currentSessionId = sessionId;
      
      // Save to localStorage for page reload restoration
      localStorage.setItem('lastSessionId', sessionId);
      
      // Update UI
      this.updateSessionDisplay(sessionId);
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
    
    // Persist to localStorage for page refresh
    if (sessionId) {
      localStorage.setItem('lastSessionId', sessionId);
    } else {
      localStorage.removeItem('lastSessionId');
    }
    
    // Update active state in list
    if (this.sidebarOpen) {
      this.renderSessions();
    }
  }

  updateSessionDisplay(sessionId = null) {
    const headerEl = document.getElementById('headerSessionId');
    if (headerEl) {
      headerEl.textContent = sessionId ? sessionId.substring(0, 8) : '--';
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
