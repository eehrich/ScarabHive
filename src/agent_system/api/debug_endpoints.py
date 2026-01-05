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
    <nav style="background: #0f0f23; padding: 10px 20px; margin: -20px -20px 20px -20px; display: flex; gap: 20px;">
        <a href="/debug/profile/dashboard" style="color: #00d4ff; text-decoration: none; font-weight: bold;">🔬 Performance</a>
        <a href="/debug/memory/dashboard" style="color: #888; text-decoration: none;">🧠 Memory</a>
    </nav>
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


# =============================================================================
# Memory Profiling Endpoints
# =============================================================================

def _check_memory_profiling_enabled():
    """Raise 404 if memory profiling is not enabled."""
    from ..utils.memory_profiling import MEMORY_PROFILING_ENABLED
    if not MEMORY_PROFILING_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Memory profiling not enabled. Set AGENT_ENABLE_MEMORY_PROFILING=1 to enable."
        )


@router.get("/memory")
async def get_memory_report() -> dict[str, Any]:
    """Get comprehensive memory profiling report.
    
    Returns detailed information about:
    - Current memory usage
    - Object counts by type
    - Tracemalloc allocation tracking
    - Memory trend analysis
    - Potential memory leaks
    - Reference cycles
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_memory_report_async
    return await get_memory_report_async()


@router.post("/memory/snapshot")
async def take_memory_snapshot() -> dict[str, Any]:
    """Take a memory snapshot for comparison.
    
    Snapshots are stored internally and used for:
    - Comparing memory growth over time
    - Identifying consistently growing object types
    - Detecting memory leaks
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import take_memory_snapshot_async
    snapshot = await take_memory_snapshot_async()
    return {
        "status": "snapshot_taken",
        "timestamp": snapshot.timestamp.isoformat(),
        "total_mb": snapshot.total_mb,
        "object_count": sum(snapshot.object_counts.values())
    }


@router.get("/memory/diff")
async def get_memory_diff() -> dict[str, Any]:
    """Get difference between last two memory snapshots.
    
    Shows:
    - Memory growth in MB
    - Object count changes by type
    - Top growing object types (potential leaks)
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    
    detector = get_leak_detector()
    diff = detector.get_latest_diff()
    
    if diff is None:
        return {"status": "insufficient_snapshots", "message": "Need at least 2 snapshots"}
    
    return diff.to_dict()


@router.get("/memory/trend")
async def get_memory_trend() -> dict[str, Any]:
    """Analyze memory trend over all snapshots.
    
    Identifies:
    - Overall memory growth rate
    - Consistently growing object types (likely leaks)
    - Memory growth per hour
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    
    detector = get_leak_detector()
    return detector.get_trend()


@router.get("/memory/objects")
async def get_object_growth() -> dict[str, Any]:
    """Get object count changes from baseline.
    
    Shows which object types have grown since profiling started.
    Useful for identifying accumulating objects.
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_object_growth_async
    
    growth = await get_object_growth_async()
    # Sort by absolute growth
    sorted_growth = sorted(growth.items(), key=lambda x: abs(x[1]), reverse=True)
    
    return {
        "total_types_changed": len(growth),
        "growth": dict(sorted_growth[:50])  # Top 50
    }


@router.post("/memory/baseline")
async def set_memory_baseline() -> dict[str, Any]:
    """Set current object counts as baseline.
    
    Future calls to /memory/objects will show changes from this point.
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    
    detector = get_leak_detector()
    await detector._object_tracker.set_baseline_async()
    
    return {
        "status": "baseline_set",
        "timestamp": datetime.now().isoformat()
    }


@router.get("/memory/tracemalloc")
async def get_tracemalloc_stats() -> dict[str, Any]:
    """Get tracemalloc allocation statistics.
    
    Shows top memory allocations with file:line information.
    Requires AGENT_ENABLE_MEMORY_PROFILING=1.
    """
    _check_memory_profiling_enabled()
    import tracemalloc
    
    if not tracemalloc.is_tracing():
        return {"status": "not_tracing", "message": "tracemalloc not started"}
    
    snapshot = tracemalloc.take_snapshot()
    
    # Get stats by line
    top_by_line = []
    for stat in snapshot.statistics('lineno')[:30]:
        top_by_line.append({
            "file": str(stat.traceback),
            "size_kb": stat.size / 1024,
            "count": stat.count
        })
    
    # Get stats by file
    top_by_file = []
    for stat in snapshot.statistics('filename')[:20]:
        top_by_file.append({
            "file": str(stat.traceback),
            "size_kb": stat.size / 1024,
            "count": stat.count
        })
    
    current, peak = tracemalloc.get_traced_memory()
    
    return {
        "status": "tracing",
        "current_mb": current / (1024 * 1024),
        "peak_mb": peak / (1024 * 1024),
        "top_by_line": top_by_line,
        "top_by_file": top_by_file
    }


