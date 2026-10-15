# ComfyUI workflows -- requirements and notes

What each workflow configured in `config/plugins.yaml` needs on the ComfyUI server: model files, custom nodes,
and the parameters worth knowing. How to add a workflow of your own: `README.md` in this folder.

## Overview

| Workflow ID | File | Category | Needs |
|---|---|---|---|
| `stable_audio` | stable_audio.json | audio | Stable Audio Open 1.0, T5 base |
| `sdxl_txt2img` | sdxl_txt2img.json | image_generation | SDXL base |
| `juggernaut_xl` | juggernaut_xl.json | image_generation | Juggernaut XL v9, 4x-UltraSharp |
| `sdxl_base_refiner` | sdxl_base_refiner.json | image_generation | SDXL base + refiner, 4x-UltraSharp |
| `sdxl_img2img` | sdxl_img2img.json | image_generation | SDXL base + refiner |
| `sdxl_ipadapter` | sdxl_ipadapter.json | image_generation | SDXL base + refiner, IPAdapter Plus |
| `qwenvl_caption` | qwenvl_caption.json | image_describe | a QwenVL custom node, Qwen3-VL-8B-Instruct-FP8 |

Paths below are relative to the ComfyUI installation.

---

## `stable_audio` -- Stable Audio Generation

A sound clip from a text prompt (`duration` in seconds, up to 47).

**Model files:**
- `models/checkpoints/stable-audio-open-1.0.safetensors`
  - Source: `stabilityai/stable-audio-open-1.0` → `model.safetensors` (rename on copy)
- `models/text_encoders/t5-base.safetensors`
  - Source: `stabilityai/stable-audio-open-1.0` → `text_encoder/model.safetensors` (rename on copy)

**Custom nodes:** none.

---

## `sdxl_txt2img` -- SDXL Text-to-Image

A single SDXL pass: the fastest of the image workflows.

**Model files:**
- `models/checkpoints/sd_xl_base_1.0.safetensors`
  - Source: https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0

**Custom nodes:** none.

---

## `juggernaut_xl` -- Juggernaut XL v9 + hires fix

A base render at `width` x `height` (default 1024 x 1536), a 4x model upscale, a scale down to 1536 x 2304 and a
low-denoise refine pass. The scale-down size is fixed in the workflow file (node 12): with another base size the
result is still 1536 x 2304.

**Model files:**
- `models/checkpoints/Juggernaut-XL_v9.safetensors`
- `models/upscale_models/4x-UltraSharp.safetensors`

**Custom nodes:** none.

---

## `sdxl_base_refiner` -- SDXL Base + Refiner

Two passes: the base model lays down the structure, the refiner works on the details; then a hires-fix pass
(4x model upscale, a scale to `hires_width` x `hires_height`, a refine at `hires_denoise`).

**Model files:**
- `models/checkpoints/sd_xl_base_1.0.safetensors`
- `models/checkpoints/sd_xl_refiner_1.0.safetensors`
  - Source: https://huggingface.co/stabilityai/stable-diffusion-xl-refiner-1.0
- `models/upscale_models/4x-UltraSharp.safetensors`

**Custom nodes:** none.

---

## `sdxl_img2img` -- SDXL Image-to-Image

Encodes a reference image (`reference_image`) as the starting latent and denoises from `start_at_step`. The later
it starts, the more of the reference's composition and colours survive.

**`start_at_step`** (with `steps=30`):

| `start_at_step` | Steps that change the picture | Effect |
|---|---|---|
| 4 | 26 of 30 | loose: mostly a new picture |
| 7 | 23 of 30 | balanced (the default) |
| 12 | 18 of 30 | close to the reference |
| 18 | 12 of 30 | little more than a touch-up |

The base model runs from `start_at_step` to step 26, the refiner from 26 to 30.

**The reference image:** pass a local path. The plugin uploads the file to the ComfyUI server that runs the job,
as long as it lies in one of `upload_source_dirs` and has an image extension:

```json
{"operation": "execute", "workflow_id": "sdxl_img2img",
 "parameters": "{\"prompt\": \"a harbour at dawn\", \"reference_image\": \"data/workspace/images/sketch.png\", \"start_at_step\": 7}"}
```

