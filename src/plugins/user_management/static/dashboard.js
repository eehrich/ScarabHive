/**
 * User Management Dashboard JavaScript
 * Handles user creation, editing, deletion, and search functionality
 */

// Note: Authentication is handled via cookies automatically
// fetch() with credentials: 'include' sends cookies with every request

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

// Show toast notification
function showToast(message, duration = 3000) {
    const toast = document.getElementById('toast');
    const toastMessage = document.getElementById('toastMessage');
    toastMessage.textContent = message;
    toast.style.display = 'block';
    setTimeout(() => {
        toast.style.display = 'none';
    }, duration);
}

// Show confirm dialog modal
function showConfirm(title, message, onConfirm) {
    const modal = document.getElementById('confirmModal');
    const titleEl = document.getElementById('confirmTitle');
    const messageEl = document.getElementById('confirmMessage');
    const confirmBtn = document.getElementById('confirmBtn');
    
    titleEl.textContent = title;
    messageEl.textContent = message;
    modal.style.display = 'block';
    
    // Remove old event listeners
    const newConfirmBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);
    
    // Add new event listener
    newConfirmBtn.onclick = () => {
        closeConfirmModal();
        onConfirm();
    };
}

function closeConfirmModal() {
    document.getElementById('confirmModal').style.display = 'none';
}

// =============================================================================
// CREATE USER MODAL
// =============================================================================

function openCreateUserModal() {
    document.getElementById('createUserModal').style.display = 'block';
}

function closeCreateUserModal() {
    document.getElementById('createUserModal').style.display = 'none';
    document.getElementById('createUserForm').reset();
}

// Create user function
async function createUser(event) {
    event.preventDefault();
    
    const form = event.target;
    const formData = new FormData(form);
    
    // Build user data object
    const userData = {
        username: formData.get('username'),
        email: formData.get('email'),
        password: formData.get('password'),
        full_name: formData.get('full_name') || null,
        role: formData.get('role'),
        is_active: formData.get('is_active') === 'on'
    };
    
    try {
        const response = await fetch('/plugins/user_management/users', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',  // Send cookie automatically
            body: JSON.stringify(userData)
        });
        
        if (response.ok) {
            const result = await response.json();
            showToast(`User "${result.username}" created successfully!`);
            closeCreateUserModal();
            setTimeout(() => location.reload(), 1000);
        } else {
            if (response.status === 401) {
                showToast('Authentication required. Please log in again.');
                setTimeout(() => window.top.location.href = '/', 1500);
                return;
            }
            const error = await response.json();
            showToast('Error: ' + (error.detail || 'Unknown error'));
        }
    } catch (error) {
        showToast('Network error: ' + error.message);
    }
}

// =============================================================================
// EDIT USER MODAL
// =============================================================================

function openEditUserModal() {
    document.getElementById('editUserModal').style.display = 'block';
}

function closeEditUserModal() {
    document.getElementById('editUserModal').style.display = 'none';
    document.getElementById('editUserForm').reset();
}

// Edit user - opens modal with current data
async function editUser(userId) {
    try {
        // Fetch current user data
        const response = await fetch(`/plugins/user_management/users/${userId}`, {
            method: 'GET',
            credentials: 'include'
        });
        
        if (!response.ok) {
            if (response.status === 401) {
                showToast('Authentication required. Please log in again.');
                setTimeout(() => window.top.location.href = '/', 1500);
                return;
            }
            throw new Error('Failed to fetch user data');
        }
        
        const user = await response.json();
        
        // Populate form
        document.getElementById('edit-user-id').value = userId;
        document.getElementById('edit-username').value = user.username;
        document.getElementById('edit-email').value = user.email;
        document.getElementById('edit-full-name').value = user.full_name || '';
        document.getElementById('edit-role').value = user.role;
        document.getElementById('edit-is-active').checked = user.is_active;
        
        // Open modal
        openEditUserModal();
    } catch (error) {
        showToast('Error: ' + error.message);
    }
}

