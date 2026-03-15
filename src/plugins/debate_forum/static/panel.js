/* Debate Forum - Panel JavaScript */
(function () {
    "use strict";

    // ── State ─────────────────────────────────────────────────
    let channels = [];
    let selectedChannelId = null;
    let currentChannel = null;
    let pollTimer = null;

    const POLL_INTERVAL = 4000; // ms
    const ROLE_PALETTE_SIZE = 7; // number of color slots (slot0–slot6)

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

    // Participants sidebar
    const $participantsSidebar = document.getElementById("participants-sidebar");
    const $participantsList = document.getElementById("participants-list");

    // Chat input
    const $chatInputBar = document.getElementById("chat-input-bar");
    const $chatInputName = document.getElementById("chat-input-name");
    const $chatInputRole = document.getElementById("chat-input-role");
    const $chatInputMsg = document.getElementById("chat-input-msg");
    const $btnSend = document.getElementById("btn-send");

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

    async function apiPostJson(path, body) {
        const resp = await fetch(apiUrl(path), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `API POST ${path}: ${resp.status}`);
        }
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
            updateChatInputVisibility();

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
        if (normalized === "moderator") return "moderator";
        // Stable color slot derived from role name — works for any role string
        let hash = 0;
        for (let i = 0; i < normalized.length; i++) {
            hash = (hash * 31 + normalized.charCodeAt(i)) & 0xffff;
        }
        return "slot" + (hash % ROLE_PALETTE_SIZE);
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
            renderParticipants([]);
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
            const isPinned = msg.pinned ? true : false;
            const pinIcon = isPinned ? "📌" : "📍";
            const pinnedClass = isPinned ? " pinned" : "";
            html += `
            <div class="message-card${pinnedClass}" data-msg-id="${msg.id}">
                <div class="msg-avatar avatar-${rc}">${getInitials(msg.agent_name)}</div>
                <div class="msg-body">
                    <div class="msg-header">
                        <span class="msg-agent-name">${escapeHtml(msg.agent_name)}</span>
                        <span class="msg-role-badge role-${rc}">${escapeHtml(msg.agent_role)}</span>
                        <span class="msg-timestamp">${formatTimestamp(msg.created_at)}</span>
                        <button class="btn-pin" title="${isPinned ? 'Unpin' : 'Pin'}" data-msg-id="${msg.id}" data-pinned="${isPinned ? '1' : '0'}">${pinIcon}</button>
                    </div>
                    <div class="msg-content">${escapeHtml(msg.content)}</div>
                </div>
            </div>`;
        }

        $chatMessages.innerHTML = html;
        // Scroll to bottom
        $chatMessages.scrollTop = $chatMessages.scrollHeight;

        // Update participants sidebar
        renderParticipants(messages);
    }

    function renderParticipants(messages) {
        if (!messages.length) {
            $participantsSidebar.style.display = "none";
            return;
        }

        // Extract unique participants with message counts
        const participants = new Map();
        for (const msg of messages) {
            const key = msg.agent_name;
            if (!participants.has(key)) {
                participants.set(key, {
                    name: msg.agent_name,
                    role: msg.agent_role,
                    count: 0,
                });
            }
            participants.get(key).count++;
        }

        $participantsSidebar.style.display = "flex";
        $participantsList.innerHTML = Array.from(participants.values())
            .map((p) => {
                const rc = roleClass(p.role);
                return `
                <div class="participant-item">
                    <div class="participant-avatar avatar-${rc}">${getInitials(p.name)}</div>
                    <div class="participant-info">
                        <span class="participant-name">${escapeHtml(p.name)}</span>
                        <span class="participant-role">${escapeHtml(p.role)}</span>
                    </div>
                    <span class="participant-msg-count">${p.count}</span>
                </div>`;
            })
            .join("");
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

    // ── Send message from chat input ──────────────────────────
    async function sendMessage() {
        if (!selectedChannelId || !currentChannel || currentChannel.status !== "active") return;

        const name = $chatInputName.value.trim();
        const role = $chatInputRole.value.trim() || "user";
        const content = $chatInputMsg.value.trim();
        if (!name || !content) return;

        // Determine round: use latest round from current messages
        let round = 0;
        const roundDividers = $chatMessages.querySelectorAll(".round-divider");
        if (roundDividers.length > 0) {
            const lastDiv = roundDividers[roundDividers.length - 1];
            const match = lastDiv.textContent.match(/(\d+)/);
            if (match) round = parseInt(match[1], 10);
        }

        $btnSend.disabled = true;
        try {
            await apiPostJson("channels/" + selectedChannelId + "/messages", {
                agent_name: name,
                agent_role: role,
                content: content,
                round: round,
            });
            $chatInputMsg.value = "";
            // Refresh messages immediately
            const msgData = await apiFetch("channels/" + selectedChannelId + "/messages");
            renderMessages(msgData.messages || []);
            $chatMsgCount.textContent = (msgData.count || 0) + " messages";
        } catch (e) {
            console.warn("Failed to send message:", e);
            alert("Failed to send: " + e.message);
        } finally {
            $btnSend.disabled = false;
            $chatInputMsg.focus();
        }
    }

    function updateChatInputVisibility() {
        if (currentChannel && currentChannel.status === "active") {
            $chatInputBar.style.display = "flex";
        } else {
            $chatInputBar.style.display = "none";
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

    $btnSend.addEventListener("click", sendMessage);
    $chatInputMsg.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Pin toggle via event delegation
    $chatMessages.addEventListener("click", async (e) => {
        const btn = e.target.closest(".btn-pin");
        if (!btn) return;
        const msgId = btn.getAttribute("data-msg-id");
        const currentlyPinned = btn.getAttribute("data-pinned") === "1";
        btn.disabled = true;
        try {
            await apiPostJson("messages/" + msgId + "/pin", { pinned: !currentlyPinned });
            // Refresh messages to show updated pin state
            const msgData = await apiFetch("channels/" + selectedChannelId + "/messages");
            renderMessages(msgData.messages || []);
        } catch (err) {
            console.warn("Failed to toggle pin:", err);
        }
    });

    let searchTimeout;
    $filterSearch.addEventListener("input", () => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(loadChannels, 300);
    });

    // ── Init ──────────────────────────────────────────────────
    refresh().then(() => startPolling());
})();
