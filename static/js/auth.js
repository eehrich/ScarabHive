/**
 * Authentication Module
 * Handles JWT token management, login/logout, and user session
 */

class AuthManager {
    constructor() {
        this.token = null;
        this.refreshToken = null;
        this.user = null;
        this.tokenKey = 'auth_token';
        this.refreshTokenKey = 'refresh_token';
        this.usernameKey = 'auth_username';
        this.refreshInterval = null;
        this.init();
    }

    init() {
        // Check if we have a valid cookie-based session
        // We don't read tokens from storage anymore - only use HttpOnly cookies
        // Just verify if we're authenticated by checking /auth/me
        this.verifyToken().then(authenticated => {
            if (authenticated) {
                console.log('Authenticated via cookie');
            }
        }).catch(() => {
            // Not authenticated, that's fine
            console.log('Not authenticated');
        });
    }

    async verifyToken() {
        try {
            // Cookie is sent automatically by browser
            const response = await fetch('/auth/me', {
                credentials: 'include'  // Important: include cookies
            });

            if (response.ok) {
                this.user = await response.json();
                this.token = 'cookie-based'; // Marker that we're authenticated
                return true;
            } else {
                this.user = null;
                this.token = null;
                return false;
            }
        } catch (error) {
            console.error('Token verification failed:', error);
            return false;
        }
    }

    isAuthenticated() {
        return !!this.token && !!this.user;
    }

    getToken() {
        // For compatibility - return a marker since we use cookies
        return this.token || null;
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
                credentials: 'include',  // Important: include cookies
                body: JSON.stringify({ username, password })
            });

            if (response.ok) {
                const data = await response.json();
                // Cookie is set by server automatically
                // We don't store tokens in localStorage anymore
                
                // Store username for display (optional)
                const storage = rememberMe ? localStorage : sessionStorage;
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
            // Call logout endpoint (server will delete cookie)
            await fetch('/auth/logout', {
                method: 'POST',
                credentials: 'include'  // Important: include cookies
            });
        } catch (error) {
            console.error('Logout error:', error);
        } finally {
            this.clearAuth();
        }
    }

    clearAuth() {
        this.token = null;
        this.refreshToken = null;
        this.user = null;
        // Only clear username from storage
        localStorage.removeItem(this.usernameKey);
        sessionStorage.removeItem(this.usernameKey);
        // Tokens are in HttpOnly cookies - browser will handle them
    }

    // Helper to add credentials to fetch requests
    async authFetch(url, options = {}) {
        // Always include cookies
        options.credentials = 'include';
        
        // No need to add Authorization header - cookie is sent automatically
        const response = await fetch(url, options);

        // If we get 401, user needs to re-login
        if (response.status === 401) {
            console.log('Received 401, redirecting to login...');
            this.clearAuth();
            this.redirectToLogin();
        }

        return response;
    }

    // Redirect to login page
    redirectToLogin(returnUrl = null) {
        const currentUrl = returnUrl || window.location.pathname + window.location.search;
        window.location.href = `/login?return=${encodeURIComponent(currentUrl)}`;
    }

    // Check if user has admin role
    isAdmin() {
        // role is returned as lowercase "admin" from the API (see UserRole enum)
        return this.user && (this.user.role === 'admin' || this.user.role === 'ADMIN');
    }
}

// Global auth manager instance
const authManager = new AuthManager();
window.authManager = authManager;

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = AuthManager;
}

