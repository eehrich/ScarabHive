from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
import json
import os
import logging
import uvicorn

from ..config.loader import load_config
from ..mcp.base import MCPRegistry
from ..agent.core import Agent
from ..servers.bootstrap import bootstrap_servers
from ..utils.logging import setup_logging


app = FastAPI(title="Agent System (MCP)")


def build_app(config_path: Optional[str] = None) -> FastAPI:
    cfg_path = config_path or str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)

    # Logging: truncate file each start; console INFO+, file per config
    log_file = setup_logging(config.logging.enabled, config.logging.level, config.logging.file)
    if log_file:
        logging.getLogger(__name__).info("Logging initialized, file=%s", log_file)

    # SSL verify off if configured
    if not config.network.ssl_verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")

    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    agent = Agent(config, registry)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/config")
    def get_config():
        return config.model_dump()

    @app.post("/run")
    async def run(task: str):
        logging.getLogger(__name__).info("/run invoked, task=%s", task)
        return await agent.run(task)

    @app.get("/events")
    async def events(task: str):
        logger = logging.getLogger(__name__)
        logger.info("SSE /events connected, task=%s", task)

        async def event_stream():
            # Initial keep-alive line
            yield ":ok\n\n"
            async for ev in agent.run_events(task):
                logger.debug("SSE event: %s", ev.get("type"))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """
<!doctype html>
<html>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <title>Agent System (MCP)</title>
  <style>
    :root{ color-scheme:dark; }
    *{ box-sizing:border-box }
    body{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, Noto Sans, 'Apple Color Emoji', 'Segoe UI Emoji'; background:#0b0f14; color:#e6edf3; }
    header{ position:sticky; top:0; background:linear-gradient(180deg,#0b0f14,#0b0f14cc 70%,transparent); padding:16px 24px; border-bottom:1px solid #1f2937; backdrop-filter: blur(6px); z-index:10 }
    h1{ margin:0; font-size:18px; letter-spacing:.2px; color:#cbd5e1 }
    main{ max-width:980px; margin:0 auto; padding:16px; }
    .chat{ display:flex; flex-direction:column; gap:12px; padding-bottom:100px }
    .row{ display:flex; gap:12px; align-items:flex-start }
    .msg{ max-width:80%; padding:12px 14px; border-radius:14px; line-height:1.45; box-shadow:0 1px 0 #0008 inset, 0 0 0 1px #ffffff12 inset }
    .user{ margin-left:auto; background:#1f2937; border:1px solid #334155 }
    .assistant{ background:#0f172a; border:1px solid #243244 }
    .inputBar{ position:fixed; bottom:0; left:0; right:0; padding:12px 16px; background:linear-gradient(180deg,transparent,#0b0f14 30%); border-top:1px solid #1f2937; }
    form{ display:flex; gap:8px; max-width:980px; margin:0 auto }
    input[type=text]{ flex:1; background:#0f172a; color:#e6edf3; border:1px solid #334155; border-radius:10px; padding:12px 14px; outline:none }
    input[type=text]::placeholder{ color:#64748b }
    button{ background:#2563eb; color:white; border:none; padding:10px 14px; border-radius:10px; cursor:pointer }
    details{ background:#0b1220; border:1px solid #243244; border-radius:12px; padding:10px 12px; }
    summary{ cursor:pointer; color:#9ab; }
    pre{ margin:8px 0 0; white-space:pre-wrap; word-break:break-word }
    code, pre{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace; font-size:13px }
  </style>
</head>
<body>
  <header>
    <h1>Agent System <span style='display:inline-block;padding:2px 8px;border-radius:999px;background:#0b1220;border:1px solid #243244;color:#9ab;font-size:12px;'>MCP</span></h1>
  </header>
  <main>
    <div id='chat' class='chat'></div>
  </main>
  <div class='inputBar'>
    <form id='f'>
      <input id='task' type='text' placeholder='Ask the agent…' autocomplete='on' />
      <button id='runBtn' type='submit'>Run</button>
    </form>
  </div>
  <script>
    const chat = document.getElementById('chat');
    const form = document.getElementById('f');
    const runBtn = document.getElementById('runBtn');

    function addUser(text){
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `<div class=\"msg user\">${escapeHtml(text)}</div>`;
      chat.appendChild(row); scrollBottom();
    }
    function addAssistantBlock(){
      const row = document.createElement('div'); row.className = 'row';
      const box = document.createElement('div'); box.className='msg assistant';
      box.innerHTML = `<div id=\"assistantText\"></div>
        <details id=\"thinkingBox\"><summary>Thinking…</summary><pre id=\"thinking\"></pre></details>
        <details id=\"mcpBox\"><summary>MCP Calls</summary><pre id=\"mcp\"></pre></details>`;
      row.appendChild(box); chat.appendChild(row); scrollBottom();
      return {row, box,
        t: box.querySelector('#assistantText'),
        think: box.querySelector('#thinking'),
        mcp: box.querySelector('#mcp'),
        thinkBox: box.querySelector('#thinkingBox'),
        mcpBox: box.querySelector('#mcpBox')};
    }
    function scrollBottom(){ requestAnimationFrame(()=>{ window.scrollTo({top:document.body.scrollHeight, behavior:'smooth'}); }); }
    function escapeHtml(s){ return s.replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

    form.addEventListener('submit', (e)=>{ e.preventDefault(); run(); });

    async function run(){
      const task = document.getElementById('task').value.trim(); if(!task) return;
      addUser(task); document.getElementById('task').value='';
      const blk = addAssistantBlock(); runBtn.disabled = true;
      let sseOk = false;
      const es = new EventSource(`/events?task=${encodeURIComponent(task)}`);
      es.onopen = ()=>{ console.log('SSE connected'); sseOk = true; };
      let summary = '';
      es.onmessage = (ev)=>{
        try{
          const data = JSON.parse(ev.data);
          sseOk = true;
          switch(data.type){
            case 'thinking': blk.think.textContent += JSON.stringify(data.assistant, null, 2) + "\n"; blk.thinkBox.open = true; break;
            case 'mcp_call': blk.mcp.textContent += `> ${data.server}.${data.action} ${JSON.stringify(data.params)}\n`; blk.mcpBox.open = true; break;
            case 'mcp_result': blk.mcp.textContent += JSON.stringify(data.result, null, 2) + "\n\n"; break;
            case 'final': summary = data.summary || ''; blk.t.textContent = summary; blk.thinkBox.open = false; break;
            case 'end': es.close(); runBtn.disabled = false; break;
            case 'error': blk.t.textContent = (summary || '') + `\nError: ${data.message}`; break;
          }
          scrollBottom();
        }catch{}
      };
      setTimeout(async ()=>{
        if(!sseOk){
          try{
            const r = await fetch('/run?task='+encodeURIComponent(task), {method:'POST'});
            const j = await r.json();
            blk.t.textContent = j.summary || JSON.stringify(j, null, 2);
          }catch(e){ blk.t.textContent = 'Request failed: '+e; }
          runBtn.disabled = false; es.close();
        }
      }, 1500);
      es.onerror = ()=>{ es.close(); runBtn.disabled=false; };
    }
  </script>
</body>
</html>
"""

    return app


def run() -> None:
    build_app()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
