/* Debate Forum - Panel JavaScript */
(function () {
    "use strict";

    // ── State ─────────────────────────────────────────────────
    let channels = [];
    let selectedChannelId = null;
    let currentChannel = null;
    let pollTimer = null;

    const POLL_INTERVAL = 4000; // ms
    const ROLE_PALETTE_SIZE = 8; // number of color slots (slot0–slot7)

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
    const $filterGroup = document.getElementById("filter-group");
    const $filterStatus = document.getElementById("filter-status");
    const $filterSearch = document.getElementById("filter-search");
    const $btnRefresh = document.getElementById("btn-refresh");
    const $btnArchive = document.getElementById("btn-archive");
    const $btnReopen = document.getElementById("btn-reopen");
    const $btnDelete = document.getElementById("btn-delete");
    const $btnCopy = document.getElementById("btn-copy");

    // Delete modal
    const $modalDelete = document.getElementById("modal-delete-channel");
    const $btnDeleteCancel = document.getElementById("btn-delete-cancel");
    const $btnDeleteConfirm = document.getElementById("btn-delete-confirm");

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

    async function apiFetch(path, options = {}) {
        const resp = await fetch(apiUrl(path), options);
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

    // ── Groups ────────────────────────────────────────────────
    async function loadGroups() {
        try {
            const data = await apiFetch("groups");
            const groups = data.groups || [];
            // Preserve current selection
            const current = $filterGroup.value;
            $filterGroup.innerHTML = '<option value="">All Groups</option>';
            for (const g of groups) {
                const opt = document.createElement("option");
                opt.value = g.id;
                opt.textContent = `${g.name} (${g.channel_count || 0})`;
                $filterGroup.appendChild(opt);
            }
            if (current) $filterGroup.value = current;
        } catch (e) {
            console.warn("Failed to load groups:", e);
        }
    }

    // ── Channels ──────────────────────────────────────────────
    async function loadChannels() {
        try {
            const params = new URLSearchParams();
            const status = $filterStatus.value;
            const search = $filterSearch.value.trim();
            const groupId = $filterGroup.value;
            if (status) params.set("status", status);
            if (search) params.set("search", search);
            if (groupId) params.set("group_id", groupId);

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

    function getGroupName(groupId) {
        if (!groupId) return null;
        const opt = $filterGroup.querySelector(`option[value="${groupId}"]`);
        return opt ? opt.textContent.replace(/\s*\(\d+\)$/, "") : `Group ${groupId}`;
    }

    // ── Collapsed-groups state (persisted in localStorage) ──
    const COLLAPSED_KEY = "debate_forum_collapsed_groups";
    function loadCollapsed() {
        try {
            const raw = localStorage.getItem(COLLAPSED_KEY);
            return raw ? new Set(JSON.parse(raw)) : null; // null = first visit
        } catch { return null; }
    }
    function saveCollapsed(set) {
        try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set])); } catch {}
    }
    let collapsedGroups = loadCollapsed(); // null until first render (default all collapsed)

    const MAX_VISIBLE_GROUPS = 20; // hide oldest groups when more than this

    function renderChannelList() {
        if (channels.length === 0) {
            $channelList.innerHTML = '<div class="empty-state">No channels yet</div>';
            return;
        }

        const filteringByGroup = !!$filterGroup.value;

        // When filtering by one group, render flat list (no headers)
        if (filteringByGroup) {
            let html = "";
            for (const ch of channels) {
                const isActive = ch.id === selectedChannelId;
                const badge = ch.message_count
                    ? `<span class="channel-msg-badge">${ch.message_count}</span>`
                    : "";
                html += `
                <div class="channel-item${isActive ? " active" : ""}" data-id="${ch.id}">
                    <span class="channel-status-icon">${statusIcon(ch.status)}</span>
                    <span class="channel-name" title="${escapeHtml(ch.topic)}">${escapeHtml(ch.name)}</span>
                    ${badge}
                </div>`;
            }
            $channelList.innerHTML = html;
            $channelList.querySelectorAll(".channel-item").forEach((el) => {
                el.addEventListener("click", () => selectChannel(parseInt(el.dataset.id, 10)));
            });
            return;
        }

        // Bucket channels by group
        const buckets = new Map(); // group_id → [channels]
        for (const ch of channels) {
            const gid = ch.group_id || 0;
            if (!buckets.has(gid)) buckets.set(gid, []);
            buckets.get(gid).push(ch);
        }
        // Sort channels within each group: oldest first (chronological)
        for (const arr of buckets.values()) {
            arr.sort((a, b) => (a.id || 0) - (b.id || 0));
        }
        // Sort groups: newest first (by highest channel id in group)
        const sortedGroups = [...buckets.entries()].sort((a, b) => {
            const maxA = Math.max(...a[1].map(c => c.id || 0));
            const maxB = Math.max(...b[1].map(c => c.id || 0));
            return maxB - maxA;
        });

        // First render: default all collapsed
        if (collapsedGroups === null) {
            collapsedGroups = new Set(sortedGroups.map(([gid]) => String(gid)));
            saveCollapsed(collapsedGroups);
        }

        // Limit visible groups — hide oldest entirely
        const visibleGroups = sortedGroups.slice(0, MAX_VISIBLE_GROUPS);
        const hiddenCount = sortedGroups.length - visibleGroups.length;

        let html = "";
        for (const [gid, arr] of visibleGroups) {
            const gidStr = String(gid);
            const collapsed = collapsedGroups.has(gidStr);
            const groupLabel = gid ? `${escapeHtml(getGroupName(gid))} (#${escapeHtml(String(gid))})` : "Ungrouped";
            const chevron = collapsed ? "▸" : "▾";
            const chCount = arr.length;
            html += `<div class="channel-group-header" data-gid="${gidStr}" title="${groupLabel}">`
                + `<span class="group-chevron">${chevron}</span> `
                + `<span class="group-label">${groupLabel}</span>`
                + `<span class="group-count">${chCount}</span>`
                + `</div>`;

            if (!collapsed) {
                for (const ch of arr) {
                    const isActive = ch.id === selectedChannelId;
                    const badge = ch.message_count
                        ? `<span class="channel-msg-badge">${ch.message_count}</span>`
                        : "";
                    html += `
                    <div class="channel-item${isActive ? " active" : ""}" data-id="${ch.id}">
                        <span class="channel-status-icon">${statusIcon(ch.status)}</span>
                        <span class="channel-name" title="${escapeHtml(ch.topic)}">${escapeHtml(ch.name)}</span>
                        ${badge}
                    </div>`;
                }
            }
        }

        if (hiddenCount > 0) {
            html += `<div class="channel-group-overflow">${hiddenCount} older group${hiddenCount > 1 ? "s" : ""} hidden — use group filter</div>`;
        }

        $channelList.innerHTML = html;

        // Click handlers: channels
        $channelList.querySelectorAll(".channel-item").forEach((el) => {
            el.addEventListener("click", () => selectChannel(parseInt(el.dataset.id, 10)));
        });
        // Click handlers: group headers toggle collapse
        $channelList.querySelectorAll(".channel-group-header").forEach((el) => {
            el.addEventListener("click", () => {
                const gid = el.getAttribute("data-gid");
                if (collapsedGroups.has(gid)) {
                    collapsedGroups.delete(gid);
                } else {
                    collapsedGroups.add(gid);
                }
                saveCollapsed(collapsedGroups);
                renderChannelList();
            });
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
            $chatChannelName.textContent = chData.name + "  #" + chData.id;
            $chatChannelTopic.textContent = chData.topic;
            $chatMsgCount.textContent = (msgData.count || 0) + " messages";

            renderMessages(msgData.messages || []);
            renderVerdict(chData);
            updateChatInputVisibility();

            // Show/hide archive and reopen buttons based on status
            $btnArchive.style.display =
                chData.status === "archived" ? "none" : "inline-block";
            $btnReopen.style.display =
                (chData.status === "concluded" || chData.status === "archived") ? "inline-block" : "none";
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

    function renderVerdictValue(key, val) {
        const label = key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
        if (val === null || val === undefined) {
            return `<li><strong>${escapeHtml(label)}:</strong> <em>—</em></li>`;
        }
        if (Array.isArray(val)) {
            let h = `<li><strong>${escapeHtml(label)}:</strong><ul>`;
            for (const item of val) {
                if (typeof item === "object" && item !== null) {
                    h += `<li>${escapeHtml(JSON.stringify(item, null, 2))}</li>`;
                } else {
                    h += `<li>${escapeHtml(String(item))}</li>`;
                }
            }
            return h + `</ul></li>`;
        }
        if (typeof val === "object") {
            let h = `<li><strong>${escapeHtml(label)}:</strong><ul>`;
            for (const [k, v] of Object.entries(val)) {
                h += renderVerdictValue(k, v);
            }
            return h + `</ul></li>`;
        }
        return `<li><strong>${escapeHtml(label)}:</strong> ${escapeHtml(String(val))}</li>`;
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
                        html += renderVerdictValue(key, vj[key]);
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

    // ── Copy channel text to clipboard ──────────────────────
    async function copyChannelText() {
        if (!selectedChannelId) return;
        try {
            const msgData = await apiFetch("channels/" + selectedChannelId + "/messages");
            const messages = msgData.messages || [];
            if (!messages.length) return;

            let text = "";
            if (currentChannel) {
                text += `# ${currentChannel.name}\n`;
                if (currentChannel.topic) text += `Topic: ${currentChannel.topic}\n`;
                text += "\n";
            }

            let lastRound = -1;
            for (const msg of messages) {
                if (msg.round !== lastRound) {
                    text += `--- Round ${msg.round} ---\n\n`;
                    lastRound = msg.round;
                }
                const role = (msg.agent_role || "").toUpperCase();
                text += `[${role} "${msg.agent_name}"]\n${msg.content}\n\n`;
            }

            if (currentChannel && currentChannel.verdict_summary) {
                text += `--- Verdict ---\n${currentChannel.verdict_summary}\n`;
            }

            await navigator.clipboard.writeText(text);
            $btnCopy.textContent = "✅";
            setTimeout(() => { $btnCopy.textContent = "📋"; }, 1500);
        } catch (e) {
            console.warn("Failed to copy:", e);
        }
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

    // ── Reopen action ─────────────────────────────────────────
    async function reopenChannel() {
        if (!selectedChannelId || !currentChannel) return;
        if (currentChannel.status === "active") return;

        try {
            await apiPost("channels/" + selectedChannelId + "/reopen");
            await refresh();
        } catch (e) {
            console.warn("Failed to reopen:", e);
        }
    }
    // ── Delete action ───────────────────────────────────────────────────
    function openDeleteModal() {
        if (!selectedChannelId) return;
        $modalDelete.classList.add("visible");
    }

    function closeDeleteModal() {
        $modalDelete.classList.remove("visible");
    }

    async function deleteChannel() {
        if (!selectedChannelId) return;
        closeDeleteModal();
        try {
            await apiFetch("channels/" + selectedChannelId, { method: "DELETE" });
            selectedChannelId = null;
            currentChannel = null;
            $chatHeader.style.display = "none";
            $chatMessages.style.display = "none";
            $chatPlaceholder.style.display = "flex";
            await loadChannels();
        } catch (e) {
            console.warn("Failed to delete:", e);
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
        await Promise.all([loadStats(), loadGroups()]);
        await loadChannels(); // must run AFTER loadGroups so getGroupName() works
        if (selectedChannelId) {
            await selectChannel(selectedChannelId);
        }
    }

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(async () => {
            await Promise.all([loadStats(), loadGroups()]);
            await loadChannels(); // must run AFTER loadGroups so getGroupName() works
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

    // ── Create Channel Modal ─────────────────────────────────
    const $modalCreate = document.getElementById("modal-create-channel");
    const $createName = document.getElementById("create-ch-name");
    const $createTopic = document.getElementById("create-ch-topic");
    const $createContext = document.getElementById("create-ch-context");
    const $btnCreateChannel = document.getElementById("btn-create-channel");
    const $btnCreateCancel = document.getElementById("btn-create-cancel");
    const $btnCreateConfirm = document.getElementById("btn-create-confirm");

    function openCreateModal() {
        $createName.value = "";
        $createTopic.value = "";
        $createContext.value = "";
        $modalCreate.classList.add("visible");
        $createName.focus();
    }

    function closeCreateModal() {
        $modalCreate.classList.remove("visible");
    }

    async function createChannel() {
        const name = $createName.value.trim();
        if (!name) { $createName.focus(); return; }
        try {
            const result = await apiPostJson("channels", {
                name: name,
                topic: $createTopic.value.trim(),
                context: $createContext.value.trim(),
            });
            closeCreateModal();
            await refresh();
            if (result.channel_id) {
                await selectChannel(result.channel_id);
            }
        } catch (e) {
            console.warn("Failed to create channel:", e);
        }
    }

    $btnCreateChannel.addEventListener("click", openCreateModal);
    $btnCreateCancel.addEventListener("click", closeCreateModal);
    $btnCreateConfirm.addEventListener("click", createChannel);
    $modalCreate.addEventListener("click", (e) => {
        if (e.target === $modalCreate) closeCreateModal();
    });
    $createName.addEventListener("keydown", (e) => {
        if (e.key === "Enter") createChannel();
    });

    // ── Event bindings ────────────────────────────────────────
    $btnRefresh.addEventListener("click", refresh);
    $btnArchive.addEventListener("click", archiveChannel);
    $btnReopen.addEventListener("click", reopenChannel);
    $btnDelete.addEventListener("click", openDeleteModal);
    $btnCopy.addEventListener("click", copyChannelText);

    $btnDeleteCancel.addEventListener("click", closeDeleteModal);
    $btnDeleteConfirm.addEventListener("click", deleteChannel);
    $modalDelete.addEventListener("click", (e) => {
        if (e.target === $modalDelete) closeDeleteModal();
    });
    $filterStatus.addEventListener("change", loadChannels);
    $filterGroup.addEventListener("change", loadChannels);

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

    // ── Sidebar Splitter ───────────────────────────────────────
    const $splitter = document.getElementById("sidebar-splitter");
    const $sidebar = document.getElementById("channel-sidebar");
    if ($splitter && $sidebar) {
        let dragging = false;
        $splitter.addEventListener("mousedown", (e) => {
            e.preventDefault();
            dragging = true;
            $splitter.classList.add("dragging");
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
        });
        document.addEventListener("mousemove", (e) => {
            if (!dragging) return;
            const rect = $sidebar.parentElement.getBoundingClientRect();
            const newWidth = Math.min(Math.max(e.clientX - rect.left, 140), 500);
            $sidebar.style.width = newWidth + "px";
        });
        document.addEventListener("mouseup", () => {
            if (!dragging) return;
            dragging = false;
            $splitter.classList.remove("dragging");
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
        });
    }

    // ── Init ──────────────────────────────────────────────────
    loadGroups().then(() => refresh().then(() => startPolling()));
})();
