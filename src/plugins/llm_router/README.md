# LLM Router Plugin

The LLM Router plugin enables routing chat requests to different Large Language Model (LLM) providers using configurable profiles. It supports multiple AI models and providers for specialized tasks or alternative processing approaches.

## Overview

This plugin acts as an intelligent router for LLM requests, allowing you to configure different model profiles for various use cases. Each profile defines a specific provider, model, and settings optimized for particular types of tasks.

## Features

### Core Operations
- **Chat Routing**: Route conversations to specific LLM profiles
- **Profile Management**: List and configure available LLM profiles  
- **Multi-Provider Support**: OpenAI, Anthropic, Google, and more
- **Task Specialization**: Different models for different purposes
- **Fallback Logic**: Automatic failover between providers

### Profile Types
- **Speed Profiles**: Fast models for quick responses
- **Quality Profiles**: High-quality models for complex reasoning
- **Specialized Profiles**: Models optimized for coding, writing, analysis
- **Cost-Effective Profiles**: Budget-friendly options for bulk processing

## Configuration

Configure the LLM Router plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - llm_router

servers:
  llm_router:
    type: llm_router

# LLM profiles are configured in config/llm.yaml:
llm_profiles:
      turbo:
        provider: "openai"
        model: "gpt-4-turbo"
        max_tokens: 4096
        temperature: 0.7
        description: "Fast, high-quality responses"
      
      normal:
        provider: "openai"  
        model: "gpt-4"
        max_tokens: 8192
        temperature: 0.5
        description: "Balanced performance and quality"
        
      think:
        provider: "anthropic"
        model: "claude-3-opus"
        max_tokens: 4096
        temperature: 0.3
        description: "Deep reasoning and analysis"
        
      code:
        provider: "openai"
        model: "gpt-4"
        max_tokens: 8192
        temperature: 0.1
        system_prompt: "You are an expert programmer."
        description: "Specialized for coding tasks"
```

## Usage Examples

### Basic Chat Routing
```python
# Route to turbo profile for quick response
{
  "profile": "turbo",
  "message": "What is the capital of France?"
}

# Route to thinking profile for complex analysis
{
  "profile": "think", 
  "messages": [
    {"role": "user", "content": "Analyze the economic implications of AI automation"}
  ]
}
```

### Specialized Routing
```python
# Use coding profile for programming tasks
{
  "profile": "code",
  "message": "Write a Python function to calculate fibonacci numbers"
}

# Use normal profile for general conversation
{
  "profile": "normal",
  "messages": [
    {"role": "user", "content": "Explain quantum computing in simple terms"}
  ]
}
```

### Profile Management
```python
# List all available profiles
{
  "action": "list_profiles"
}
```

## API Reference

### Tools

#### chat
Routes chat messages to specified LLM profile.

**Parameters:**
- **profile** (required): LLM profile name to use
- **message**: Single message text (alternative to messages array)
- **messages**: Array of chat messages with role and content

#### list_profiles  
Lists all available LLM profiles with their configurations.

**Parameters:** None

### Response Formats

#### Chat Response
```json
{
  "response": "Paris is the capital of France.",
  "profile_used": "turbo",
  "provider": "openai",
  "model": "gpt-4-turbo", 
  "tokens_used": {
    "prompt": 15,
    "completion": 8,
    "total": 23
  },
  "response_time": 1.2
}
```

#### Profile List Response
```json
{
  "profiles": {
    "turbo": {
      "provider": "openai",
      "model": "gpt-4-turbo",
      "max_tokens": 4096,
      "temperature": 0.7,
      "description": "Fast, high-quality responses",
      "status": "active"
    },
    "think": {
      "provider": "anthropic", 
      "model": "claude-3-opus",
      "max_tokens": 4096,
      "temperature": 0.3,
      "description": "Deep reasoning and analysis",
      "status": "active"
    }
  },
  "total_profiles": 2,
  "active_profiles": 2
}
```

## Profile Configuration

### Profile Structure
Each profile defines:
- **provider**: LLM service provider (openai, anthropic, google, etc.)
- **model**: Specific model name
- **max_tokens**: Maximum response length
- **temperature**: Response randomness (0.0-1.0)
- **system_prompt**: Optional system message
- **description**: Human-readable profile purpose

### Example Profiles

#### Speed-Optimized Profile
```yaml
fast:
  provider: "openai"
  model: "gpt-3.5-turbo"
  max_tokens: 2048
  temperature: 0.8
  description: "Quick responses for simple queries"
