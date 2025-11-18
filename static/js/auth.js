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
        // Load tokens from storage
        this.token = localStorage.getItem(this.tokenKey) || sessionStorage.getItem(this.tokenKey);
        this.refreshToken = localStorage.getItem(this.refreshTokenKey) || sessionStorage.getItem(this.refreshTokenKey);

        if (this.token) {
            // Verify token is still valid
            this.verifyToken().catch(() => {
                // Try to refresh if we have a refresh token
                if (this.refreshToken) {
                    this.performRefresh().catch(() => this.clearAuth());
                } else {
                    this.clearAuth();
                }
            });

            // Start auto-refresh (refresh every 25 minutes if access token is 30 min)
            this.startAutoRefresh();

            // Perform immediate refresh if token might be close to expiry
            this.checkAndRefreshIfNeeded();
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
                this.refreshToken = data.refresh_token;

                // Store tokens
                const storage = rememberMe ? localStorage : sessionStorage;
                storage.setItem(this.tokenKey, this.token);
                if (this.refreshToken) {
                    storage.setItem(this.refreshTokenKey, this.refreshToken);
                }
                storage.setItem(this.usernameKey, username);

                // Fetch user info
                await this.verifyToken();

                // Start auto-refresh
                this.startAutoRefresh();

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
        // Stop auto-refresh
        this.stopAutoRefresh();

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
        this.refreshToken = null;
        this.user = null;
        localStorage.removeItem(this.tokenKey);
        localStorage.removeItem(this.refreshTokenKey);
        localStorage.removeItem(this.usernameKey);
        sessionStorage.removeItem(this.tokenKey);
        sessionStorage.removeItem(this.refreshTokenKey);
        sessionStorage.removeItem(this.usernameKey);
    }

    async performRefresh() {
        if (!this.refreshToken) {
            throw new Error('No refresh token available');
        }

        try {
            const response = await fetch('/auth/refresh', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ refresh_token: this.refreshToken })
            });

            if (response.ok) {
                const data = await response.json();
                this.token = data.access_token;

                // Update refresh token if new one provided (token rotation)
                if (data.refresh_token) {
                    this.refreshToken = data.refresh_token;
                }

                // Update storage
                const hasLocalStorage = localStorage.getItem(this.tokenKey);
                const storage = hasLocalStorage ? localStorage : sessionStorage;
                storage.setItem(this.tokenKey, this.token);
                if (data.refresh_token) {
                    storage.setItem(this.refreshTokenKey, this.refreshToken);
                }

                console.log('Token refreshed successfully');
                return true;
            } else {
                console.error('Token refresh failed');
                this.clearAuth();
                return false;
            }
        } catch (error) {
            console.error('Token refresh error:', error);
            this.clearAuth();
            return false;
        }
    }

    startAutoRefresh() {
        // Stop any existing interval
        this.stopAutoRefresh();

        // Refresh token every 25 minutes (5 min before expiry if token is 30 min)
        const refreshInterval = 25 * 60 * 1000; // 25 minutes in ms

        this.refreshInterval = setInterval(() => {
            if (this.refreshToken) {
                this.performRefresh().catch(error => {
                    console.error('Auto-refresh failed:', error);
                    this.clearAuth();
                    this.redirectToLogin();
                });
            }
        }, refreshInterval);

        console.log('Auto-refresh started (every 25 minutes)');
    }

    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
            console.log('Auto-refresh stopped');
        }
    }

    async checkAndRefreshIfNeeded() {
        // Try to decode token to check expiry (simple check without validation)
        if (!this.token || !this.refreshToken) return;

        try {
            const parts = this.token.split('.');
            if (parts.length !== 3) return;

            const payload = JSON.parse(atob(parts[1]));
            const exp = payload.exp;
            const now = Math.floor(Date.now() / 1000);

            // If token expires in less than 5 minutes, refresh immediately
            const timeUntilExpiry = exp - now;
            if (timeUntilExpiry < 300) { // 5 minutes
                console.log('Token close to expiry, refreshing immediately...');
                await this.performRefresh();
            }
        } catch (error) {
            // If we can't decode, ignore and let normal flow handle it
            console.debug('Could not check token expiry:', error);
        }
    }

    // Helper to add auth header to fetch requests with automatic token refresh on 401
    async authFetch(url, options = {}) {
        if (this.token) {
            options.headers = options.headers || {};
            options.headers['Authorization'] = `Bearer ${this.token}`;
        }

        let response = await fetch(url, options);

        // If we get 401 and have a refresh token, try to refresh and retry
        if (response.status === 401 && this.refreshToken && !options._isRetry) {
            console.log('Received 401, attempting token refresh...');
            const refreshed = await this.performRefresh();

            if (refreshed) {
                // Update auth header with new token and retry request
                options.headers['Authorization'] = `Bearer ${this.token}`;
                options._isRetry = true; // Prevent infinite retry loop
                response = await fetch(url, options);
            }
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
        return this.user && this.user.role === 'ADMIN';
    }
}

// Global auth manager instance
const authManager = new AuthManager();
window.authManager = authManager;

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = AuthManager;
}