A name that is no local file is passed on as the name of a file already in ComfyUI's input folder
(`operation: upload_image` puts one there, on the primary server only).

**Model files:** SDXL base and refiner, as above.

**Custom nodes:** none (`LoadImage` and `VAEEncode` are built in).

---

## `sdxl_ipadapter` -- SDXL + IP-Adapter (style transfer)

Takes a style embedding from the reference image and adds it to the text prompt as a second conditioning: the
composition is new, the look follows the reference. `reference_image` as for `sdxl_img2img`.

**`weight`:**

| `weight` | Effect |
|---|---|
| 0.5 | a hint of the style |
| 0.7 | a clear match (the default) |
| 0.9 | very close to the reference |

**Model files:**
- SDXL base and refiner, as above
- `models/ipadapter/ip-adapter_sdxl.safetensors`
  - Source: `h94/IP-Adapter` → `sdxl_models/ip-adapter_sdxl.safetensors`
- `models/clip_vision/CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors`
  - Source: `h94/IP-Adapter` → `sdxl_models/image_encoder/model.safetensors` (rename on copy)

With the `hf` CLI (set `COMFYUI` to your ComfyUI folder):

```bash
COMFYUI="/path/to/ComfyUI"

hf download h94/IP-Adapter "sdxl_models/ip-adapter_sdxl.safetensors" --local-dir tmp/hf_dl
cp tmp/hf_dl/sdxl_models/ip-adapter_sdxl.safetensors "$COMFYUI/models/ipadapter/ip-adapter_sdxl.safetensors"

hf download h94/IP-Adapter "sdxl_models/image_encoder/model.safetensors" --local-dir tmp/hf_dl
cp tmp/hf_dl/sdxl_models/image_encoder/model.safetensors \
   "$COMFYUI/models/clip_vision/CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors"
```

**Custom nodes:** **ComfyUI_IPAdapter_plus** by cubiq (ComfyUI Manager: search "IPAdapter Plus", or
`git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus` into `custom_nodes/`). It provides
`IPAdapterModelLoader` and `IPAdapterAdvanced`.

**Troubleshooting:**
- `IPAdapterAdvanced not found` → the custom node is not installed, or ComfyUI was not restarted.
- `ip-adapter_sdxl.safetensors not found` → wrong folder (it must be `models/ipadapter/`).
- `CLIP-ViT ... not found` → wrong folder (`models/clip_vision/`) or wrong file name.

---

## `qwenvl_caption` -- describe an image

Runs a vision-language model over `image` and answers with text: people, objects, setting and anything notable,
as the default `prompt` asks. `image` is a local path as for `sdxl_img2img`. The text comes back in the result's
`outputs.text`.

**Nodes the workflow uses:** `LoadImage` (built in), `AILab_QwenVL` with the model `Qwen3-VL-8B-Instruct-FP8`,
and `TextOutput` -- both from custom node packs that have to be installed on the server.

---

## Windows: KSamplerAdvanced fails with `[Errno 22] Invalid argument`

**Symptom:** every workflow fails in KSamplerAdvanced with `OSError: [Errno 22] Invalid argument`; the traceback
ends in `tqdm → wandb/sdk/lib/console_capture.py → comfyui_manager/prestartup_script.py`.

**Cause:** `wandb` hooks into `sys.stderr`; in the ComfyUI desktop app on Windows that handle can become invalid,
and the write fails.

**Fix:** uninstall `wandb` from ComfyUI's own virtual environment
(`<ComfyUI>\.venv\Scripts\pip.exe uninstall wandb -y`), then end the ComfyUI processes completely -- the desktop
app's Restart leaves its Python backend running -- and start ComfyUI again. `wandb` comes in as a dependency of
some custom nodes; no standard workflow needs it.

---

## Pointing the plugin at a local ComfyUI

For a ComfyUI on the same machine, set the entry's `host` to `127.0.0.1` and `port` to `8188` (and leave
`servers` out, or list only that one). With `servers` set, `host` and `port` are not used.
