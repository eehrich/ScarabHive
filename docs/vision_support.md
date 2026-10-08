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
      capabilities:
        image_input: true
        max_image_size: 20971520   # bytes
        supported_image_formats: [jpeg, jpg, png, gif, webp]
        image_detail_control: true
```

**Key Parameters:**
- `image_input`: Enable/disable vision for this model
- `max_image_size`, `supported_image_formats`, `image_detail_control`: describe the model;
  nothing reads them today (no size or format check, no detail gate)

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

The WebUI checks a file before it sends it: by its MIME type or else its extension (images jpg,
jpeg, png, gif, webp; audio; text files), and at most 20 MB -- otherwise "Unsupported file type"
or "File too large (max 20MB)".

The server sorts an attachment by its extension when the request arrives:
- **Format**: images (jpeg, jpg, jfif, jpe, png, gif, webp, bmp, tiff, tif), audio and text files
  (text is inlined); a file with an unknown extension is skipped
- **Size**: Not checked by the server (no size limit is applied on `/run`)
- **Model**: The model must declare `capabilities.image_input: true`

Refused attachments get HTTP 400 and the reason, e.g.:
- "Model 'X' does not support image_input (1 attachment(s) given)"

## API Usage

### Endpoint: `/run`

Send multimodal requests with text and images using FormData.

**HTTP Method:** `POST`

**Content-Type:** `multipart/form-data`

**Parameters:**
- `task` (form field, required): Text prompt/question
- `files` (file upload, optional): One or more image files

### Example: cURL with Single Image

```bash
curl -X POST http://127.0.0.1:8000/run \
  -F "task=What's in this image?" \
  -F "files=@/path/to/image.jpg"
```

### Example: cURL with Multiple Images

```bash
curl -X POST http://127.0.0.1:8000/run \
  -F "task=Compare these two screenshots" \
  -F "files=@screenshot1.png" \
  -F "files=@screenshot2.png"
```

### Example: Python with `requests`

```python
import requests

url = "http://127.0.0.1:8000/run"

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

const response = await fetch('http://127.0.0.1:8000/run', {
    method: 'POST',
    body: formData
});

const result = await response.text();
console.log(result);
```

### Response Format

**Success (200 OK, `text/event-stream`):** the run's events, e.g.
```
The image shows a bar chart with quarterly sales data...
```

**Error Responses:**

- **400 Bad Request**: Image refused (unsupported by the model or unreadable)
```json
{
  "detail": "Model 'X' does not support image_input (1 attachment(s) given)"
}
```

- **400 Bad Request**: Neither `task` nor files given
```json
{
  "detail": "Missing 'task' in request"
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

Size limits vary by model and are enforced by the provider, not by `/run` (`capabilities.max_image_size` only describes them):
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

**Setting detail level**: per image via `ImageContent.detail` (`auto`, `low`, `high`); it is sent as it is, whatever the model declares.

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
            source=ImageSource(type="url", url="https://example.com/image.jpg")
        )
    ]
)

# Multimodal message with base64 image
msg3 = ChatMessage(
    role="user",
    content=[
        TextContent(text="Analyze this chart"),
        ImageContent(
            source=ImageSource(type="base64", media_type="image/png", data="iVBORw0KG...")
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
      capabilities:
        image_input: true
        max_image_size: 20971520   # bytes
        supported_image_formats: [jpeg, jpg, png, gif, webp]
        image_detail_control: true

      # Other settings
      max_tokens: 128000
      temperature: 1.0
      # ... (see llm_system configuration for full options)
```

### Adding New Vision Models

To enable vision for a new model:

1. Add model configuration to `llm_system:` section:
```yaml
my-custom-vision-model:
  capabilities:
    image_input: true
    max_image_size: 10485760   # bytes
    supported_image_formats: [jpeg, png]
```

2. Update model routing logic if needed (see `src/agent_system/llm/capabilities.py`)

3. Test with sample images via WebUI or API

## Troubleshooting

### Error: "Unsupported file type" (WebUI)

**Cause:** Neither the file's MIME type nor its extension is one of the image, audio or text types
the WebUI knows

**Solution:** 
- Convert image to JPEG or PNG
- Verify file extension matches actual format

### Error: "Image too large"

**Cause:** Image exceeds the provider's size limit

**Solution:**
- Compress image (reduce quality or resize)
- Use online tools like TinyPNG or ImageOptim
- Switch to a model with higher size limits

### Error: "Model '...' does not support image_input"

**Cause:** Selected model has `capabilities.image_input: false`

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
- Keep the number of images per message small

### 4. Error Handling

Always handle potential errors in API requests:

```python
try:
    response = requests.post(url, data=data, files=files)
    response.raise_for_status()
except requests.HTTPError as e:
    if e.response.status_code == 400:
        print(f"Validation error: {e.response.json()['detail']}")
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

- [Configuration](configuration.md)
- [Plugin Authoring Guide](plugin_authoring.md)

## Support

For issues, questions, or feature requests:
1. Check existing tests in `tests/llm/test_llm_models_multimodal.py`
2. Review capability config in `llm_system:` configuration section
3. Check API logs in `logs/api.log`
4. Consult source code in `src/agent_system/llm/`

```
