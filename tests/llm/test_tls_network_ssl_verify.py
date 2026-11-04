"""Plugins read network.ssl_verify through one helper.

web_scraper and weather read ``system_config.ssl_verify`` -- a key that does
not exist -- so ``network.ssl_verify: false`` behind a TLS proxy did nothing.
"""
from types import SimpleNamespace

from agent_system.config.models import AgentSystemConfig
from agent_system.llm.tls import network_ssl_verify


def test_the_network_setting_is_read():
    config = AgentSystemConfig()
    config.network.ssl_verify = False
    assert network_ssl_verify(config) is False
    config.network.ssl_verify = True
    assert network_ssl_verify(config) is True


def test_no_network_section_is_the_default():
    assert network_ssl_verify(SimpleNamespace()) is True
    assert network_ssl_verify(SimpleNamespace(network=None), default=False) is False


def test_the_plugins_follow_it():
    from unittest.mock import MagicMock

    from plugins.weather.server import WeatherServer
    from plugins.web_scraper.server import WebScraperServer

    config = AgentSystemConfig()
    config.network.ssl_verify = False
    server_config = MagicMock(proxies=None)
    assert WeatherServer("weather", config, server_config).ssl_verify is False
    assert WebScraperServer("web", config, server_config).ssl_verify is False


def test_a_config_that_does_not_name_it_keeps_the_default():
    """The model's own default is False and its network section always exists: read as a setting, a config without
    the key turned verification off in plugins that had always verified."""
    assert network_ssl_verify(AgentSystemConfig()) is True
    assert network_ssl_verify(AgentSystemConfig(network={"ssl_verify": False})) is False
