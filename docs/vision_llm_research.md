# Vision-Capable LLM Models Research

**Date:** October 7, 2025  
**Purpose:** Research document for Epic 0040 - LLM Vision Support Implementation

## Overview

This document provides comprehensive research on vision-capable LLM models and their API requirements to guide the implementation of image input support in AgentSystem.

## Supported Vision Models (2025)

### 1. OpenAI GPT-5 Vision

**Model Information:**
- **Model Name:** `gpt-5` (latest as of August 2025)
- **Capabilities:** Text and image input, text output
- **Variants:** `gpt-5`, `gpt-5-nano` (smaller, faster, more affordable)

**Image Input Format:**
```python
{
    "type": "input_image",
    "image_url": "data:image/jpeg;base64,{base64_encoded_data}",
    "detail": "high"  # or "low" for reduced token usage
}
```

**Supported Formats:**
- JPEG
- PNG
- GIF
- WEBP

**Size Limits:**
- Maximum dimension: Not explicitly documented (estimated 20MB total request size)
- Detail parameter affects token consumption
- High detail: More tokens, better analysis
- Low detail: Fewer tokens (note: some reports indicate detail parameter may be ignored in gpt-5)

**Integration Approach:**
- Base64 encoding of images
- Multimodal message content with mixed text and image blocks
- URL-based images also supported

**API Pattern:**
```python
response = client.chat.completions.create(
    model="gpt-5",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{image_data}",
                    "detail": "high"
                }
            ]
        }
    ]
)
```

---

### 2. Anthropic Claude Vision

**Model Information:**
- **Models:** Claude Sonnet 4.5, Claude Opus 4.1
- **Capabilities:** Text and image input, text output
- **Context Window:** 200k tokens (1M beta available for Sonnet 4.5)

**Claude Sonnet 4.5:**
- Best for complex agents and coding
- Full multimodal support
- Highest intelligence across most tasks

**Claude Opus 4.1:**
- Exceptional for specialized complex tasks
- Superior reasoning capabilities
- Premium pricing tier

**Image Input Format:**
```python
{
    "type": "image",
    "source": {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": base64_encoded_data
    }
}
```

**Supported Formats:**
- JPEG
- PNG
- GIF
- WEBP

**Size Limits:**
- **Maximum file size:** 30MB per image
- **Maximum resolution:** 8000 x 8000 pixels
- **Recommendation:** Avoid small or low-resolution images

**Special Features:**
- Files API (Beta since April 14, 2025) for reusable file storage
- Store files once, reference by `file_id` in subsequent requests
- Reduces repeated upload overhead
- OCR, chart/diagram interpretation built-in

**Integration Approach:**
- Base64 encoding (primary method)
- URL-based images supported
- Files API for large or frequently used images

**API Pattern:**
```python
import anthropic
import base64

client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-3-5-sonnet-20240620",
    max_tokens=1024,
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_data
                    }
                },
                {"type": "text", "text": "What type of animal is in this image?"}
            ]
        }
    ]
)
```

---

### 3. Google Gemini Vision

**Model Information:**
- **Models:** Gemini 2.5 Pro, Gemini Pro (various tiers)
- **Capabilities:** Full multimodal (text, images, audio, video)
- **Context Window:** Up to 1,000,000 tokens (Gemini 2.5 Pro)

**Gemini 2.5 Pro:**
- Advanced multimodal capabilities
- Supports audio and video in addition to images
- Research, legal, enterprise automation focus
- Up to 1M tokens context

**Image Input Format:**
Two approaches:
1. **Inline data** (for smaller files <20MB total request)
2. **File API upload** (for larger files or reusable images)

**Supported Formats:**
- JPEG
- PNG (PNG8, PNG24)
- GIF (animated GIF uses first frame only)
- WEBP
- BMP
- RAW
- ICO
- PDF
- TIFF

**Size Limits:**
- **Inline method:** Total request <20MB including prompts
- **File API:** Up to 50MB per file (Vertex AI Gemini Pro)
- **Resolution:** Minimum 640x480 pixels recommended
- **Maximum:** 75M pixels (length × width) - images are resized if larger
- **Batch:** Up to 3,000 files (Vertex AI)

**Integration Approach:**
- Inline base64 for small images
- File API for large images or batch processing
- Supports multiple images per prompt
- Async/batch workflows available

**API Pattern (Inline):**
```python
import google.generativeai as genai

model = genai.GenerativeModel('gemini-2.5-pro')
response = model.generate_content([
    "What's in this image?",
    {"mime_type": "image/jpeg", "data": base64_image_data}
])
```

**API Pattern (File API):**
```python
# Upload file
file = genai.upload_file(path="image.jpg")

# Use file in request
response = model.generate_content([
    "What's in this image?",
    file
])
```

---

## Comparison Matrix

| Feature | GPT-5 Vision | Claude Vision | Gemini Vision |
|---------|--------------|---------------|---------------|
| **Supported Formats** | JPEG, PNG, GIF, WEBP | JPEG, PNG, GIF, WEBP | JPEG, PNG, GIF, WEBP, BMP, RAW, ICO, PDF, TIFF |
| **Max File Size** | ~20MB (request) | 30MB | 50MB (File API) |
| **Max Resolution** | Not specified | 8000x8000 px | 75M pixels |
| **Min Resolution** | Not specified | Avoid low-res | 640x480 px |
| **Context Window** | 32k-128k (model dependent) | 200k (1M beta) | Up to 1M tokens |
| **Encoding** | Base64, URL | Base64, URL, Files API | Base64, File API |
| **Detail Control** | Yes (high/low) | No | No |
| **Batch Upload** | No | Files API | File API (3000 files) |
| **Multimodal Beyond Images** | No | No | Yes (audio, video) |
| **Reusable File Storage** | No | Files API (Beta) | File API |