@router.get("/memory/dashboard", response_class=HTMLResponse)
async def memory_dashboard() -> HTMLResponse:
    """Interactive HTML dashboard for memory profiling."""
    _check_memory_profiling_enabled()
    
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Agent System - Memory Profiling Dashboard</title>
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
        .metric.warning { color: #fbbf24; }
        .metric.danger { color: #f87171; }
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
            font-size: 13px;
        }
        th { color: #00d4ff; font-size: 12px; text-transform: uppercase; }
        .btn {
            background: #00d4ff;
            color: #1a1a2e;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            margin-right: 10px;
            font-weight: bold;
        }
        .btn:hover { background: #00b4d8; }
        .btn.secondary { background: #ff6b6b; }
        .controls { margin-bottom: 20px; }
        .leak-warning {
            background: #7f1d1d;
            border: 1px solid #dc2626;
            border-radius: 5px;
            padding: 15px;
            margin: 10px 0;
        }
        .growth-positive { color: #f87171; }
        .growth-negative { color: #4ade80; }
    </style>
</head>
<body>
    <nav style="background: #0f0f23; padding: 10px 20px; margin: -20px -20px 20px -20px; display: flex; gap: 20px;">
        <a href="/debug/profile/dashboard" style="color: #888; text-decoration: none;">🔬 Performance</a>
        <a href="/debug/memory/dashboard" style="color: #00d4ff; text-decoration: none; font-weight: bold;">🧠 Memory</a>
    </nav>
    <h1>🧠 Memory Profiling Dashboard</h1>
    
    <div class="controls">
        <button class="btn" onclick="takeSnapshot()">📸 Take Snapshot</button>
        <button class="btn" onclick="setBaseline()">🎯 Set Baseline</button>
        <button class="btn" onclick="triggerGC()">🗑️ Trigger GC</button>
        <button class="btn" onclick="refresh()">🔄 Refresh</button>
        <label style="margin-left: 20px;">
            <input type="checkbox" id="autoRefresh" checked> Auto-refresh (10s)
        </label>
    </div>
    
    <div class="dashboard">
        <div class="card">
            <h3>Current Memory</h3>
            <div class="metric" id="memoryMB">--</div>
            <div class="label">MB (RSS)</div>
            <div style="margin-top: 10px; color: #888;">
                Peak: <span id="peakMB">--</span> MB |
                Objects: <span id="objectCount">--</span>
            </div>
        </div>
        
        <div class="card">
            <h3>Memory Trend</h3>
            <div class="metric" id="growthRate">--</div>
            <div class="label">MB/hour growth rate</div>
            <div style="margin-top: 10px; color: #888;">
                Snapshots: <span id="snapshotCount">--</span> |
                Time span: <span id="timeSpan">--</span>s
            </div>
        </div>
        
        <div class="card">
            <h3>GC Stats</h3>
            <div id="gcStats">Loading...</div>
        </div>
        
        <div class="card">
            <h3>Potential Memory Leaks</h3>
            <div id="leaks">Loading...</div>
        </div>
    </div>
    
    <h2>Top Object Types by Count</h2>
    <div class="card">
        <table>
            <thead>
                <tr><th>Type</th><th>Count</th><th>Growth</th></tr>
            </thead>
            <tbody id="objectsTable"></tbody>
        </table>
    </div>
    
    <h2>Top Memory Allocations (tracemalloc)</h2>
    <div class="card">
        <table>
            <thead>
                <tr><th>Location</th><th>Size (KB)</th><th>Count</th></tr>
            </thead>
            <tbody id="allocTable"></tbody>
        </table>
    </div>

    <script>
        let refreshInterval;
        
        async function refresh() {
            try {
                // Get main report
                const report = await fetch('/debug/memory').then(r => r.json());
                
                // Update metrics
                if (report.memory) {
                    const mem = report.memory.rss_mb || 0;
                    const el = document.getElementById('memoryMB');
                    el.textContent = mem.toFixed(1);
                    el.className = 'metric' + (mem > 500 ? ' danger' : mem > 200 ? ' warning' : '');
                    document.getElementById('objectCount').textContent = 
                        report.gc?.total_objects?.toLocaleString() || '--';
                }
                
                // Tracemalloc peak
                const tm = await fetch('/debug/memory/tracemalloc').then(r => r.json());
                if (tm.peak_mb) {
                    document.getElementById('peakMB').textContent = tm.peak_mb.toFixed(1);
                }
                
                // GC stats
                if (report.gc) {
                    document.getElementById('gcStats').innerHTML = `
                        <div>Generation counts: ${report.gc.counts?.join(', ') || '--'}</div>
                        <div>Thresholds: ${report.gc.threshold?.join(', ') || '--'}</div>
                        <div>Garbage items: ${report.gc.garbage_count || 0}</div>
                    `;
                }
                
                // Trend analysis
                const trend = await fetch('/debug/memory/trend').then(r => r.json());
                if (trend.memory_mb) {
                    const rate = trend.memory_mb.growth_rate_mb_per_hour || 0;
                    const el = document.getElementById('growthRate');
                    el.textContent = rate.toFixed(2);
                    el.className = 'metric' + (rate > 10 ? ' danger' : rate > 2 ? ' warning' : '');
                    document.getElementById('snapshotCount').textContent = trend.snapshots || '--';
                    document.getElementById('timeSpan').textContent = 
                        (trend.time_span_seconds || 0).toFixed(0);
                }
                
                // Potential leaks
                if (trend.likely_leaks?.length > 0) {
                    document.getElementById('leaks').innerHTML = trend.likely_leaks
                        .slice(0, 10)
                        .map(l => `<div class="leak-warning">
                            <strong>${l.type}</strong>: +${l.total_growth} objects
                        </div>`)
                        .join('');
                } else {
                    document.getElementById('leaks').innerHTML = 
                        '<div style="color: #4ade80;">No obvious leaks detected</div>';
                }
                
                // Object table
                if (report.top_objects) {
                    const growth = await fetch('/debug/memory/objects').then(r => r.json());
                    document.getElementById('objectsTable').innerHTML = report.top_objects
                        .slice(0, 20)
                        .map(o => {
                            const g = growth.growth?.[o.type] || 0;
                            const gClass = g > 0 ? 'growth-positive' : g < 0 ? 'growth-negative' : '';
                            return `<tr>
                                <td>${o.type}</td>
                                <td>${o.count.toLocaleString()}</td>
                                <td class="${gClass}">${g > 0 ? '+' : ''}${g}</td>
                            </tr>`;
                        })
                        .join('');
                }
                
                // Allocation table
                if (tm.top_by_line) {
                    document.getElementById('allocTable').innerHTML = tm.top_by_line
                        .slice(0, 15)
                        .map(a => `<tr>
                            <td style="font-family: monospace; font-size: 11px;">${a.file}</td>
                            <td>${a.size_kb.toFixed(1)}</td>
                            <td>${a.count}</td>
                        </tr>`)
                        .join('');
                }
                
            } catch (e) {
                console.error('Refresh error:', e);
            }
        }
        
        async function takeSnapshot() {
            await fetch('/debug/memory/snapshot', {method: 'POST'});
            refresh();
        }
        
        async function setBaseline() {
            await fetch('/debug/memory/baseline', {method: 'POST'});
            refresh();
        }
        
        async function triggerGC() {
            const result = await fetch('/debug/profile/gc', {method: 'POST'}).then(r => r.json());
            alert(`GC collected ${result.collected_objects} objects, freed ${result.freed_mb?.toFixed(2) || 0} MB`);
            refresh();
        }
        
        // Initial load
        refresh();
        
        // Auto-refresh
        document.getElementById('autoRefresh').addEventListener('change', function() {
            if (this.checked) {
                refreshInterval = setInterval(refresh, 10000);
            } else {
                clearInterval(refreshInterval);
            }
        });
        
        refreshInterval = setInterval(refresh, 10000);
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)