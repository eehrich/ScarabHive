# Vision Support Documentation

## Overview

The AgentSystem now supports **multimodal conversations** with vision-capable LLMs, allowing you to include images alongside text in your prompts. This enables use cases like:

- Image analysis and description
- Visual question answering
- Document/screenshot analysis
- Object detection and recognition
- Chart/graph interpretation
- OCR and text extraction from images

## Supported Models

Vision capabilities are configured in the `llm_system:` configuration section. Currently supported vision models include:

- **GPT-5** (OpenAI): Supports JPEG, PNG, GIF, WebP images up to 20MB
- **GPT-4.1** (OpenAI): Supports JPEG, PNG, GIF, WebP images up to 20MB
- **GPT-5-mini** (OpenAI): Supports JPEG, PNG, GIF, WebP images up to 5MB
- **Ollama models** (Local): Check individual model capabilities in config

### Model Capability Configuration

Each model's vision capabilities are defined in the `llm_system:` section:

```yaml
llm_system:
  models:
    gpt-5:
      vision_support: true
      max_image_size_mb: 20
      supported_image_formats: [jpeg, jpg, png, gif, webp]
      max_images_per_message: 10
      image_detail_levels: [auto, low, high]
```

**Key Parameters:**
- `vision_support`: Enable/disable vision for this model
- `max_image_size_mb`: Maximum file size per image (in megabytes)
- `supported_image_formats`: Allowed image formats (lowercase)
- `max_images_per_message`: Maximum number of images in a single message
- `image_detail_levels`: Available detail modes:
  - `auto`: Let model choose optimal detail level
  - `low`: Faster, less detailed analysis (uses 85 tokens)
  - `high`: Detailed analysis (uses 129 tokens + scaled by image size)

## WebUI Usage

### Uploading Images

1. **Navigate to the WebUI** (usually `http://127.0.0.1:8000`)

2. **Upload images using any of these methods:**

   - **Drag & Drop**: Drag image files from your file explorer into the upload area
   - **Click to Browse**: Click the "Choose images..." button and select files
   - **Paste**: Copy an image to clipboard (Ctrl+C) and paste (Ctrl+V) in the upload area

3. **Preview**: Uploaded images appear with thumbnails. You can:
   - View each image preview
   - Remove individual images by clicking the ❌ button
   - See file names and sizes

4. **Send**: Type your question/prompt in the text area and click **Send** (or press Ctrl+Enter)

### Example Workflow

```
1. Upload screenshot.png (drag & drop)
2. Type: "What error is shown in this screenshot?"
3. Click Send
4. Agent analyzes image and responds with error details
```

### Image Validation

The WebUI validates images before sending:
- **Format**: Must be a supported image format (JPEG, PNG, GIF, WebP)
- **Size**: Must not exceed model's `max_image_size_mb` limit
- **Count**: Cannot exceed `max_images_per_message` limit

Invalid files trigger error messages:
- "Invalid image format. Supported: jpeg, png, gif, webp"
- "Image too large (25.3 MB). Maximum: 20 MB"
- "Too many images (12). Maximum: 10"

## API Usage

### Endpoint: `/run/multimodal`

Send multimodal requests with text and images using FormData.

**HTTP Method:** `POST`

**Content-Type:** `multipart/form-data`

**Parameters:**
- `task` (form field, required): Text prompt/question
- `files` (file upload, optional): One or more image files

### Example: cURL with Single Image

```bash
curl -X POST http://127.0.0.1:8000/run/multimodal \
  -F "task=What's in this image?" \
  -F "files=@/path/to/image.jpg"
```

### Example: cURL with Multiple Images

```bash
curl -X POST http://127.0.0.1:8000/run/multimodal \
  -F "task=Compare these two screenshots" \
  -F "files=@screenshot1.png" \
  -F "files=@screenshot2.png"
```

### Example: Python with `requests`

```python
import requests

url = "http://127.0.0.1:8000/run/multimodal"

# Single image
with open("diagram.png", "rb") as img:
    response = requests.post(
        url,
        data={"task": "Explain this architecture diagram"},
        files={"files": img}
    )
    print(response.text)

# Multiple images
with open("before.jpg", "rb") as img1, open("after.jpg", "rb") as img2:
    response = requests.post(
        url,
        data={"task": "What changed between these images?"},
        files=[
            ("files", img1),
            ("files", img2)
        ]
    )
    print(response.text)
```

### Example: JavaScript/Fetch

