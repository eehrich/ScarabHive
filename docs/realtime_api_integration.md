# OpenAI Realtime API Integration Design

## Overview

This document describes the design for integrating OpenAI's Realtime API into the AgentSystem, enabling text and audio-based interactions through a WebSocket connection while maintaining compatibility with the existing chat interface.

## Problem Statement

The current system uses the Chat Completions API (REST-based) for all LLM interactions. OpenAI's Realtime API provides:
- Low-latency speech-to-speech interactions
- WebSocket-based event-driven protocol
- Text-only mode support (our primary use case)
- Native streaming support
- Function/tool calling capabilities

**Key Challenge**: The Realtime API uses a completely different protocol (WebSocket events) compared to Chat Completions (REST request/response).

## Requirements

### Functional Requirements
1. Support text-based conversations via Realtime API (audio can be added later)
2. Maintain existing `chat_tools()` and `chat_tools_streaming()` interface compatibility
3. Support tool/function calling through Realtime API
4. Handle session configuration (instructions, temperature, etc.)
5. Proper error handling and connection management
6. Cancellation token support for request interruption

### Non-Functional Requirements
1. Clean architecture - no hacks or workarounds
2. Reusable WebSocket connection where appropriate
3. Proper resource cleanup (close WebSocket on errors)
4. Comprehensive logging for debugging
5. Type safety with proper models

## Architecture

### Component Structure

```
OpenAIAsyncClient (existing)
├── chat_tools() - REST API dispatcher
│   ├── Uses Chat Completions API (existing)
│   └── Uses Realtime API (new) - via _chat_tools_realtime()
│
├── chat_tools_streaming() - Streaming dispatcher  
│   ├── Uses Chat Completions Streaming (existing)
│   └── Uses Realtime API (new) - via _chat_tools_streaming_realtime()
│
└── RealtimeSession (new helper class)
    ├── WebSocket connection management
    ├── Event serialization/deserialization
    ├── Session configuration
    └── Conversation state tracking
```

### API Selection Logic

```python
def chat_tools(self, messages, tools, cancellation_token):
    api_type = self.get_api_type()  # Returns 'chat_completions' or 'realtime'
    
    if api_type == 'realtime':
        return await self._chat_tools_realtime(messages, tools, cancellation_token)
    else:
        return await self._chat_tools_chat_completions(messages, tools, cancellation_token)
```

### Realtime API Protocol

**WebSocket URL**: `wss://api.openai.com/v1/realtime?model={model}`

**Authentication**: Bearer token in headers or query params

**Event Flow** (Text-only mode):
```
Client -> Server:
1. session.update (configure session)
2. conversation.item.create (add user message)
3. response.create (request LLM response)

Server -> Client:
1. session.created (session ready)
2. conversation.item.created (message added confirmation)
3. response.created (response started)
4. response.content_part.added (content chunk)
5. response.text.delta (streaming text)
6. response.text.done (text complete)
7. response.done (response finished)
```

**Tool Calling Flow**:
```
Server -> Client:
1. response.function_call_arguments.delta (streaming tool args)
2. response.function_call_arguments.done (tool call ready)

Client -> Server:
3. conversation.item.create (add function result)
4. response.create (continue conversation)
```

## Implementation Plan

### Phase 1: Core Realtime Session Handler

**File**: `src/agent_system/llm/realtime_session.py` (new)

```python
class RealtimeSession:
    """Manages a single Realtime API WebSocket session."""
    
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key
        self.ws = None
        self.event_queue = asyncio.Queue()
    
    async def connect(self) -> None:
        """Establish WebSocket connection."""
    
    async def configure_session(self, config: dict) -> None:
        """Send session.update event."""
    
    async def add_conversation_item(self, item: dict) -> None:
        """Send conversation.item.create event."""
    
    async def create_response(self, tools: Optional[list] = None) -> None:
        """Send response.create event."""
    
    async def receive_events(self) -> AsyncGenerator:
        """Yield server events as they arrive."""
    
    async def close(self) -> None:
        """Close WebSocket connection."""
```

### Phase 2: Message Conversion Layer

**File**: `src/agent_system/llm/realtime_adapter.py` (new)

```python
class RealtimeMessageAdapter:
    """Converts between ChatMessage format and Realtime API conversation items."""
    
    @staticmethod
    def messages_to_conversation_items(messages: list[ChatMessage]) -> list[dict]:
        """Convert ChatMessage list to Realtime conversation items.
        
        ChatMessage(role='user', content='Hello')
        -> {type: 'message', role: 'user', content: [{type: 'input_text', text: 'Hello'}]}
        """
    
    @staticmethod
    def tools_to_realtime_tools(tools: list[dict]) -> list[dict]:
        """Convert tool schemas to Realtime API tool format."""
    
    @staticmethod
    def parse_response_event(event: dict) -> dict:
        """Extract assistant message from response events."""
```

### Phase 3: Integration into OpenAIAsyncClient

**File**: `src/agent_system/llm/openai_client.py` (modify)

