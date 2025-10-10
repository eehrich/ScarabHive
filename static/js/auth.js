/**
 * Authentication Module
 * Handles JWT token management, login/logout, and user session
 */

class AuthManager {
    constructor() {
        this.token = null;
        this.user = null;
        this.tokenKey = 'auth_token';
        this.usernameKey = 'auth_username';
        this.init();
    }
    
    init() {
        // Load token from storage
        this.token = localStorage.getItem(this.tokenKey) || sessionStorage.getItem(this.tokenKey);
        
        if (this.token) {
            // Verify token is still valid
            this.verifyToken().catch(() => this.clearAuth());
        }
    }
    
    async verifyToken() {
        if (!this.token) return false;
        
        try {
            const response = await fetch('/auth/me', {
                headers: {
                    'Authorization': `Bearer ${this.token}`
                }
            });
            
            if (response.ok) {
                this.user = await response.json();
                return true;
            } else {
                this.clearAuth();
                return false;
            }
        } catch (error) {
            console.error('Token verification failed:', error);
            return false;
        }
    }
    
    isAuthenticated() {
        return !!this.token;
    }
    
    getToken() {
        return this.token;
    }
    
    getUser() {
        return this.user;
    }
    
    getUsername() {
        if (this.user) {
            return this.user.username;
        }
        return localStorage.getItem(this.usernameKey) || 
               sessionStorage.getItem(this.usernameKey) || 
               'Guest';
    }
    
    async login(username, password, rememberMe = false) {
        try {
            const response = await fetch('/auth/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ username, password })
            });
            
            if (response.ok) {
                const data = await response.json();
                this.token = data.access_token;
                
                // Store token
                const storage = rememberMe ? localStorage : sessionStorage;
                storage.setItem(this.tokenKey, this.token);
                storage.setItem(this.usernameKey, username);
                
                // Fetch user info
                await this.verifyToken();
                
                return { success: true, user: this.user };
            } else {
                const error = await response.json();
                return { success: false, error: error.detail || 'Login failed' };
            }
        } catch (error) {
            console.error('Login error:', error);
            return { success: false, error: 'Network error' };
        }
    }
    
    async logout() {
        try {
            // Call logout endpoint (for server-side cleanup if needed)
            if (this.token) {
                await fetch('/auth/logout', {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${this.token}`
                    }
                });
            }
        } catch (error) {
            console.error('Logout error:', error);
        } finally {
            this.clearAuth();
        }
    }
    
    clearAuth() {
        this.token = null;
        this.user = null;
        localStorage.removeItem(this.tokenKey);
        localStorage.removeItem(this.usernameKey);
        sessionStorage.removeItem(this.tokenKey);
        sessionStorage.removeItem(this.usernameKey);
    }
    
    // Helper to add auth header to fetch requests
    authFetch(url, options = {}) {
        if (this.token) {
            options.headers = options.headers || {};
            options.headers['Authorization'] = `Bearer ${this.token}`;
        }
        return fetch(url, options);
    }
    
    // Redirect to login page
    redirectToLogin(returnUrl = null) {
        const currentUrl = returnUrl || window.location.pathname + window.location.search;
        window.location.href = `/login?return=${encodeURIComponent(currentUrl)}`;
    }
    
    // Check if user has admin role
    isAdmin() {
        return this.user && this.user.role === 'ADMIN';
    }
}

// Global auth manager instance
const authManager = new AuthManager();

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = AuthManager;
}
