"""Debug API endpoints for performance profiling.

These endpoints are only available when AGENT_ENABLE_PROFILING=1.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from ..utils.profiling import (
    PROFILING_ENABLED,
    get_profiler,
    get_task_monitor,
    get_loop_monitor,
    get_profiling_report,
    MemoryMonitor,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


def _check_profiling_enabled():
    """Raise 404 if profiling is not enabled."""
    if not PROFILING_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Profiling not enabled. Set AGENT_ENABLE_PROFILING=1 to enable."
        )


@router.get("/profile")
async def get_profile() -> dict[str, Any]:
    """Get comprehensive profiling report.
    
    Returns detailed information about:
    - Active and slow requests
    - Async task states
    - Event loop lag
    - Memory usage
    - Thread counts
    """
    _check_profiling_enabled()
    return get_profiling_report()


@router.get("/profile/requests")
async def get_request_stats() -> dict[str, Any]:
    """Get request statistics by endpoint."""
    _check_profiling_enabled()
    profiler = get_profiler()
    return {
        "active": [
            {
                "id": r.request_id,
                "path": r.path,
                "method": r.method,
                "duration_ms": r.duration_ms
            }
            for r in profiler.get_active_requests()
        ],
        "stats_by_path": profiler.get_stats()
    }


@router.get("/profile/tasks")
async def get_async_tasks() -> dict[str, Any]:
    """Get information about all async tasks.
    
    Useful for identifying:
    - Tasks that are stuck
    - Task names and coroutine info
    - Potential deadlocks
    """
    _check_profiling_enabled()
    task_monitor = get_task_monitor()
    all_tasks = task_monitor.get_all_tasks_info()
    
    return {
        "total_count": len(all_tasks),
        "tasks": all_tasks,
        "slow_callbacks": task_monitor.get_slow_callbacks()
    }


@router.get("/profile/loop")
async def get_event_loop_stats() -> dict[str, Any]:
    """Get event loop health statistics.
    
    High lag values indicate blocking operations in the event loop.
    """
    _check_profiling_enabled()
    loop_monitor = get_loop_monitor()
    
    # Also include current loop info
    loop = asyncio.get_event_loop()
    
    return {
        "lag": loop_monitor.get_lag_stats(),
        "loop_info": {
            "running": loop.is_running(),
            "closed": loop.is_closed(),
            "debug": loop.get_debug() if hasattr(loop, 'get_debug') else None
        }
    }


@router.get("/profile/memory")
async def get_memory_stats() -> dict[str, Any]:
    """Get detailed memory statistics."""
    _check_profiling_enabled()
    return {
        "current": MemoryMonitor.get_memory_details(),
        "gc": {
            "counts": gc.get_count(),
            "thresholds": gc.get_threshold(),
            "is_enabled": gc.isenabled()
        }
    }


@router.post("/profile/gc")
async def trigger_gc() -> dict[str, Any]:
    """Trigger garbage collection and return stats."""
    _check_profiling_enabled()
    
    before = MemoryMonitor.get_memory_details()
    collected = gc.collect()
    after = MemoryMonitor.get_memory_details()
    
    return {
        "collected_objects": collected,
        "memory_before_mb": before.get("rss_mb", 0),
        "memory_after_mb": after.get("rss_mb", 0),
        "freed_mb": before.get("rss_mb", 0) - after.get("rss_mb", 0)
    }


@router.post("/profile/reset")
async def reset_profile_stats() -> dict[str, str]:
    """Reset all profiling statistics."""
    _check_profiling_enabled()
    profiler = get_profiler()
    profiler.reset_stats()
    return {"status": "reset", "timestamp": datetime.now().isoformat()}


@router.get("/profile/dashboard", response_class=HTMLResponse)
async def profile_dashboard() -> HTMLResponse:
    """Interactive HTML dashboard for profiling data."""
    _check_profiling_enabled()
    
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Agent System - Performance Dashboard</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background: #1a1a2e;
            color: #eee;
        }
        h1 {
            color: #00d4ff;
            border-bottom: 2px solid #00d4ff;
            padding-bottom: 10px;
        }
        h2 {
            color: #ff6b6b;
            margin-top: 30px;
        }
        .dashboard {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
        }
        .card {
            background: #16213e;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .card h3 {
            margin-top: 0;
            color: #00d4ff;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .metric {
            font-size: 36px;
            font-weight: bold;
            color: #4ade80;
        }
        .metric.warning {
            color: #fbbf24;
        }
        .metric.danger {
            color: #f87171;
        }
        .label {
            font-size: 12px;
            color: #888;
            text-transform: uppercase;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }
        th, td {
            padding: 8px 12px;
            text-align: left;
            border-bottom: 1px solid #333;
            word-wrap: break-word;
            overflow-wrap: break-word;
            max-width: 300px;
        }
        th {
            color: #00d4ff;
            font-size: 12px;
            text-transform: uppercase;
        }
        .task-item {
            background: #0f0f23;
            border-radius: 5px;
            padding: 10px;
            margin: 5px 0;
            font-family: monospace;
            font-size: 12px;
            word-wrap: break-word;
            overflow-wrap: break-word;
            word-break: break-all;
        }
        .refresh-btn {
            background: #00d4ff;
            color: #1a1a2e;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-weight: bold;
        }
        .refresh-btn:hover {
            background: #00b4df;
        }
        .auto-refresh {
            margin-left: 20px;
        }
        #lastUpdate {
            color: #666;
            font-size: 12px;
            margin-left: 20px;
        }
    </style>
</head>
<body>
    <h1>🔬 Agent System Performance Dashboard</h1>
    <div>
        <button class="refresh-btn" onclick="refresh()">Refresh Now</button>
        <label class="auto-refresh">
            <input type="checkbox" id="autoRefresh" checked> Auto-refresh (5s)
        </label>
        <span id="lastUpdate"></span>
    </div>
    
    <div class="dashboard" id="dashboard">
        <div class="card">
            <h3>Loading...</h3>
            <p>Fetching profiling data...</p>
        </div>
    </div>

    <script>
        let refreshInterval;
        
        async function refresh() {
            try {
                const response = await fetch('/debug/profile');
                const data = await response.json();
                renderDashboard(data);
                document.getElementById('lastUpdate').textContent = 
                    'Last update: ' + new Date().toLocaleTimeString();
            } catch (error) {
                console.error('Failed to fetch profile data:', error);
            }
        }
        
        function renderDashboard(data) {
            const dashboard = document.getElementById('dashboard');
            
            const lagClass = data.event_loop.current_ms > 100 ? 'danger' : 
                            data.event_loop.current_ms > 50 ? 'warning' : '';
            const memClass = data.memory.rss_mb > 1000 ? 'danger' :
                            data.memory.rss_mb > 500 ? 'warning' : '';
            
            let html = `
                <div class="card">
                    <h3>📊 Overview</h3>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                        <div>
                            <div class="metric">${data.requests.active.length}</div>
                            <div class="label">Active Requests</div>
                        </div>
                        <div>
                            <div class="metric">${data.async_tasks.total_count}</div>
                            <div class="label">Async Tasks</div>
                        </div>
                        <div>
                            <div class="metric ${lagClass}">${data.event_loop.current_ms?.toFixed(1) || 0}ms</div>
                            <div class="label">Event Loop Lag</div>
                        </div>
                        <div>
                            <div class="metric ${memClass}">${data.memory.rss_mb?.toFixed(0) || 0}MB</div>
                            <div class="label">Memory Usage</div>
                        </div>
                    </div>
                </div>
                
                <div class="card">
                    <h3>🚨 Active Requests</h3>
                    ${data.requests.active.length === 0 ? '<p>No active requests</p>' : `
                    <table>
                        <tr><th>Path</th><th>Method</th><th>Duration</th></tr>
                        ${data.requests.active.map(r => `
                            <tr>
                                <td>${r.path}</td>
                                <td>${r.method}</td>
                                <td class="${r.duration_ms > 1000 ? 'danger' : ''}">${r.duration_ms?.toFixed(0)}ms</td>
                            </tr>
                        `).join('')}
                    </table>`}
                </div>
                
                <div class="card">
                    <h3>🐢 Slow Requests (Recent)</h3>
                    ${data.requests.slow_recent.length === 0 ? '<p>No slow requests recorded</p>' : `
                    <table>
                        <tr><th>Path</th><th>Duration</th><th>Status</th></tr>
                        ${data.requests.slow_recent.map(r => `
                            <tr>
                                <td>${r.path}</td>
                                <td class="danger">${r.duration_ms?.toFixed(0)}ms</td>
                                <td>${r.status}</td>
                            </tr>
                        `).join('')}
                    </table>`}
                </div>
                
                <div class="card">
                    <h3>⚡ Async Tasks (Top 10)</h3>
                    ${data.async_tasks.tasks.slice(0, 10).map(t => `
                        <div class="task-item">
                            <strong>${t.name}</strong><br>
                            ${t.coro}<br>
                            <span style="color: ${t.done ? '#4ade80' : '#fbbf24'}">
                                ${t.done ? '✓ Done' : t.cancelled ? '✗ Cancelled' : '⏳ Running'}
                            </span>
                        </div>
                    `).join('')}
                </div>
                
                <div class="card">
                    <h3>📈 Request Stats by Path</h3>
                    <table>
                        <tr><th>Path</th><th>Count</th><th>Avg</th><th>Slow %</th></tr>
                        ${Object.entries(data.requests.stats_by_path).map(([path, stats]) => `
                            <tr>
                                <td>${path}</td>
                                <td>${stats.count}</td>
                                <td>${stats.avg_ms?.toFixed(0)}ms</td>
                                <td class="${stats.slow_pct > 10 ? 'danger' : ''}">${stats.slow_pct?.toFixed(1)}%</td>
                            </tr>
                        `).join('')}
                    </table>
                </div>
                
                <div class="card">
                    <h3>🧵 Threads</h3>
                    <div class="metric">${data.threads.count}</div>
                    <div class="label">Active Threads</div>
                    <div style="margin-top: 10px; font-family: monospace; font-size: 11px;">
                        ${data.threads.names.join('<br>')}
                    </div>
                </div>
            `;
            
            dashboard.innerHTML = html;
        }
        
        // Initial load
        refresh();
        
        // Auto-refresh
        document.getElementById('autoRefresh').addEventListener('change', function() {
            if (this.checked) {
                refreshInterval = setInterval(refresh, 5000);
            } else {
                clearInterval(refreshInterval);
            }
        });
        
        refreshInterval = setInterval(refresh, 5000);
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Basic health check with minimal profiling info.
    
    Available even when profiling is disabled.
    """
    try:
        all_tasks = asyncio.all_tasks()
        task_count = len(all_tasks)
    except RuntimeError:
        task_count = 0
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "profiling_enabled": PROFILING_ENABLED,
        "async_tasks": task_count,
        "threads": __import__('threading').active_count(),
        "memory_mb": MemoryMonitor.get_memory_mb()
    }