```javascript
const formData = new FormData();
formData.append('task', 'Describe this chart');

// Add image from file input
const fileInput = document.querySelector('input[type="file"]');
formData.append('files', fileInput.files[0]);

const response = await fetch('http://127.0.0.1:8000/run/multimodal', {
    method: 'POST',
    body: formData
});

const result = await response.text();
console.log(result);
```

### Response Format

**Success (200 OK):**
```
The image shows a bar chart with quarterly sales data...
```

**Error Responses:**

- **400 Bad Request**: Invalid image format or validation error
```json
{
  "detail": "Image format 'tiff' not supported. Supported formats: jpeg, png, gif, webp"
}
```

- **413 Payload Too Large**: Image exceeds size limit
```json
{
  "detail": "Image 'large.jpg' size (25.5 MB) exceeds maximum 20 MB"
}
```

- **422 Unprocessable Entity**: Missing required parameters
```json
{
  "detail": [
    {
      "loc": ["body", "task"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

## Image Processing Details

### Image Encoding

Images are automatically:
1. **Validated** for format and size
2. **Encoded** to base64 for transmission to LLM APIs
3. **Included** in message content alongside text

### Supported Formats

Standard web image formats are supported:
- **JPEG** (.jpg, .jpeg)
- **PNG** (.png)
- **GIF** (.gif) - first frame used for static analysis
- **WebP** (.webp)

**Note:** TIFF, BMP, and other formats are NOT supported by most vision APIs.

### Size Limits

Size limits vary by model (configured in `llm_system:` section):
- **GPT-5**: 20 MB per image
- **GPT-5-mini**: 5 MB per image
- **Custom models**: Check config

**Best practices:**
- Optimize images before upload (compress JPEGs, use PNG for screenshots)
- Resize large images to 2000x2000px or smaller
- Keep images under 2 MB when possible for faster processing

### Detail Levels (OpenAI Models)

OpenAI vision models support detail level control:

- **`auto`** (default): Model chooses based on image complexity
- **`low`**: Faster, uses 85 tokens regardless of image size
- **`high`**: Detailed analysis, higher token cost (129 base + scaled)

**Setting detail level** (currently applies to all images):
Configure in `llm_system:` section under model's `default_image_detail` parameter.

## Message Structure

### Internal Representation

Multimodal messages use a structured content format:

```python
from agent_system.llm.models import ChatMessage, TextContent, ImageContent, ImageSource

# Text-only message (backward compatible)
msg1 = ChatMessage(
    role="user",
    content="Hello"
)

# Multimodal message with image URL
msg2 = ChatMessage(
    role="user",
    content=[
        TextContent(text="What's in this image?"),
        ImageContent(
            image_url=ImageSource(url="https://example.com/image.jpg")
        )
    ]
)

# Multimodal message with base64 image
msg3 = ChatMessage(
    role="user",
    content=[
        TextContent(text="Analyze this chart"),
        ImageContent(
            image_url=ImageSource(url="data:image/png;base64,iVBORw0KG...")
        )
    ]
)
```

### Utility Methods

```python
message = ChatMessage(role="user", content=[...])

# Check if message contains images
if message.is_multimodal():
    print(f"Message has {message.count_images()} images")

# Extract text content
text = message.get_text_content()

# Check for images
if message.has_images():
    print("Processing vision request...")
```

## Configuration Reference

### Model Capabilities

Configure in `llm_system:` section:

```yaml
llm_system:
  models:
    gpt-5:
      # Vision support
      vision_support: true
      max_image_size_mb: 20
      supported_image_formats: [jpeg, jpg, png, gif, webp]
      max_images_per_message: 10
      image_detail_levels: [auto, low, high]
      default_image_detail: auto
      
      # Other capabilities
      text_generation: true
      max_tokens: 128000
      temperature: 1.0
      # ... (see llm_system configuration for full options)
```

### Adding New Vision Models

To enable vision for a new model:

1. Add model configuration to `llm_system:` section:
```yaml
my-custom-vision-model:
  vision_support: true
  max_image_size_mb: 10
  supported_image_formats: [jpeg, png]
  max_images_per_message: 5
  image_detail_levels: [auto]