```python
async def chat_tools(self, messages, tools, cancellation_token=None):
    """Dispatch to appropriate API based on capabilities."""
    api_type = self.get_api_type()
    
    if api_type == 'realtime':
        return await self._chat_tools_realtime(messages, tools, cancellation_token)
    else:
        return await self._chat_tools_chat_completions(messages, tools, cancellation_token)

async def _chat_tools_realtime(self, messages, tools, cancellation_token):
    """Non-streaming Realtime API call."""
    # 1. Create session
    # 2. Convert messages to conversation items
    # 3. Configure session with tools
    # 4. Create response
    # 5. Collect all events until response.done
    # 6. Return final assistant message
    
async def chat_tools_streaming(self, messages, tools, cancellation_token=None):
    """Dispatch streaming to appropriate API."""
    api_type = self.get_api_type()
    
    if api_type == 'realtime':
        async for event in self._chat_tools_streaming_realtime(messages, tools, cancellation_token):
            yield event
    else:
        async for event in self._chat_tools_streaming_chat_completions(messages, tools, cancellation_token):
            yield event

async def _chat_tools_streaming_realtime(self, messages, tools, cancellation_token):
    """Streaming Realtime API call - yields events in real-time."""
    # 1. Create session
    # 2. Configure and add messages
    # 3. Yield events as they arrive:
    #    - response.text.delta -> {type: 'content_delta', delta: text}
    #    - response.function_call_arguments.delta -> accumulate
    #    - response.done -> {type: 'final', assistant: {...}}
```

### Phase 4: Configuration and Testing

**Updates to llm.yaml**:
- Already configured with `supported_api_types: [realtime]`
- Add realtime-specific config if needed (voice, modalities, etc.)

**Test Cases**:
1. Simple text conversation via Realtime API
2. Streaming text response
3. Tool calling through Realtime API
4. Error handling (connection failures, timeouts)
5. Cancellation token interruption

## Data Models

### Realtime API Event Models

```python
from pydantic import BaseModel
from typing import Literal, Optional, List, Any

class RealtimeSessionConfig(BaseModel):
    """Configuration for Realtime API session."""
    modalities: List[Literal["text", "audio"]] = ["text"]
    instructions: Optional[str] = None
    voice: Optional[str] = None
    input_audio_format: Optional[str] = None
    output_audio_format: Optional[str] = None
    input_audio_transcription: Optional[dict] = None
    turn_detection: Optional[dict] = None
    tools: Optional[List[dict]] = None
    tool_choice: str = "auto"
    temperature: Optional[float] = None
    max_response_output_tokens: Optional[int] = None

class RealtimeConversationItem(BaseModel):
    """A conversation item for Realtime API."""
    type: Literal["message", "function_call", "function_call_output"]
    role: Optional[str] = None
    content: Optional[List[dict]] = None
    # ... additional fields

class RealtimeEvent(BaseModel):
    """Base model for Realtime API events."""
    type: str
    event_id: Optional[str] = None
    # ... common fields
```

## Error Handling Strategy

### Connection Errors
- Retry with exponential backoff (similar to existing retry logic)
- Log detailed error information
- Clean up WebSocket resources
- Fallback: Not applicable (Realtime-only models can't use Chat Completions)

### Protocol Errors
- Handle malformed events gracefully
- Log unexpected event types
- Continue processing or close session based on severity

### Cancellation
- Close WebSocket immediately on cancellation token trigger
- Ensure no zombie connections
- Proper cleanup in finally blocks

## Resource Management

### WebSocket Lifecycle
```python
session = None
try:
    session = RealtimeSession(model, api_key)
    await session.connect()
    # ... use session
finally:
    if session:
        await session.close()
```

### Connection Pooling (Future Enhancement)
- Keep-alive connection for multiple requests
- Session reuse with conversation.item.truncate
- Connection timeout and refresh logic

## Migration Path

### Backward Compatibility
- Existing Chat Completions API code remains unchanged
- API selection is automatic based on model capabilities
- No changes required to Agent code or calling code

### Testing Strategy
1. Unit tests for RealtimeSession
2. Integration tests for message conversion
3. End-to-end tests with actual Realtime API
4. Regression tests for Chat Completions API

## Security Considerations

1. **API Key Protection**: Passed via headers, not URL params
2. **SSL/TLS**: Use secure WebSocket (wss://)
3. **Token Validation**: Verify API key before connection
4. **Rate Limiting**: Respect OpenAI rate limits
5. **Data Privacy**: No logging of sensitive content

## Performance Considerations

1. **Latency**: WebSocket reduces overhead vs REST
2. **Throughput**: Single connection can handle multiple turns
3. **Memory**: Event buffering should be bounded
4. **CPU**: JSON parsing is lightweight

## Future Enhancements

1. **Audio Support**: Add audio input/output handling
2. **Session Persistence**: Reuse connections across requests
3. **Advanced Features**:
   - Voice Activity Detection (VAD)
   - Input audio transcription
   - Audio format conversion
4. **Monitoring**: Metrics for connection health, latency
5. **Fallback Logic**: Graceful degradation if Realtime API unavailable

## References

- [OpenAI Realtime API Docs](https://platform.openai.com/docs/guides/realtime)
- [Realtime API Events Reference](https://platform.openai.com/docs/api-reference/realtime-client-events)
- [WebSocket RFC 6455](https://tools.ietf.org/html/rfc6455)

## Implementation Checklist

- [ ] Create `realtime_session.py` with WebSocket management
- [ ] Create `realtime_adapter.py` with message conversion
- [ ] Add Realtime API models to `models.py`
- [ ] Modify `openai_client.py` with API dispatcher
- [ ] Implement `_chat_tools_realtime()` (non-streaming)
- [ ] Implement `_chat_tools_streaming_realtime()` (streaming)
- [ ] Add error handling and logging
- [ ] Write unit tests
- [ ] Write integration tests
- [ ] Update documentation
- [ ] Test with actual Realtime API endpoints

## Timeline Estimate

- Phase 1 (Core Session): 2-3 hours
- Phase 2 (Adapter): 1-2 hours  
- Phase 3 (Integration): 2-3 hours
- Phase 4 (Testing): 1-2 hours
- **Total**: 6-10 hours for complete implementation
