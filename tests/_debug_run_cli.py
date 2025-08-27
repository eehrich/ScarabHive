import sys, asyncio
from agent_system import cli

class DummyAgent:
    def __init__(self, events):
        self._events = events
    async def run_events(self, task):
        for e in self._events:
            await asyncio.sleep(0)
            yield e
    async def run(self, task):
        result={"task":task,"calls":[]}
        for e in self._events:
            if e.get("type")=="mcp_result":
                result.setdefault("calls",[]).append({"server":e.get("server"),"action":e.get("action"),"result":e.get("result")})
            if e.get("type")=="final":
                result["summary"]=e.get("summary")
        return result

events=[
    {"type":"mcp_call","server":"s","action":"a","params":{"x":1}},
    {"type":"mcp_result","server":"s","action":"a","result":{"ok":True}},
    {"type":"final","summary":"done"},
    {"type":"end"},
]

# monkeypatch Agent in cli
cli.Agent = lambda *a, **k: DummyAgent(events)
# set argv
sys.argv=['agent-cli','--no-stream','--raw','run','do it']
# call main
cli.main()
print('--- EXIT ---')
