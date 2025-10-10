/**
 * User Management Dashboard JavaScript
 * Handles user creation, editing, deletion, and search functionality
 */

// Helper function to get JWT token from cookie
function getAuthToken() {
    const cookies = document.cookie.split(';');
    for (let cookie of cookies) {
        const [name, value] = cookie.trim().split('=');
        if (name === 'access_token') {
            return value;
        }
    }
    return null;
}

// Modal functions
function openCreateUserModal() {
    document.getElementById('createUserModal').style.display = 'block';
}

function closeCreateUserModal() {
    document.getElementById('createUserModal').style.display = 'none';
    document.getElementById('createUserForm').reset();
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('createUserModal');
    if (event.target === modal) {
        closeCreateUserModal();
    }
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
        const token = getAuthToken();
        if (!token) {
            alert('Authentication required. Please log in again.');
            window.top.location.href = '/';
            return;
        }
        
        const response = await fetch('/admin/users', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(userData)
        });
        
        if (response.ok) {
            const result = await response.json();
            alert(`User "${result.username}" created successfully!`);
            closeCreateUserModal();
            location.reload();
        } else {
            const error = await response.json();
            alert('Error creating user: ' + (error.detail || 'Unknown error'));
        }
    } catch (error) {
        alert('Network error: ' + error.message);
    }
}

// Search functionality
document.addEventListener('DOMContentLoaded', function() {
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

// Update active count
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

// Toggle user active status
async function toggleUserStatus(userId, isCurrentlyActive) {
    if (!confirm(`${isCurrentlyActive ? 'Deactivate' : 'Activate'} this user?`)) {
        return;
    }
    
    try {
        const response = await fetch(`/plugins/user_management/users/${userId}/toggle-active`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (response.ok) {
            const result = await response.json();
            alert(result.message);
            location.reload();
        } else {
            const error = await response.json();
            alert('Error: ' + (error.detail || 'Failed to toggle user status'));
        }
    } catch (error) {
        alert('Network error: ' + error.message);
    }
}

// Edit user
function editUser(userId) {
    alert('Edit user functionality - integrate with /admin/users/' + userId + ' endpoint');
    // TODO: Implement edit modal or redirect to edit page
}

// Delete user
async function deleteUser(userId, username) {
    if (!confirm(`Delete user "${username}"? This action cannot be undone.`)) {
        return;
    }
    
    try {
        const response = await fetch(`/plugins/user_management/users/${userId}`, {
            method: 'DELETE'
        });
        
        if (response.ok) {
            const result = await response.json();
            alert(result.message);
            location.reload();
        } else {
            const error = await response.json();
            alert('Error: ' + (error.detail || 'Failed to delete user'));
        }
    } catch (error) {
        alert('Network error: ' + error.message);
    }
}
