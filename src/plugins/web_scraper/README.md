# Web scraper

Lets an agent read the web: a URL becomes clean, paged text or a list of links, and files a page cannot show as
text -- PDFs, images, archives -- are saved into a directory you allow. Every URL and every redirect hop is checked
first, so an agent cannot be steered into your own machine or internal network.

- **Tool** `web_scraper_page` -- a page's readable text (paged with `offset`, tables and lists on request) or its
  links. Pages are cached per session; bot walls and non-text files come back as errors, not as text.
- **Tool** `web_scraper_download` -- saves what a URL serves to a file inside `allowed_directories`, with size and
  sha256. Offered only when a directory is configured.

Enable it in `config/plugins.yaml` (`web_scraper: {type: web_scraper, enabled: true}`, plus `allowed_directories`
for downloads) and allow `+web_scraper/*` in an agent's tool list.

The full manual -- every parameter and answer, the address checks and their limits, size and time limits, what
the model sees of a page, and the server settings -- is the plugin's guide, `web_scraper.guide`, in the Help panel.
