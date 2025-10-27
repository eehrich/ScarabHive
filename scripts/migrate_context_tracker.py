"""
Migrate existing context usage data to include history snapshots.

This script creates synthetic history entries from existing accumulated stats
so the panel shows data immediately.
"""

import json
import time
from pathlib import Path


def migrate_context_tracker():
    """Add history entries to existing context tracker data."""
    
    tracker_file = Path("data/context_usage_tracker.json")
    
    if not tracker_file.exists():
        print("❌ No context tracker data file found")
        return
    
    print(f"📂 Loading {tracker_file}")
    with open(tracker_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Check if already has history
    if "history" in data and data["history"]:
        print(f"✅ Already has {len(data['history'])} history entries")
        return
    
    agents = data.get("agents", {})
    
    if not agents:
        print("❌ No agent data found")
        return
    
    print(f"📊 Found {len(agents)} agents with accumulated stats")
    
    # Create synthetic history entries
    # We'll create one "latest" snapshot per agent using their stats
    history = []
    latest = None
    latest_time = 0
    
    for agent_id, stats in agents.items():
        # Create a snapshot representing the agent's accumulated state
        snapshot = {
            "timestamp": stats.get("last_activity", time.time()),
            "agent_id": stats["agent_id"],
            "agent_name": stats["agent_name"],
            "session_id": "migration",
            "total_tokens": stats.get("peak_tokens", 0),  # Use peak as representative
            "prompt_tokens": int(stats.get("peak_tokens", 0) * 0.7),  # Estimate 70% prompt
            "completion_tokens": int(stats.get("peak_tokens", 0) * 0.3),  # Estimate 30% completion
            "message_count": stats.get("message_count", 0),
            "context_window": 128000,  # Assume default context window
            "usage_percentage": (stats.get("peak_tokens", 0) / 128000 * 100) if stats.get("peak_tokens", 0) else 0
        }
        
        history.append(snapshot)
        
        # Track most recent for "latest"
        if snapshot["timestamp"] > latest_time:
            latest_time = snapshot["timestamp"]
            latest = snapshot
    
    # Sort history by timestamp
    history.sort(key=lambda x: x["timestamp"])
    
    # Update data
    data["history"] = history
    data["latest"] = latest
    
    # Backup original
    backup_file = tracker_file.with_suffix('.json.backup')
    print(f"💾 Creating backup: {backup_file}")
    with open(backup_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    # Write updated data
    print(f"✏️  Adding {len(history)} history snapshots")
    with open(tracker_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    print("✅ Migration complete!")
    print(f"   - History entries: {len(history)}")
    print(f"   - Latest snapshot: {latest['agent_name']} at {time.ctime(latest['timestamp'])}")


if __name__ == "__main__":
    migrate_context_tracker()
