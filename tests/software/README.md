# Browser tests: the web interface from its user's side

These are the software tests (ASPICE SWE.6) of ScarabHive's web interface. Each test drives the real app in
Chromium with pytest-playwright and checks what the user sees.

| File | What it is |
|---|---|
| `scarab_harness.py` | Serves the ScarabHive under test: the real app (`build_app`) on a free port of 127.0.0.1 with a temporary data folder (`AGENT_DATA_DIR`), the test users and the model's stand-in. |
| `conftest.py` | The fixtures: `scarab`, `base_url`, `test_data`, `users`, `stand_in`, `page_as`, `user_page`. |
| `data/users.yaml` | The test users (tester, admin, guest), created in the temporary data folder. |
| `data/model.yaml` | The model's script, read by the `ModelStandIn` in `tests/agent_stand_in.py`. |
| `test_harness.py` | Tests of the harness itself. |

## The model's stand-in

Every language model of the app is replaced by `ModelStandIn` (`tests/agent_stand_in.py`). It is installed at
`llm/registry.build_client`, the one place every LLM client is built. The agent itself stays real: the request,
the session and its history, the events on the page and Stop are all ScarabHive's own code. The stand-in answers
from `data/model.yaml`:

- by what the user wrote (`when`, a regular expression),
- by name for the next run (`stand_in.use("slow")`),
- or with a test's own answer (`stand_in.when("(?i)weather", text="Sunny.")`).

`stand_in.calls` records what the agent sent. Every test starts again from the script.

## Test data

What a test needs comes from `data/`, not from values typed into the test. If a test needs a new user or answer,
add an entry there. A TEST specification names its entries by name, for example `users.tester` or `model.slow`.

## A test

```python
from playwright.sync_api import expect
from scarab_harness import open_chat

# ScarabHive: SHV-TEST-4 · software · SWE.6
def test_SHV_TEST_4_message_and_answer_in_the_chat(user_page, stand_in):
    open_chat(user_page)                       # the main page, once its chat takes a message
    user_page.locator("#task").fill("Hallo Agent")
    user_page.locator("#runBtn").click()
    expect(user_page.locator("#chat")).to_contain_text(stand_in.answers["greeting"].text)
```

## Running the tests

Once per machine, in the project's virtual environment:

```
pip install -r requirements/browser-tests.txt
python -m playwright install chromium        # or use the installed Chrome: --browser-channel chrome
```

Then (Windows PowerShell: `.venv\Scripts\python` in place of `python`):

```
python -m pytest tests/software                                   # all browser tests, headless
python -m pytest tests/software --headed --slowmo 300             # watch them in a browser window
python -m pytest tests/software -k chat --headed                  # only some of them
python -m pytest tests/software --junitxml=build/test-results/software.xml `
    --tracing=retain-on-failure --screenshot=only-on-failure --output=build/test-results/playwright
python -m playwright show-trace build/test-results/playwright/<test>/trace.zip    # replay a failure
```

The last but one command is how the test process of ScarabHive Enterprise runs them (the line break is
PowerShell's backtick; in bash it is a backslash).

Without pytest-playwright, this folder is not collected. To look at the app by hand, or to record a flow with
`playwright codegen <url>`, start it with `python tests/software/scarab_harness.py`. It prints the address and the
test users.
