"""The version the API reports is the framework's, and its two declarations agree.

/api/version returned a fixed "1.0.0" while config.yaml and pyproject.toml
said 0.6.x. It reports config.version now, and a bump that forgets one of the
two files fails here.
"""
import tomllib
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api.endpoints import router
from agent_system.config.models import AgentSystemConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_endpoint_reports_the_configured_version():
    app = FastAPI()
    app.include_router(router)
    app.state.config = AgentSystemConfig(version="9.9.9")

    response = TestClient(app).get("/api/version")

    assert response.json()["version"] == "9.9.9"


def test_config_and_package_declare_the_same_version():
    config = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["version"] == project["project"]["version"]
