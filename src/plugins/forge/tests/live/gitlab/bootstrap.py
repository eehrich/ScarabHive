"""Set up the forge test GitLab: group, project with CI, bot user, protected main, runner.

Runs on the Docker host, in the folder holding compose.yaml, .env and
root_token.out (README.md). Standard library only; safe to run again.
"""
import json
import os
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ENV = dict(line.split("=", 1) for line in open(".env").read().split() if "=" in line)
EXTERNAL = ENV["GITLAB_EXTERNAL_URL"].rstrip("/")
BASE = f"http://127.0.0.1:{ENV['GITLAB_HTTP_PORT']}/api/v4"
ROOT = open("root_token.out").read().split("ROOT_TOKEN=", 1)[1].split()[0]
PROJECT = "forge-test/app"
CI = """stages: [test]
unit:
  stage: test
  image: alpine:3.20
  script:
    - for i in $(seq 1 300); do echo "log line $i"; done
    - 'if [ -f FAIL ]; then echo "ERROR: FAIL marker present"; exit 1; fi'
    - echo "all good"
lint:
  stage: test
  image: alpine:3.20
  allow_failure: true
  script:
    - echo lint ok
"""


def call(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method,
                                     headers={"PRIVATE-TOKEN": ROOT, "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def maybe(method, path, data=None):
    """For steps that fail when they were done before."""
    try:
        return call(method, path, data)
    except urllib.error.HTTPError:
        return None


def q(value):
    return urllib.parse.quote(value, safe="")


group = next((g for g in call("GET", "/groups?search=forge-test") if g["path"] == "forge-test"), None) \
    or call("POST", "/groups", {"name": "forge-test", "path": "forge-test", "visibility": "private"})
project = maybe("GET", f"/projects/{q(PROJECT)}") or call("POST", "/projects", {
    "name": "app", "path": "app", "namespace_id": group["id"], "initialize_with_readme": True,
    "default_branch": "main", "visibility": "private"})
pid = project["id"]

bot = next(iter(call("GET", "/users?username=scarabhive-bot")), None) or call("POST", "/users", {
    "username": "scarabhive-bot", "name": "ScarabHive Bot", "email": "scarabhive-bot@example.invalid",
    "password": secrets.token_urlsafe(24), "skip_confirmation": True})
maybe("POST", f"/projects/{pid}/members", {"user_id": bot["id"], "access_level": 30})
bot_token = call("POST", f"/users/{bot['id']}/personal_access_tokens",
                 {"name": "forge-bot", "scopes": ["api"], "expires_at": "2027-09-01"})["token"]

maybe("DELETE", f"/projects/{pid}/protected_branches/main")
call("POST", f"/projects/{pid}/protected_branches?name=main&push_access_level=40&merge_access_level=30")
maybe("POST", f"/projects/{pid}/repository/files/{q('.gitlab-ci.yml')}",
      {"branch": "main", "content": CI, "commit_message": "Add CI"})

if not call("GET", "/runners/all?type=instance_type"):
    runner = call("POST", "/user/runners", {"runner_type": "instance_type", "run_untagged": True,
                                            "description": "forge-test docker"})
    subprocess.run(["docker", "compose", "exec", "-T", "runner", "gitlab-runner", "register", "--non-interactive",
                    "--url", EXTERNAL, "--token", runner["token"], "--executor", "docker",
                    "--docker-image", "alpine:3.20", "--docker-pull-policy", "if-not-present",
                    "--description", "forge-test docker"], check=True)

with open("CREDENTIALS", "w") as out:
    out.write(f"FORGE_TEST_GITLAB_URL={EXTERNAL}\nFORGE_TEST_GITLAB_PROJECT={PROJECT}\n"
              f"FORGE_TEST_GITLAB_ROOT_TOKEN={ROOT}\nFORGE_TEST_GITLAB_BOT_TOKEN={bot_token}\n")
os.chmod("CREDENTIALS", 0o600)
print("ready:", PROJECT, "bot", bot["id"])
