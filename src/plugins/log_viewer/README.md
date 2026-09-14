# Log Viewer Plugin

Reads the application's log files: agents get three tools to list, tail and search them, people get the **Logs**
panel. Only the files named in the configuration are ever opened — by the tools and by the panel alike.

## Configuration

```yaml
# config/plugins.yaml
plugins:
  servers:
    log_viewer:
      type: log_viewer
      enabled: true
      log_files:
        - logs/api.log
        - logs/cli.log
        - logs/profiling.log
        - logs/security.log
```

`log_files` is the allowlist, relative to the working directory of the process; without it the four files above are
the default. Tools and panel read the same list. A file's rotations (`api.log.1`,
`api.log.2`, … up to the first gap) belong to it in the panel; the tools read the file itself.

## Tools

| Tool | Parameters | Answer |
|---|---|---|
| `log_viewer_list` | — | `{logs: [{name, size, modified, exists}]}` for every configured file |
| `log_viewer_tail` | `log_file` (a configured name), `lines` (default 500) | `{log_file, lines, total_lines, returned_lines}` |
| `log_viewer_search` | `pattern` (regex, case-insensitive), `log_file` (optional), `max_results` (default 100) | `{pattern, results: [{file, line_number, line}], total_matches, files_searched, truncated}` |

## The panel

Open **Logs** from the panel launcher or the command palette (category *Debug*), or directly at `/plugins/log_viewer/`.

- **File**: the configured files that exist. The status line names how many entries are shown, the size of the file
  and, with rotations, in how many files it lies.
- **Entries**: the newest ones, oldest at the top, each with its time (the full timestamp as tooltip), level and
  message. An entry is a line starting with a timestamp plus the lines after it, so a traceback folds under its first
  line. Understood formats: `TS LEVEL logger message`, `TS LEVEL [logger] message`, `TS - logger - LEVEL - message` and
  `TS | LEVEL | message`; lines at the top of a file without a timestamp before them form an entry of their own. The
  panel's own requests in an access log are left out as long as they are routine (debug, info or no level); a warning
  or error about them is shown.
- **Search** (case-insensitive, over the whole entry including its traceback), **Last 100 … 5000** and the level buttons
  (*Error* includes critical) ask the server anew; with every level on, entries without a level are shown too.
- **Follow** keeps the newest entry in view. Switched off, a refresh keeps the place; a different file or filter
  always starts at the newest entry.
- The toolbar refreshes at once or every five seconds (off at the start) and shows the time of the last update.
- File, filters and Follow are remembered in the browser.
- **No log files** when none of the configured files exists, **No level chosen**, **No entries** (with whether the file
  is empty or nothing matches), and **could not be loaded** with the server's message when a call fails. None of them
  keeps what was shown before.

## Endpoints

| Route | Answer |
|---|---|
| `GET /plugins/log_viewer/` | the panel |
| `GET /plugins/log_viewer/logs/list` | `{logs: [{name, exists, rotation_count, size, modified, rotation_files}]}` — every configured file; `size` and `modified` across its rotations, only for one that exists |
| `GET /plugins/log_viewer/logs/content/{log_name}?lines=500&levels=&search=` | `{entries: [{timestamp, level, message}]}` — the last `lines` (1–5000) entries matching, oldest first; `levels` comma-separated from `debug`, `info`, `warning`, `error`, `critical` (empty: all, entries without a level included); `timestamp` and `level` are `null` when a line has none |

The content route answers **404** for a name that is not configured (whatever else a path names is never opened) or a
configured file that does not exist, **422** for a `lines` out of range or an unknown level, **500** with the reason
when the file cannot be read.

## Tests

`tests/test_plugin_log_viewer_panel.py` drives the panel in headless Chromium against the real plugin and log files in
a temporary directory (`tests/panel_tests.html` holds the checks); `tests/test_plugin_log_viewer.py` covers discovery,
routes, the allowlist, line formats and reading backwards without a browser; `tests/test_log_viewer_rotation.py` the
rotation files.
