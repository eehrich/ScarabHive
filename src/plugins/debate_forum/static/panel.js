/* Debate Forum - Panel JavaScript */
(function () {
    "use strict";

    // ── State ─────────────────────────────────────────────────
    let channels = [];
    let selectedChannelId = null;
    let currentChannel = null;
    let pollTimer = null;

    const POLL_INTERVAL = 4000; // ms
    const ROLE_COLORS = {
        advocate: "advocate",
        critic: "critic",
        moderator: "moderator",
        challenger: "challenger",
        observer: "observer",
    };

    // ── DOM refs ──────────────────────────────────────────────
    const $channelList = document.getElementById("channel-list");
    const $chatPlaceholder = document.getElementById("chat-placeholder");
    const $chatHeader = document.getElementById("chat-header");
    const $chatMessages = document.getElementById("chat-messages");
    const $chatChannelName = document.getElementById("chat-channel-name");
    const $chatChannelTopic = document.getElementById("chat-channel-topic");
    const $chatMsgCount = document.getElementById("chat-msg-count");
    const $verdictBox = document.getElementById("verdict-box");
    const $verdictContent = document.getElementById("verdict-content");
    const $filterStatus = document.getElementById("filter-status");
    const $filterSearch = document.getElementById("filter-search");
    const $btnRefresh = document.getElementById("btn-refresh");
    const $btnArchive = document.getElementById("btn-archive");

    // Stats
    const $statActive = document.querySelector("#stat-active span");
    const $statConcluded = document.querySelector("#stat-concluded span");
    const $statArchived = document.querySelector("#stat-archived span");
    const $statMessages = document.querySelector("#stat-messages span");

    // ── API helpers ───────────────────────────────────────────
    function apiUrl(path) {
        // Endpoints are relative to the plugin's mount point
        return "api/" + path;
    }

    async function apiFetch(path) {
        const resp = await fetch(apiUrl(path));
        if (!resp.ok) throw new Error(`API ${path}: ${resp.status}`);
        return resp.json();
    }

    async function apiPost(path) {
        const resp = await fetch(apiUrl(path), { method: "POST" });
        if (!resp.ok) throw new Error(`API POST ${path}: ${resp.status}`);
        return resp.json();
    }

    // ── Stats ─────────────────────────────────────────────────
    async function loadStats() {
        try {
            const data = await apiFetch("stats");
            $statActive.textContent = data.active || 0;
            $statConcluded.textContent = data.concluded || 0;
            $statArchived.textContent = data.archived || 0;
            $statMessages.textContent = data.total_messages || 0;
        } catch (e) {
            console.warn("Failed to load stats:", e);
        }
    }

    // ── Channels ──────────────────────────────────────────────
    async function loadChannels() {
        try {
            const params = new URLSearchParams();
            const status = $filterStatus.value;
            const search = $filterSearch.value.trim();
            if (status) params.set("status", status);
            if (search) params.set("search", search);

            const qs = params.toString();
            const data = await apiFetch("channels" + (qs ? "?" + qs : ""));
            channels = data.channels || [];
            renderChannelList();
        } catch (e) {
            console.warn("Failed to load channels:", e);
        }
    }

    function statusIcon(status) {
        switch (status) {
            case "active": return "🟢";
            case "concluded": return "✅";
            case "archived": return "📦";
            default: return "⚪";
        }
    }

    function renderChannelList() {
        if (channels.length === 0) {
            $channelList.innerHTML = '<div class="empty-state">No channels yet</div>';
            return;
        }

        $channelList.innerHTML = channels
            .map((ch) => {
                const isActive = ch.id === selectedChannelId;
                const badge = ch.message_count
                    ? `<span class="channel-msg-badge">${ch.message_count}</span>`
                    : "";
                return `
                <div class="channel-item${isActive ? " active" : ""}" data-id="${ch.id}">
                    <span class="channel-status-icon">${statusIcon(ch.status)}</span>
                    <span class="channel-name" title="${escapeHtml(ch.topic)}">${escapeHtml(ch.name)}</span>
                    ${badge}
                </div>`;
            })
            .join("");

        // Attach click handlers
        $channelList.querySelectorAll(".channel-item").forEach((el) => {
            el.addEventListener("click", () => selectChannel(parseInt(el.dataset.id, 10)));
        });
    }

    // ── Channel selection & messages ──────────────────────────
    async function selectChannel(channelId) {
        selectedChannelId = channelId;
        renderChannelList(); // highlight active
        $chatPlaceholder.style.display = "none";
        $chatHeader.style.display = "flex";
        $chatMessages.style.display = "block";

        try {
            const [chData, msgData] = await Promise.all([
                apiFetch("channels/" + channelId),
                apiFetch("channels/" + channelId + "/messages"),
            ]);

            currentChannel = chData;
            $chatChannelName.textContent = chData.name;
            $chatChannelTopic.textContent = chData.topic;
            $chatMsgCount.textContent = (msgData.count || 0) + " messages";

            renderMessages(msgData.messages || []);
            renderVerdict(chData);

            // Show/hide archive button based on status
            $btnArchive.style.display =
                chData.status === "archived" ? "none" : "inline-block";
        } catch (e) {
            console.warn("Failed to load channel:", e);
            $chatMessages.innerHTML =
                '<div class="empty-state">Failed to load channel</div>';
        }
    }

    function roleClass(role) {
        const normalized = (role || "").toLowerCase().trim();
        return ROLE_COLORS[normalized] || "default";
    }

    function getInitials(name) {
        return (name || "?")
            .split(/[\s_-]+/)
            .map((w) => w.charAt(0).toUpperCase())
            .slice(0, 2)
            .join("");
    }

    function renderMessages(messages) {
        if (!messages.length) {
            $chatMessages.innerHTML =
                '<div class="empty-state">No messages yet — debate has not started</div>';
            return;
        }

        let html = "";
        let lastRound = -1;

        for (const msg of messages) {
            // Round divider
            if (msg.round !== lastRound) {
                html += `<div class="round-divider">Round ${msg.round}</div>`;
                lastRound = msg.round;
            }

            const rc = roleClass(msg.agent_role);
            html += `
            <div class="message-card">
                <div class="msg-avatar avatar-${rc}">${getInitials(msg.agent_name)}</div>
                <div class="msg-body">
                    <div class="msg-header">
                        <span class="msg-agent-name">${escapeHtml(msg.agent_name)}</span>
                        <span class="msg-role-badge role-${rc}">${escapeHtml(msg.agent_role)}</span>
                        <span class="msg-timestamp">${formatTimestamp(msg.created_at)}</span>
                    </div>
                    <div class="msg-content">${escapeHtml(msg.content)}</div>
                </div>
            </div>`;
        }

        $chatMessages.innerHTML = html;
        // Scroll to bottom
        $chatMessages.scrollTop = $chatMessages.scrollHeight;
    }

    function renderVerdict(channel) {
        if (channel.status === "concluded" && (channel.verdict_summary || channel.verdict_json)) {
            $verdictBox.style.display = "block";

            // Extract summary: prefer verdict_summary, fall back to verdict_json.summary
            const vj = channel.verdict_json || {};
            const summary = channel.verdict_summary
                || (typeof vj === "object" ? vj.summary : null)
                || "";

            // Build HTML
            let html = "";
            if (summary) {
                html += `<p class="verdict-summary">${escapeHtml(summary)}</p>`;
            }

            // Show key verdict fields (if verdict_json is an object with useful keys)
            if (typeof vj === "object" && Object.keys(vj).length > 0) {
                const interestingKeys = Object.keys(vj).filter(
                    k => k !== "summary" && k !== "remaining_differences"
                );
                if (interestingKeys.length > 0) {
                    html += `<details class="verdict-details"><summary>Details</summary><ul>`;
                    for (const key of interestingKeys) {
                        const val = vj[key];
                        const label = key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
                        if (Array.isArray(val)) {
                            html += `<li><strong>${escapeHtml(label)}:</strong><ul>`;
                            for (const item of val) {
                                html += `<li>${escapeHtml(String(item))}</li>`;
                            }
                            html += `</ul></li>`;
                        } else {
                            html += `<li><strong>${escapeHtml(label)}:</strong> ${escapeHtml(String(val))}</li>`;
                        }
                    }
                    html += `</ul></details>`;
                }
            }

            // Fallback: no summary at all, show raw JSON
            if (!html) {
                html = `<pre>${escapeHtml(JSON.stringify(vj, null, 2))}</pre>`;
            }

            $verdictContent.innerHTML = html;
        } else {
            $verdictBox.style.display = "none";
        }
    }

    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // ── Archive action ────────────────────────────────────────
    async function archiveChannel() {
        if (!selectedChannelId || !currentChannel) return;
        if (currentChannel.status === "archived") return;

        try {
            await apiPost("channels/" + selectedChannelId + "/archive");
            await refresh();
        } catch (e) {
            console.warn("Failed to archive:", e);
        }
    }

    // ── Polling / Refresh ─────────────────────────────────────
    async function refresh() {
        await Promise.all([loadStats(), loadChannels()]);
        if (selectedChannelId) {
            await selectChannel(selectedChannelId);
        }
    }

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(async () => {
            await loadStats();
            await loadChannels();
            // Refresh messages if a channel is selected and active
            if (selectedChannelId && currentChannel && currentChannel.status === "active") {
                try {
                    const msgData = await apiFetch("channels/" + selectedChannelId + "/messages");
                    const currentCount = parseInt($chatMsgCount.textContent, 10) || 0;
                    if ((msgData.count || 0) !== currentCount) {
                        renderMessages(msgData.messages || []);
                        $chatMsgCount.textContent = (msgData.count || 0) + " messages";
                    }
                } catch (e) { /* ignore */ }
            }
        }, POLL_INTERVAL);
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    // ── Utilities ─────────────────────────────────────────────
    function escapeHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    function formatTimestamp(ts) {
        if (!ts) return "";
        try {
            const d = new Date(ts + (ts.includes("Z") || ts.includes("+") ? "" : "Z"));
            return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        } catch {
            return ts;
        }
    }

    // ── Event bindings ────────────────────────────────────────
    $btnRefresh.addEventListener("click", refresh);
    $btnArchive.addEventListener("click", archiveChannel);
    $filterStatus.addEventListener("change", loadChannels);

    let searchTimeout;
    $filterSearch.addEventListener("input", () => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(loadChannels, 300);
    });

    // ── Init ──────────────────────────────────────────────────
    refresh().then(() => startPolling());
})();