```

#### Reasoning Profile  
```yaml
reasoning:
  provider: "anthropic"
  model: "claude-3-opus"
  max_tokens: 8192
  temperature: 0.2
  system_prompt: "Think step by step and provide detailed reasoning."
  description: "Complex analysis and reasoning tasks"
```

#### Creative Writing Profile
```yaml
creative:
  provider: "openai"
  model: "gpt-4"
  max_tokens: 4096
  temperature: 0.9
  system_prompt: "You are a creative writing assistant."
  description: "Creative content generation"
```

## Supported Providers

### OpenAI
- **Models**: GPT-4, GPT-4-turbo, GPT-3.5-turbo
- **Features**: Function calling, JSON mode, vision (GPT-4V)
- **Configuration**: API key via environment variable

### Anthropic  
- **Models**: Claude-3-opus, Claude-3-sonnet, Claude-3-haiku
- **Features**: Large context windows, constitutional AI
- **Configuration**: API key via environment variable

### Google
- **Models**: Gemini-pro, Gemini-pro-vision
- **Features**: Multimodal capabilities, large context
- **Configuration**: API key via environment variable

### Local Models
- **Provider**: ollama, llamacpp
- **Models**: Llama, Mistral, CodeLlama (locally hosted)
- **Configuration**: Local server endpoints

## Use Case Examples

### Task-Specific Routing

#### Code Generation
```python
{
  "profile": "code",
  "message": "Create a REST API endpoint for user authentication in Python FastAPI"
}
```

#### Data Analysis  
```python
{
  "profile": "think",
  "message": "Analyze this dataset and identify key trends: [data here]"
}
```

#### Quick Q&A
```python
{
  "profile": "fast", 
  "message": "What's the weather like in New York today?"
}
```

#### Creative Writing
```python
{
  "profile": "creative",
  "message": "Write a short story about AI and human friendship"
}
```

### Multi-Turn Conversations
```python
{
  "profile": "normal",
  "messages": [
    {"role": "user", "content": "I need help planning a vacation"},
    {"role": "assistant", "content": "I'd be happy to help! Where are you thinking of going?"},
    {"role": "user", "content": "Somewhere warm in Europe"}
  ]
}
```

## Performance Optimization

### Profile Selection Guidelines
- **Simple Queries**: Use fast/turbo profiles
- **Complex Reasoning**: Use thinking/analysis profiles  
- **Code Tasks**: Use coding-specialized profiles
- **Creative Tasks**: Use high-temperature creative profiles

### Cost Optimization
- **Tier Models**: Use smaller models for simple tasks
- **Token Limits**: Set appropriate max_tokens for each use case
- **Caching**: Consider response caching for repeated queries

## Error Handling

### Common Errors

#### Profile Not Found
```json
{
  "error": "Profile 'unknown' not found",
  "available_profiles": ["turbo", "normal", "think", "code"],
  "suggestion": "Use list_profiles to see all available profiles"
}
```

#### Provider Error
```json
{
  "error": "OpenAI API error: Rate limit exceeded",
  "profile": "turbo",
  "suggestion": "Try a different profile or wait before retrying"
}
```

#### Configuration Error
```json
{
  "error": "Invalid model configuration for profile 'custom'",
  "details": "Missing required field: model",
  "suggestion": "Check profile configuration in agent.yaml"
}
```

## Best Practices

### Profile Design
1. **Clear Naming**: Use descriptive profile names
2. **Appropriate Settings**: Match temperature to task type
3. **Documentation**: Include helpful descriptions
4. **Testing**: Test profiles with representative queries

### Usage Patterns
1. **Route by Task**: Choose profiles based on task requirements
2. **Fallback Strategy**: Configure backup profiles for reliability
3. **Monitor Usage**: Track token usage and costs per profile
4. **Update Regularly**: Keep model versions current

### Security Considerations
1. **API Key Management**: Store API keys securely
2. **Rate Limiting**: Implement appropriate request limits
3. **Content Filtering**: Consider output filtering for sensitive applications
4. **Access Control**: Restrict profile access as needed

## Troubleshooting

### Profile Issues
- **Profile Not Loading**: Check YAML syntax in configuration
- **Model Unavailable**: Verify model names with provider documentation
- **Authentication Errors**: Confirm API keys are correctly set

### Performance Issues  
- **Slow Responses**: Try faster models or lower max_tokens
- **Rate Limits**: Implement request queuing or use multiple API keys
- **High Costs**: Monitor token usage and optimize profile selection

### Configuration Debugging
- Use `list_profiles` to verify profile availability
- Check logs for detailed error messages
- Validate configuration against provider requirements