---

## Implementation Recommendations

### 1. **Unified Interface Design**

Create a common abstraction for image handling across all providers:

```python
class ImageInput:
    """Unified image input representation"""
    data: bytes
    mime_type: str  # e.g., "image/jpeg"
    detail: Optional[str] = None  # GPT-5 specific
    file_id: Optional[str] = None  # For cached uploads
```

### 2. **Provider-Specific Adapters**

Each LLM provider needs its own adapter to transform the unified format:

- **OpenAI:** Convert to `input_image` content blocks with base64 data URLs
- **Anthropic:** Convert to `image` blocks with base64 source
- **Google:** Support both inline data and File API uploads

### 3. **Image Preprocessing**

Implement common preprocessing pipeline:

```python
def preprocess_image(image_bytes: bytes, provider: str) -> bytes:
    """Resize, compress, and validate images based on provider limits"""
    # 1. Validate format
    # 2. Check size limits
    # 3. Resize if needed
    # 4. Compress if needed
    # 5. Return processed bytes
```

### 4. **Format Support Strategy**

**Core Formats (all providers):**
- JPEG (recommended for photos)
- PNG (recommended for screenshots, diagrams)
- WEBP (good compression, modern)

**Extended Formats (provider-specific):**
- GIF: Supported by all (first frame only for animated)
- PDF, TIFF, RAW: Gemini only

### 5. **Size Limit Handling**

Implement progressive quality reduction:

```python
def ensure_size_limit(image_bytes: bytes, max_size: int) -> bytes:
    """Compress image to fit within size limit"""
    if len(image_bytes) <= max_size:
        return image_bytes
    
    # Progressive JPEG quality reduction
    for quality in [95, 85, 75, 65, 55]:
        compressed = compress_jpeg(image_bytes, quality)
        if len(compressed) <= max_size:
            return compressed
    
    raise ImageTooLargeError(f"Cannot compress to {max_size} bytes")
```

### 6. **Configuration Schema**

```yaml
llm:
  provider: openai
  model: gpt-5
  
  # Image processing config
  image_processing:
    max_file_size: 20971520  # 20MB
    max_resolution: [8000, 8000]
    min_resolution: [640, 480]
    supported_formats: [jpeg, png, webp, gif]
    auto_resize: true
    auto_compress: true
    compression_quality: 85
    
  # Provider-specific options
  openai:
    detail: high  # high or low
  
  anthropic:
    use_files_api: false  # Enable for large/reusable images
  
  google:
    use_file_api_threshold: 10485760  # 10MB - use File API above this
```

### 7. **Error Handling**

Implement comprehensive error handling:

- **Unsupported format:** Convert or reject with clear message
- **File too large:** Auto-compress or reject
- **Resolution issues:** Auto-resize or reject
- **Network errors:** Retry with exponential backoff
- **API errors:** Provider-specific error mapping

### 8. **Capability System**

Track model capabilities (relates to Task 9210):

```python
MODEL_CAPABILITIES = {
    "gpt-5": {
        "tools": True,
        "image_input": True,
        "audio_input": False,
        "video_input": False,
        "function_calling": True,
        "max_image_size": 20 * 1024 * 1024,
        "supported_formats": ["jpeg", "png", "gif", "webp"]
    },
    "claude-sonnet-4.5": {
        "tools": True,
        "image_input": True,
        "audio_input": False,
        "video_input": False,
        "function_calling": True,
        "max_image_size": 30 * 1024 * 1024,
        "supported_formats": ["jpeg", "png", "gif", "webp"]
    },
    "gemini-2.5-pro": {
        "tools": True,
        "image_input": True,
        "audio_input": True,
        "video_input": True,
        "function_calling": True,
        "max_image_size": 50 * 1024 * 1024,
        "supported_formats": ["jpeg", "png", "gif", "webp", "bmp", "pdf", "tiff"]
    }
}
```

---

## Testing Strategy

### Unit Tests
- Image encoding/decoding
- Format validation
- Size limit enforcement
- Compression algorithms
- Provider-specific transformations

### Integration Tests
- Real API calls with test images
- Various formats and sizes
- Error scenarios
- Provider failover

### Performance Tests
- Large image handling
- Compression performance
- Memory usage
- Concurrent uploads

---

## Security Considerations

1. **Input Validation:**
   - Strict MIME type checking
   - Magic number validation (not just extension)
   - File size pre-check before loading into memory

2. **Memory Management:**
   - Stream large files instead of loading entirely
   - Implement memory limits
   - Clean up temporary files

3. **Privacy:**
   - Option to strip EXIF metadata
   - Secure deletion of temporary files
   - No image caching without explicit consent

4. **Rate Limiting:**
   - Per-user upload limits
   - Concurrent upload limits
   - File size quotas

---

## References

- OpenAI GPT-5 Documentation: [docs.aimlapi.com](https://docs.aimlapi.com/api-references/text-models-llm/openai/gpt-5)
- Anthropic Claude Docs: [docs.claude.com](https://docs.claude.com/en/docs/about-claude/models/overview)
- Google Gemini API: [ai.google.dev](https://ai.google.dev/gemini-api/docs/image-understanding)
- Cloud Vision API: [cloud.google.com](https://cloud.google.com/vision/docs/supported-files)

---

**Document Version:** 1.0  
**Last Updated:** October 7, 2025  
**Status:** Complete - Ready for implementation
