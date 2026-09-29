# image_compose — Bundled Fonts

This directory holds the TTF/OTF files that map to font-aliases used by
`image_compose_render` (see `LAYER: text` in `schema.yaml`). The fonts are
plugin assets, not user data — they live with the plugin code so the
plugin renders deterministically on any clean checkout.

The compositor (`compositor.py`) consults `font_aliases` first; if the
alias resolves to a file in this directory, that file is used. If the
alias isn't mapped or the file isn't found, the compositor falls back to
platform fonts (`SYSTEM_FONT_FALLBACKS` table), then to PIL's bitmap
default.

## Alias Inventory

Aliases that the cover_artist agent and image_compose schema reference.
Each alias **should** have a TTF here; until it does, the system fallback
is used (so things still render, just less distinctively).

| Alias | Font (Google Fonts, OFL) | Use case |
|---|---|---|
| `sans` | Inter Regular | body / generic |
| `sans_bold` | Inter Bold | bold body, small caps |
| `serif` | Crimson Pro Regular | classical / literary |
| `serif_bold` | Crimson Pro Bold | titles in serif-dominant covers |
| `mono` | JetBrains Mono | technical / system look |
| `display` | Bebas Neue | display caps (current generic display) |
| `display_grotesk` | Oswald Bold | Type-Driven covers — condensed display |
| `script` | Caveat Regular | handwritten / personal / memoir |
| `stencil` | Stardos Stencil Bold | grunge / military / street |
| `display_block` | Bungee Inline | dramatic block display |

## Downloading

All listed fonts are on Google Fonts under the SIL Open Font License.
Drop the matching TTF into this directory and add an entry to
`font_aliases` in `config/plugins.yaml` (or rely on the alias defaults
once those land in the default config).

```yaml
image_compose:
  font_aliases:
    display_grotesk: "Oswald-Bold.ttf"
    script: "Caveat-Regular.ttf"
    stencil: "StardosStencil-Bold.ttf"
    display_block: "BungeeInline-Regular.ttf"
```
