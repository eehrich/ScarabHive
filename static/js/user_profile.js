/**
 * User Profile and Settings Management
 * 
 * Provides modal dialogs for user profile and settings
 */

window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.UserProfile = {
  /**
   * Show user profile modal
   */
  showProfile: async function() {
    try {
      // Check if authManager is available
      if (!window.authManager) {
        console.error('AuthManager not available');
        alert('Authentication system not ready');
        return;
      }

      // Check authentication
      if (!window.authManager.isAuthenticated()) {
        alert('Please login first');
        window.location.href = '/login';
        return;
      }

      const response = await window.authManager.authFetch('/auth/me');

      if (!response.ok) {
        if (response.status === 401) {
          alert('Session expired. Please login again.');
          window.location.href = '/login';
          return;
        }
        throw new Error('Failed to fetch user profile');
      }

      const user = await response.json();

      // Create modal
      const modal = this.createModal('User Profile', `
        <div class="user-profile-content">
          <div class="profile-section">
            <div class="profile-avatar">
              <span style="font-size: 48px;">👤</span>
            </div>
            <h3>${user.username}</h3>
          </div>
          
          <div class="profile-info">
            <div class="info-row">
              <label>Username:</label>
              <span>${user.username}</span>
            </div>
            <div class="info-row">
              <label>Email:</label>
              <span>${user.email || 'Not set'}</span>
            </div>
            <div class="info-row">
              <label>Full Name:</label>
              <span>${user.full_name || 'Not set'}</span>
            </div>
            <div class="info-row">
              <label>Role:</label>
              <span class="role-badge role-${user.role.toLowerCase()}">${user.role}</span>
            </div>
            <div class="info-row">
              <label>Status:</label>
              <span class="status-badge ${user.is_active ? 'active' : 'inactive'}">
                ${user.is_active ? 'Active' : 'Inactive'}
              </span>
            </div>
            <div class="info-row">
              <label>Member Since:</label>
              <span>${new Date(user.created_at).toLocaleDateString()}</span>
            </div>
          </div>
        </div>
      `);

      document.body.appendChild(modal);

    } catch (error) {
      console.error('Failed to show profile:', error);
      alert('Failed to load user profile');
    }
  },

  /**
   * Show settings modal
   */
  showSettings: async function() {
    try {
      // Check if authManager is available
      if (!window.authManager) {
        console.error('AuthManager not available');
        alert('Authentication system not ready');
        return;
      }

      // Check authentication
      if (!window.authManager.isAuthenticated()) {
        alert('Please login first');
        window.location.href = '/login';
        return;
      }

      const response = await window.authManager.authFetch('/auth/me');

      if (!response.ok) {
        if (response.status === 401) {
          alert('Session expired. Please login again.');
          window.location.href = '/login';
          return;
        }
        throw new Error('Failed to fetch user data');
      }

      const user = await response.json();

      // Create modal with settings form
      const modal = this.createModal('User Settings', `
        <form id="settingsForm" class="settings-form">
          <div class="settings-section">
            <h4>Account Information</h4>
            
            <div class="form-group">
              <label for="setting-email">Email</label>
              <input type="email" id="setting-email" name="email" value="${user.email || ''}" 
                     placeholder="Enter your email">
            </div>
            
            <div class="form-group">
              <label for="setting-fullname">Full Name</label>
              <input type="text" id="setting-fullname" name="full_name" value="${user.full_name || ''}" 
                     placeholder="Enter your full name">
            </div>
          </div>

          <div class="settings-section">
            <h4>Change Password</h4>
            
            <div class="form-group">
              <label for="setting-current-password">Current Password</label>
              <input type="password" id="setting-current-password" name="current_password" 
                     placeholder="Enter current password">
            </div>
            
            <div class="form-group">
              <label for="setting-new-password">New Password</label>
              <input type="password" id="setting-new-password" name="new_password" 
                     placeholder="Enter new password">
            </div>
            
            <div class="form-group">
              <label for="setting-confirm-password">Confirm Password</label>
              <input type="password" id="setting-confirm-password" name="confirm_password" 
                     placeholder="Confirm new password">
            </div>
          </div>

          <div class="modal-actions">
            <button type="submit" class="btn btn-primary">Save Changes</button>
            <button type="button" class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">
              Cancel
            </button>
          </div>
        </form>
      `);

      document.body.appendChild(modal);

      // Handle form submission
      const form = modal.querySelector('#settingsForm');
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        await this.saveSettings(form, modal);
      });

    } catch (error) {
      console.error('Failed to show settings:', error);
      alert('Failed to load settings');
    }
  },

  /**
   * Save settings
   */
  saveSettings: async function(form, modal) {
    try {
      const formData = new FormData(form);
      const data = {};
      
      // Only include non-empty fields
      for (let [key, value] of formData.entries()) {
        if (value.trim()) {
          data[key] = value.trim();
        }
      }

      // Validate password change if attempted
      if (data.new_password) {
        if (!data.current_password) {
          alert('Please enter your current password');
          return;
        }
        if (data.new_password !== data.confirm_password) {
          alert('New passwords do not match');
          return;
        }
        if (data.new_password.length < 8) {
          alert('Password must be at least 8 characters');
          return;
        }
      }

      // Update user profile
      const updateData = {};
      if (data.email) updateData.email = data.email;
      if (data.full_name) updateData.full_name = data.full_name;
      if (data.new_password) {
        updateData.password = data.new_password;
        updateData.current_password = data.current_password;
      }

      const response = await window.authManager.authFetch('/auth/me', {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(updateData)
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to update settings');
      }

      alert('Settings updated successfully');
      modal.remove();

    } catch (error) {
      console.error('Failed to save settings:', error);
      alert(error.message || 'Failed to save settings');
    }
  },

  /**
   * Create modal dialog
   */
  createModal: function(title, content) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal-dialog user-modal">
        <div class="modal-header">
          <h3>${title}</h3>
          <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">×</button>
        </div>
        <div class="modal-body">
          ${content}
        </div>
      </div>
    `;

    // Prevent wheel events from scrolling parent page
    overlay.addEventListener('wheel', (e) => {
      // Find if there's a scrollable element under cursor
      let target = e.target;
      while (target && target !== overlay) {
        const style = window.getComputedStyle(target);
        const overflowY = style.overflowY;
        const isScrollable = (overflowY === 'auto' || overflowY === 'scroll') && target.scrollHeight > target.clientHeight;
        
        if (isScrollable) {
          const atTop = target.scrollTop <= 0;
          const atBottom = target.scrollHeight - target.scrollTop <= target.clientHeight + 1;
          
          if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atBottom)) {
            return; // Allow scroll within element
          }
          break;
        }
        target = target.parentElement;
      }
      // Prevent parent page scroll
      e.preventDefault();
    }, { passive: false });

    // Close on overlay click
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) {
        overlay.remove();
      }
    });

    // Close on Escape key
    const escHandler = (e) => {
      if (e.key === 'Escape') {
        overlay.remove();
        document.removeEventListener('keydown', escHandler);
      }
    };
    document.addEventListener('keydown', escHandler);

    return overlay;
  }
};
