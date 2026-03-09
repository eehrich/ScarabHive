# ComfyUI Workflows — Requirements & Notes

## Overview

| Workflow ID | File | Category | Dependencies |
|---|---|---|---|
| `stable_audio` | stable_audio.json | audio | Stable Audio Open 1.0 |
| `sdxl_txt2img` | sdxl_txt2img.json | image_generation | SDXL Base |
| `sdxl_base_refiner` | sdxl_base_refiner.json | image_generation | SDXL Base + Refiner |
| `sdxl_img2img` | sdxl_img2img.json | image_generation | SDXL Base + Refiner |
| `sdxl_ipadapter` | sdxl_ipadapter.json | image_generation | SDXL Base + Refiner + IPAdapter-Plus |

---

## Workflow Details

### `stable_audio` — Stable Audio Generation
**Purpose:** Generate ambient/background audio clips.

**Model files required:**
- `ComfyUI/models/checkpoints/stable_audio/stable_audio_open_1_0.safetensors`
  - Download: https://huggingface.co/stabilityai/stable-audio-open-1.0

**Custom nodes:** None

---

### `sdxl_txt2img` — SDXL Text-to-Image
**Purpose:** Simple single-pass SDXL image generation (faster, slightly lower quality).

**Model files required:**
- `ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors`
  - Download: https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0

**Custom nodes:** None (built-in ComfyUI nodes only)

---

### `sdxl_base_refiner` — SDXL Base + Refiner (High Quality) ⭐ Default
**Purpose:** Two-pass SDXL: base model generates structure (steps 0–26), refiner polishes details
(steps 26–30). Significantly fewer artifacts and deformations than single-pass.

**Model files required:**
- `ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors`
- `ComfyUI/models/checkpoints/sd_xl_refiner_1.0.safetensors`
  - Download: https://huggingface.co/stabilityai/stable-diffusion-xl-refiner-1.0

**Custom nodes:** None (built-in ComfyUI nodes only)

---

### `sdxl_img2img` — SDXL Image-to-Image (Style Continuation)
**Purpose:** Encode a reference image as the starting latent and denoise from a given step.
Preserves composition/colour palette of the reference proportional to `start_at_step`.
Designed for **series cover continuation** — gives structural and colour continuity with a
predecessor cover.

**Denoise control via `start_at_step`** (for `steps=30`):
| `start_at_step` | Effective denoise | Effect |
|---|---|---|
| 4 | ~0.87 | Very close to reference structure |
| 7 | ~0.75 | Balanced (recommended default) |
| 12 | ~0.60 | Loose reference, more creative |
| 18 | ~0.40 | Just colour palette hints |

**Workflow — before executing:**
```python
# 1. Upload reference cover to ComfyUI input folder
upload_result = comfyui_workflow(operation="upload_image",
                                  file_path="data/writer/covers/book_X.webp")
filename = upload_result["filename"]  # e.g. "book_40.webp"

# 2. Run img2img
comfyui_workflow(operation="execute", workflow_id="sdxl_img2img", parameters={
    "reference_image": filename,
    "start_at_step": 7,   # 0.75 denoise
    ...
})
```

**Model files required:**
- `ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors`
- `ComfyUI/models/checkpoints/sd_xl_refiner_1.0.safetensors`

**Custom nodes:** None (built-in `VAEEncode` + `LoadImage`)

---

### `sdxl_ipadapter` — SDXL + IP-Adapter (Style Transfer) ⭐ Best for Series
**Purpose:** Extracts a style/mood/colour embedding from the reference image and injects it as
an additional conditioning signal alongside the text prompt. The composition is completely new
(txt2img style), but the visual aesthetic matches the reference. Best choice for series covers.

**Weight control via `weight`:**
| `weight` | Effect |
|---|---|
| 0.5 | Subtle style hint |
| 0.7 | Clear style match (recommended) |
| 0.9 | Very close to reference aesthetic |

**Workflow — before executing:**
```python
# Same upload step as img2img:
upload_result = comfyui_workflow(operation="upload_image",
                                  file_path="data/writer/covers/book_X.webp")
filename = upload_result["filename"]

comfyui_workflow(operation="execute", workflow_id="sdxl_ipadapter", parameters={
    "reference_image": filename,
    "weight": 0.7,
    ...
})
```

**Model files required:**
- `ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors`
- `ComfyUI/models/checkpoints/sd_xl_refiner_1.0.safetensors`
- `ComfyUI/models/ipadapter/ip-adapter_sdxl.safetensors` (~671 MB)
  - Source: `h94/IP-Adapter` → `sdxl_models/ip-adapter_sdxl.safetensors`
  - Alternative: `ip-adapter_sdxl_vit-h.safetensors` → then also change CLIP Vision below
