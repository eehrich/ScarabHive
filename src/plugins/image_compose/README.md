# Image composition

Lets agents build a picture from layers described in JSON -- background, images, text, rectangles, gradients,
SVG, a vignette and `canvas` layers a script draws with the browser's canvas 2D API (icons, illustrations,
patterns, seamless textures, pixel art) -- and write it as PNG, WEBP or JPEG with every pixel where the spec
puts it. Two measuring
tools tell how bright and busy an area is and find the calmest place for a block of text.

- **Tools** `render`, `analyze`, `find_region` (prefixed with the instance name). An optional
  `output_directories` list confines where an instance may write.
- **Agent** `image_agent`: makes textures, sprites, icons, illustrations and UI elements -- drawn with a canvas
  script where a drawing can do it, generated with ComfyUI where not -- with these tools,
  writing only into `data/workspace/images`.
- No hooks, no panel.

Enable it in `config/plugins.yaml` (`image_compose: {type: image_compose, enabled: true}`) and allow
`+image_compose/*` in an agent's tool list. SVG layers need svglib/reportlab, cutouts need rembg (both
installed with the plugin); drawing an SVG also needs reportlab's cairo backend, an optional part of the
install (`requirements/optional.txt`, built against cairo and pkg-config -- install.sh sets it up where it
can). Canvas layers need a Chromium on the server (Edge, Chrome or Chromium; `browser` and `browser_args` in
the server's config, `["--no-sandbox"]` for a container running as root). Font files go into `fonts/` (see its
README).

The full manual -- every layer field and answer, the path rules and size limits, the image agent, what the
model sees and the server settings -- is the plugin's guide, `image_compose.guide`, in the Help panel.
