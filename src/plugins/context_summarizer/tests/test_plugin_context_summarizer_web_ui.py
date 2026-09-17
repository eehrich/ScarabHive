"""The panel's endpoints and the history the hook keeps for them."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.context_summarizer.plugin import PLUGIN_FACTORY


@pytest.fixture
def plugin():
    return PLUGIN_FACTORY("context_summarizer", AgentSystemConfig(), ToolServerConfig())


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    return TestClient(app)


def record(plugin, session_id, status, **figures):
    plugin.server._hooks_impl._record({'session_id': session_id, 'status': status, 'before_messages': [{'role': 'user'}],
                                       'after_messages': [], **figures})


def test_the_hook_and_the_panel_share_one_history(plugin):
    assert plugin.server._hooks_impl.summarization_history is plugin.web_factory.summarization_history


def test_the_history_keeps_the_newest_runs_of_every_status_with_an_id_each(plugin):
    hooks = plugin.server._hooks_impl
    hooks.MAX_HISTORY = 3
    for status in ('success', 'skipped', 'rejected', 'skipped', 'skipped'):
        record(plugin, 's-1', status)
    history = plugin.server.summarization_history
    assert [event['id'] for event in history] == [3, 4, 5]
    assert all(event['timestamp'].endswith('+00:00') for event in history)


def test_the_history_of_a_session_counts_only_its_applied_runs_into_the_figures(plugin, client):
    record(plugin, 's-1', 'success', tokens_saved=100, messages_summarized=10, reduction_ratio=0.5)
    record(plugin, 's-1', 'rejected', tokens_saved=0, messages_summarized=8, reduction_ratio=-0.2)
    record(plugin, 's-2', 'success', tokens_saved=300, messages_summarized=4, reduction_ratio=0.9)
    record(plugin, 's-1', 'skipped')
    answer = client.get('/plugins/context_summarizer/history', params={'session_id': 's-1'}).json()
    assert [event['id'] for event in answer['events']] == [4, 2, 1]
    assert all('before_messages' not in event and 'after_messages' not in event for event in answer['events'])
    assert answer['stats'] == {'events': 3, 'applied': 1, 'rejected': 1, 'skipped': 1, 'tokens_saved': 100,
                               'messages_summarized': 10, 'average_reduction': 0.5}
    every = client.get('/plugins/context_summarizer/history', params={'limit': 2}).json()
    assert [event['id'] for event in every['events']] == [4, 3]
    assert every['stats']['events'] == 4 and every['stats']['average_reduction'] == pytest.approx(0.7)
    nothing = client.get('/plugins/context_summarizer/history', params={'session_id': 's-9'}).json()
    assert nothing == {'events': [], 'stats': {'events': 0, 'applied': 0, 'rejected': 0, 'skipped': 0, 'tokens_saved': 0,
                                               'messages_summarized': 0, 'average_reduction': None}}


@pytest.mark.parametrize('limit', [0, 1001, 'many'])
def test_a_limit_out_of_range_is_refused(client, limit):
    assert client.get('/plugins/context_summarizer/history', params={'limit': limit}).status_code == 422


def test_a_run_comes_with_its_messages_and_one_gone_is_not_found(plugin, client):
    record(plugin, 's-1', 'success')
    assert client.get('/plugins/context_summarizer/events/1').json()['before_messages'] == [{'role': 'user'}]
    gone = client.get('/plugins/context_summarizer/events/2')
    assert gone.status_code == 404 and gone.json()['detail'] == 'Event 2 is no longer in the history'


def test_clearing_forgets_every_run_and_new_ones_get_new_ids(plugin, client):
    record(plugin, 's-1', 'success')
    record(plugin, 's-2', 'skipped')
    assert client.post('/plugins/context_summarizer/clear').status_code == 200
    assert plugin.server.summarization_history == []
    record(plugin, 's-1', 'skipped')
    assert [event['id'] for event in plugin.server.summarization_history] == [3]


def test_the_panel_page_loads_its_script_and_the_kit(client):
    page = client.get('/plugins/context_summarizer/').text
    assert '/plugins/context_summarizer/static/panel.js' in page and '/static/kit/panel-kit.js' in page