// Submit edit user form
async function submitEditUser(event) {
    event.preventDefault();
    
    const userId = document.getElementById('edit-user-id').value;
    const form = event.target;
    const formData = new FormData(form);
    
    // Build update object (username is read-only, don't send it)
    const updateData = {
        email: formData.get('email'),
        full_name: formData.get('full_name') || null,
        role: formData.get('role'),
        is_active: formData.get('is_active') === 'on'
    };
    
    try {
        const response = await fetch(`/plugins/user_management/users/${userId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify(updateData)
        });
        
        if (response.ok) {
            const result = await response.json();
            showToast(result.message || 'User updated successfully!');
            closeEditUserModal();
            setTimeout(() => location.reload(), 1000);
        } else {
            if (response.status === 401) {
                showToast('Authentication required. Please log in again.');
                setTimeout(() => window.top.location.href = '/', 1500);
                return;
            }
            const error = await response.json();
            showToast('Error: ' + (error.detail || 'Unknown error'));
        }
    } catch (error) {
        showToast('Error: ' + error.message);
    }
}

// =============================================================================
// TOGGLE USER STATUS
// =============================================================================

async function toggleUserStatus(userId, isCurrentlyActive) {
    showConfirm(
        'Toggle User Status',
        `${isCurrentlyActive ? 'Deactivate' : 'Activate'} this user?`,
        async () => {
            try {
                const response = await fetch(`/plugins/user_management/users/${userId}/toggle-active`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    credentials: 'include'
                });
                
                if (response.ok) {
                    const result = await response.json();
                    showToast(result.message);
                    setTimeout(() => location.reload(), 1000);
                } else {
                    if (response.status === 401) {
                        showToast('Authentication required. Please log in again.');
                        setTimeout(() => window.top.location.href = '/', 1500);
                        return;
                    }
                    const error = await response.json();
                    showToast('Error: ' + (error.detail || 'Failed to toggle user status'));
                }
            } catch (error) {
                showToast('Network error: ' + error.message);
            }
        }
    );
}

// =============================================================================
// DELETE USER
// =============================================================================

async function deleteUser(userId, username) {
    showConfirm(
        'Delete User',
        `Delete user "${username}"? This action cannot be undone.`,
        async () => {
            try {
                const response = await fetch(`/plugins/user_management/users/${userId}`, {
                    method: 'DELETE',
                    credentials: 'include'
                });
                
                if (response.ok) {
                    const result = await response.json();
                    showToast(result.message);
                    setTimeout(() => location.reload(), 1000);
                } else {
                    if (response.status === 401) {
                        showToast('Authentication required. Please log in again.');
                        setTimeout(() => window.top.location.href = '/', 1500);
                        return;
                    }
                    const error = await response.json();
                    showToast('Error: ' + (error.detail || 'Failed to delete user'));
                }
            } catch (error) {
                showToast('Network error: ' + error.message);
            }
        }
    );
}

// =============================================================================
// SEARCH & INITIALIZATION
// =============================================================================

// Close modals when clicking outside
window.onclick = function(event) {
    const createModal = document.getElementById('createUserModal');
    const editModal = document.getElementById('editUserModal');
    const confirmModal = document.getElementById('confirmModal');
    
    if (event.target === createModal) {
        closeCreateUserModal();
    } else if (event.target === editModal) {
        closeEditUserModal();
    } else if (event.target === confirmModal) {
        closeConfirmModal();
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Search functionality
    const searchInput = document.getElementById('search-input');
    const table = document.getElementById('users-table');
    
    if (searchInput && table) {
        searchInput.addEventListener('input', function() {
            const searchTerm = this.value.toLowerCase();
            const rows = table.querySelectorAll('tbody tr');
            
            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(searchTerm) ? '' : 'none';
            });
        });
    }
    
    // Update active count
    updateActiveCount();
});

// Update active user count
function updateActiveCount() {
    const rows = document.querySelectorAll('tbody tr');
    let activeCount = 0;
    rows.forEach(row => {
        const statusCell = row.querySelector('.status-active');
        if (statusCell) activeCount++;
    });
    const activeCountEl = document.getElementById('active-count');
    if (activeCountEl) {
        activeCountEl.textContent = activeCount;
    }
}
