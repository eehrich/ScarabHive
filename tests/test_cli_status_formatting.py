import sys
import asyncio
from agent_system.mcp import status as status_mod


def test_cli_status_single_event(capsys, monkeypatch):
    # Monkeypatch subscribe to inject an event immediately after subscription.
    orig_sub = status_mod.status_bus.subscribe

    async def fake_subscribe(*a, **kw):  # type: ignore
        q = await orig_sub(*a, **kw)
        async def later():
            await status_mod.publish_status('cli_test', 'working https://example.com', phase='progress')
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(later())
        except RuntimeError:
            asyncio.run(later())
        return q

    monkeypatch.setattr(status_mod.status_bus, 'subscribe', fake_subscribe)  # type: ignore

    import agent_system.cli as cli
    argv_backup = sys.argv
    try:
        sys.argv = [argv_backup[0], 'status', '--server', 'cli_test']
        cli.main()
    finally:
        sys.argv = argv_backup

    captured = capsys.readouterr().out
    assert 'cli_test' in captured
    assert 'progress' in captured
    assert 'https://example.com' in captured
    assert '|' in captured
