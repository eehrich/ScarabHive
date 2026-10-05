---
name: web-testing
description: How to check a web page you built or changed in a real headless browser — serve it, open it, read the page and its console, click through the flow, screenshot only for layout. Use when the task touches HTML, CSS or front-end JavaScript, or a bug only shows in the browser.
metadata:
  version: '1.0.0'
---

# Look at the page, not at the code

A page that builds is not a page that works. A missing script, a thrown
error or a button wired to nothing all pass every build — they show in
the browser console and in what the page actually renders.

## The tools

The browser tools are deferred. Load the ones you need by name —
`tool_search` with `select:playwright_browser_navigate,playwright_browser_snapshot,playwright_browser_console_messages,playwright_browser_click`
(add others from the table as you need them). A keyword query returns
only five tools, and `browser` matches every one of them. If the names
are unknown, the `playwright` server is off in this installation; if the
first call fails because the browser cannot be started ("… is not
found"), the host lacks it. Either way say so, with the error, and fall
back to reading the code.

The ones you need:

| Tool | For |
|---|---|
| `playwright_browser_navigate` | open a URL |
| `playwright_browser_snapshot` | the page as an accessibility tree, with a `ref` per element |
| `playwright_browser_click`, `_type`, `_select_option`, `_press_key` | act on an element by its `ref` |
| `playwright_browser_console_messages` | errors and logs (`level: "error"` for errors only) |
| `playwright_browser_network_requests` | failed or missing requests |
| `playwright_browser_take_screenshot` | an image — layout questions only |
| `playwright_browser_close` | end the browser when you are done |

## The loop

1. **Serve it.** The browser opens `http://`, not files. Start the dev
   server — or `python -m http.server <port>` for static files — with
   `coder_shell_execute` and `background: true`, then check its output
   with `coder_shell_get_output` before you navigate. Pick a port no one
   else is likely to use.
2. **Open and read.** Navigate, then take a snapshot. The snapshot is text:
   you see headings, buttons, fields and their state. It answers "is it
   there and what does it say" without an image.
3. **Console next.** Every time. An error there is a finding even when the
   page looks right.
4. **Do what a user does.** Click, type, submit — by `ref` from the latest
   snapshot. Snapshot again after each step; a ref from an old snapshot
   may point at nothing.
5. **Screenshot only when the question is visual** — overlap, spacing,
   colours, a responsive breakpoint (`playwright_browser_resize` first).
   It costs far more than a snapshot, and a model without image input
   gets only the file path. Leave `filename` out: without it the file goes
   to the browser's output directory, with it into the working directory
   of the server — the repository root.
6. **Clean up.** Close the browser and kill the server you started
   (`coder_shell_kill_process`).

## What to report

What you opened, what you did, what the page showed and what the console
said — with the error text. "Verified in the browser" without those is
not a verification.

## One browser

There is one browser per process, shared by whoever calls it. Do not hand
browser checks to sub-agents; do them yourself, one page at a time.