- `ComfyUI/models/clip_vision/CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors` (~3.5 GB)
  - Source: `h94/IP-Adapter` → `sdxl_models/image_encoder/model.safetensors` (rename on copy!)
  - For `ip-adapter_sdxl_vit-h`: use `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` instead
    (Source: `h94/IP-Adapter` → `models/image_encoder/model.safetensors`)

**Download via `hf` CLI** (Windows, ComfyUI at `C:\Users\...\Documents\ComfyUI`):
```bash
# Set COMFYUI path
COMFYUI="C:/Users/enric/Documents/ComfyUI"

# 1. IPAdapter model
hf download h94/IP-Adapter "sdxl_models/ip-adapter_sdxl.safetensors" --local-dir tmp/hf_dl
cp tmp/hf_dl/sdxl_models/ip-adapter_sdxl.safetensors  "$COMFYUI/models/ipadapter/ip-adapter_sdxl.safetensors"

# 2. CLIP Vision (downloaded as model.safetensors → rename)
hf download h94/IP-Adapter "sdxl_models/image_encoder/model.safetensors" --local-dir tmp/hf_dl
cp tmp/hf_dl/sdxl_models/image_encoder/model.safetensors \
   "$COMFYUI/models/clip_vision/CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors"
```

**Custom nodes required:**
- **ComfyUI-IPAdapter-Plus** by cubiq
  - Install via ComfyUI Manager: search "IPAdapter Plus"
  - Or manually: `cd ComfyUI/custom_nodes && git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus`
  - Provides: `IPAdapterModelLoader`, `CLIPVisionLoader`, `IPAdapterAdvanced`

**Installation checklist:**
```
ComfyUI/
├── custom_nodes/
│   └── ComfyUI_IPAdapter_plus/    ← git clone here
├── models/
│   ├── checkpoints/
│   │   ├── sd_xl_base_1.0.safetensors
│   │   └── sd_xl_refiner_1.0.safetensors
│   ├── ipadapter/
│   │   └── ip-adapter_sdxl.safetensors
│   └── clip_vision/
│       └── CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors
```

**Troubleshooting:**
- Error `IPAdapterAdvanced not found` → custom node not installed/restarted
- Error `ip-adapter_sdxl.safetensors not found` → wrong folder (must be `models/ipadapter/`, not `models/`)
- Error `CLIP-ViT not found` → wrong folder (`models/clip_vision/`) or wrong filename

---

## Windows-Only: KSamplerAdvanced `[Errno 22] Invalid argument`

**Symptom:** Every workflow fails in KSamplerAdvanced with `OSError: [Errno 22] Invalid argument`.
The traceback ends in `tqdm → wandb/sdk/lib/console_capture.py → comfyui_manager/prestartup_script.py`.

**Cause:** `wandb` hooks into `sys.stderr` via `console_capture.py`. On Windows the ComfyUI
stderr handle becomes invalid after the Electron shell redirects it, causing the write call to
fail. Not an issue on Linux (stderr always has a valid file descriptor).

**Fix:**
```bash
# Uninstall wandb from ComfyUI's venv
"C:\Users\<user>\Documents\ComfyUI\.venv\Scripts\pip.exe" uninstall wandb -y

# Then fully kill ALL ComfyUI/Python processes (the Desktop App's "Restart" button
# does NOT kill the Python backend — use Task Manager or:
taskkill /F /IM python.exe /T
taskkill /F /IM ComfyUI.exe /T

# Start ComfyUI fresh
```

**Note:** `wandb` is not required by ComfyUI or any standard workflow. It is installed as a
transitive dependency of some custom nodes. Removing it has no impact on functionality.

---

## Series Cover Strategy

The `cover_artist` agent uses this fallback chain for series follow-up books
(`series_id ≠ ""` AND `predecessor_book_id ≠ ""`):

```
1. sdxl_ipadapter  (best: new composition, same style)
        ↓ fallback if IPAdapter node not installed
2. sdxl_img2img    (good: composition influenced by predecessor)
        ↓ fallback if no predecessor cover found
3. sdxl_base_refiner  (standard: standalone txt2img)
```

---

## Testing Against localhost

The ComfyUI plugin is configured with `host: 192.0.2.125` in `config/plugins.yaml`.
For local testing without affecting the production ComfyUI instance, override via env or
test config:

```yaml
# test override
comfyui:
  host: "127.0.0.1"
  port: 8188
```

Or run ComfyUI locally on port 8188 and the plugin will connect automatically.