```

2. Update model routing logic if needed (see `src/agent_system/llm/capabilities.py`)

3. Test with sample images via WebUI or API

## Troubleshooting

### Error: "Image format 'X' not supported"

**Cause:** Image format not in model's `supported_image_formats` list

**Solution:** 
- Convert image to JPEG or PNG
- Check model config in `llm_system:` configuration section
- Verify file extension matches actual format

### Error: "Image too large"

**Cause:** Image exceeds `max_image_size_mb` limit

**Solution:**
- Compress image (reduce quality or resize)
- Use online tools like TinyPNG or ImageOptim
- Switch to a model with higher size limits

### Error: "Model does not support vision"

**Cause:** Selected model has `vision_support: false`

**Solution:**
- Use a vision-capable model (GPT-5, GPT-4.1, etc.)
- Enable vision in model config in `llm_system:` section if supported
- Check API key has access to vision models

### Images not displaying in WebUI

**Cause:** JavaScript errors or browser compatibility

**Solution:**
- Check browser console for errors (F12)
- Ensure JavaScript is enabled
- Clear browser cache and reload
- Try different browser (Chrome, Firefox, Edge)

### Slow response times

**Cause:** Large images or high detail settings

**Solution:**
- Resize images to 1024x1024 or smaller
- Use `low` detail level for quick analysis
- Reduce number of images per request
- Compress images before upload

## Best Practices

### 1. Optimize Images

- **Resize** large images to 2000px max dimension
- **Compress** JPEGs to 80-90% quality
- **Use PNG** only for screenshots/text-heavy images
- **Convert** non-web formats (TIFF, BMP) to JPEG/PNG

### 2. Clear Prompts

- **Be specific**: "Identify the error message in this screenshot"
- **Reference images**: "In the left image vs right image..."
- **Ask single questions**: Avoid combining multiple analysis tasks

### 3. Batch Processing

- Send multiple related images together
- Use clear numbering: "Image 1 shows..., Image 2 shows..."
- Stay within `max_images_per_message` limit

### 4. Error Handling

Always handle potential errors in API requests:

```python
try:
    response = requests.post(url, data=data, files=files)
    response.raise_for_status()
except requests.HTTPError as e:
    if e.response.status_code == 400:
        print(f"Validation error: {e.response.json()['detail']}")
    elif e.response.status_code == 413:
        print("Image too large")
```

### 5. Token Costs

- Vision requests use more tokens than text-only
- High-detail images cost more tokens
- Monitor token usage in logs (`logs/api.log`)
- Use `low` detail for preliminary analysis

## Examples

### 1. Screenshot Analysis

**Prompt:** "What error is displayed in this terminal screenshot?"

**Use Case:** Debugging, error investigation

**Best Image:** PNG screenshot at native resolution

### 2. Document OCR

**Prompt:** "Extract all text from this document image"

**Use Case:** Digitizing paper documents, form processing

**Best Image:** High-contrast scan, JPEG/PNG, 300+ DPI

### 3. Chart Interpretation

**Prompt:** "Summarize the trends shown in this sales chart"

**Use Case:** Data analysis, report generation

**Best Image:** Clear chart with readable labels, PNG for line art

### 4. Object Detection

**Prompt:** "List all objects visible in this room photo"

**Use Case:** Inventory, scene understanding

**Best Image:** Well-lit photo, JPEG, standard resolution

### 5. Comparison

**Prompt:** "What are the differences between these two UI designs?"

**Use Case:** A/B testing, design review

**Best Image:** Side-by-side screenshots, PNG for UI elements

## Advanced Usage

### Custom Detail Levels (Future)

Currently, detail level applies globally via config. Future enhancements may support per-image detail:

```python
# Planned API (not yet implemented)
ImageContent(
    image_url=ImageSource(url="..."),
    detail="high"  # Override default
)
```

### Audio/Video Support (Future)

The message structure supports audio and video content types:

```python
from agent_system.llm.models import AudioContent, VideoContent

# Audio message (when models support it)
AudioContent(audio_url="...")

# Video message (when models support it)
VideoContent(video_url="...")
```

These are prepared for future model capabilities but not yet functional.

## See Also

- [LLM Configuration Guide](server_configuration.md#llm-configuration)
- [API Design Documentation](plugin_web_api_design.md)
- [Vision Research Document](vision_llm_research.md)
- [Plugin Authoring Guide](plugin_authoring.md)

## Support

For issues, questions, or feature requests:
1. Check existing tests in `tests/test_llm_models_multimodal.py`
2. Review capability config in `llm_system:` configuration section
3. Check API logs in `logs/api.log`
4. Consult source code in `src/agent_system/llm/`

```
