"""No test captures into the viewer's own stores: the message debugger and the usage tracker.

An agent a test builds from config/config.yaml loads the real plugins on its first run, their capture hooks
among them. The root conftest gives these stores a directory of the run; this checks it the way an agent of the
test process sees it -- each plugin built from its entry in the real config, by the class the registry builds,
with the ``data_path`` its module bound at import. A path the entry sets itself, wherever it points, would bypass
the conftest and turns this red. A test that starts an agent in a process of its own is out of this reach.
"""
from agent_system import paths
from agent_system.config.settings import load_settings
from agent_system.paths import PROJECT_ROOT


def _server(name):
    server = load_settings(str(PROJECT_ROOT / "config" / "config.yaml")).plugins.servers[name]
    assert server.enabled, f"{name} is off in the real config: this guard would guard nothing"
    return server


def _in_the_directory_of_the_run(opened, store):
    opened = (PROJECT_ROOT / opened).resolve()
    real = (PROJECT_ROOT / paths.data_path()).resolve()  # the data directory, wherever it is configured
    run = paths.data_path(store).parent.resolve()  # where the conftest sends this store
    assert run != real, "the conftest sends the capture stores nowhere else"
    assert run in opened.parents, f"a test run captures into {opened}"


def test_the_message_debugger_opens_its_database_in_the_directory_of_the_run():
    from plugins.message_debugger.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("message_debugger", None, _server("message_debugger"))
    try:
        opened = plugin._db.db_path
    finally:
        plugin._db.close()
    assert str(opened).endswith("debugger.db"), opened
    _in_the_directory_of_the_run(opened, "message_debugger")


def test_the_usage_tracker_keeps_its_database_in_the_directory_of_the_run():
    from plugins.context_usage_tracker.plugin import PLUGIN_FACTORY

    # the tracker opens its database on first use; where it will is fixed at construction
    plugin = PLUGIN_FACTORY("context_usage_tracker", None, _server("context_usage_tracker"))
    assert plugin.tracker.db_path.name == "usage.db", plugin.tracker.db_path
    _in_the_directory_of_the_run(plugin.tracker.db_path, "context_usage_tracker.json")
