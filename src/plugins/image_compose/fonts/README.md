# image_compose — Bundled Fonts

The fonts here ship with the plugin, so a render looks the same on every
machine — a server often carries a single family (DejaVu). Each has a
built-in name (`BUNDLED_FONTS` in `compositor.py`) that text layers
(`"font": "serif_display"`) and canvas layers (`"fonts": ["serif_display"]`,
then `ctx.font = '80px "serif_display"'`) both accept; no configuration is
needed. A `font_aliases` entry of the same name in the server config wins.
Every font is from Google Fonts; the licence of each family is in
`licenses/` (SIL Open Font License, Apache 2.0 for Special Elite and
Permanent Marker).

| Name | Font | Character |
|---|---|---|
| `sans` / `sans_bold` | Inter Regular / Bold | neutral sans |
| `sans_black` | Archivo Black | heavy grotesque |
| `geometric` | Montserrat Bold | geometric sans |
| `rounded` | Nunito ExtraBold | soft, friendly |
| `condensed` | Anton | tall condensed caps |
| `serif` / `serif_bold` | Crimson Pro Regular / Bold | book serif |
| `serif_old` | EB Garamond | classical old style |
| `serif_display` | Playfair Display Bold | high-contrast display serif |
| `serif_fat` | Abril Fatface | fat face, poster |
| `roman_caps` | Cinzel Bold | inscriptional capitals |
| `slab` | Alfa Slab One | heavy slab |
| `mono` | JetBrains Mono | monospace |
| `typewriter` | Special Elite | worn typewriter |
| `display` | Bebas Neue | condensed display caps |
| `display_grotesk` | Oswald Bold | condensed grotesque |
| `display_block` | Bungee Inline | inline block letters |
| `stencil` | Stardos Stencil Bold | stencil |
| `art_deco` | Limelight | Art Deco |
| `art_deco_thin` | Poiret One | thin Art Deco |
| `retro` | Righteous | 1970s rounded |
| `neon` | Monoton | multi-line neon |
| `script` | Caveat | handwriting |
| `brush` | Permanent Marker | marker, brush |
| `calligraphy` | Great Vibes | formal script |
| `blackletter` | UnifrakturMaguntia | Fraktur |
| `horror` | Creepster | dripping horror |
| `pixel` | Press Start 2P | 8-bit pixel |

All cover German (ä ö ü ß „“ – €). A new font: put the static TTF and its
licence here, add a name to `BUNDLED_FONTS`; `tests/test_bundled_fonts.py`
checks that every name has its file and every file its licence.
