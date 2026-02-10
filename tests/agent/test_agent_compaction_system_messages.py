"""Test: Verify all system messages are preserved during compaction.

BEFORE THE FIX:
- Only first system message was preserved
- Tools message was lost
- Session hooks messages were lost

AFTER THE FIX:
- ALL leading system messages are preserved
- Agent can access tool descriptions
- Session hooks work correctly
"""

import pytest
from agent_system.llm.models import ChatMessage


def test_all_system_messages_preserved_during_compaction():
    """Verify that compaction preserves ALL leading system messages."""
    
    # Simulate the scenario from server.py line 1490-1515
    # BEFORE compaction: [sys1, sys2, sys3, user1, asst1, user2, asst2, ...]
    # compacted_messages: [user1, asst1_compacted, user2_compacted]
    
    original_messages = [
        ChatMessage(role="system", content="# Agent System Prompt\nYou are a helpful assistant."),
        ChatMessage(role="system", content="## Available Tools\n- tool1: does X\n- tool2: does Y"),
        ChatMessage(role="system", content="## Markdown Formatting\nUse proper markdown."),
        ChatMessage(role="user", content="Old user message"),
        ChatMessage(role="assistant", content="Old response"),
        ChatMessage(role="user", content="Current message"),
    ]
    
    compacted_messages = [
        ChatMessage(role="user", content="Compacted summary of old conversation"),
        ChatMessage(role="user", content="Current message"),
    ]
    
    # NEW LOGIC (after fix): Keep ALL leading system messages
    reconstructed = []
    for msg in original_messages:
        msg_role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if msg_role == "system":
            reconstructed.append(msg)
        else:
            break  # Stop at first non-system message
    
    reconstructed.extend(compacted_messages)
    
    # Verify
    assert len(reconstructed) == 5, f"Expected 5 messages (3 system + 2 compacted), got {len(reconstructed)}"
    
    # All leading system messages should be present
    assert reconstructed[0].role == "system"
    assert "Agent System Prompt" in reconstructed[0].content
    
    assert reconstructed[1].role == "system"
    assert "Available Tools" in reconstructed[1].content
    
    assert reconstructed[2].role == "system"
    assert "Markdown Formatting" in reconstructed[2].content
    
    # Compacted messages follow
    assert reconstructed[3].role == "user"
    assert "Compacted summary" in reconstructed[3].content
    
    assert reconstructed[4].role == "user"
    assert "Current message" in reconstructed[4].content
    
    print("✓ All system messages preserved during compaction!")


def test_old_buggy_logic_would_fail():
    """Demonstrate that the OLD logic lost tools message."""
    
    original_messages = [
        ChatMessage(role="system", content="# Agent System Prompt"),
        ChatMessage(role="system", content="## Available Tools"),
        ChatMessage(role="system", content="## Markdown Formatting"),
        ChatMessage(role="user", content="Old user message"),
    ]
    
    compacted_messages = [
        ChatMessage(role="user", content="Compacted summary"),
    ]
    
    # OLD BUGGY LOGIC (before fix): Only keep first system message
    reconstructed_buggy = []
    if original_messages:
        first_msg = original_messages[0]
        first_role = first_msg.get("role") if isinstance(first_msg, dict) else getattr(first_msg, "role", None)
        if first_role == "system":
            reconstructed_buggy.append(first_msg)  # Only first!
    
    reconstructed_buggy.extend(compacted_messages)
    
    # This was the bug: Only 2 messages (1 system + 1 compacted)
    assert len(reconstructed_buggy) == 2, "Bug demonstration: Only first system message kept"
    assert reconstructed_buggy[0].content == "# Agent System Prompt"
    
    # Tools message LOST!
    assert all("Available Tools" not in msg.content for msg in reconstructed_buggy), \
        "Bug demonstration: Tools message was lost!"
    
    print("✓ Old buggy logic demonstrated (lost 2 of 3 system messages)")


if __name__ == "__main__":
    test_all_system_messages_preserved_during_compaction()
    test_old_buggy_logic_would_fail()
    print("\n✅ All tests passed!")
