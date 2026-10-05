# Decision Plugin — Design Document

## 1. Overview & Motivation

The **Decision Plugin** provides LLM agents in ScarabHive with direct access to calibrated "System One" decision models (TypeSafe's **Jev** via OpenRouter or TypeSafe, or a local Laya or Ollama model — whichever the decision profile names).

Unlike standard generative chat models that produce non-deterministic prose, decision models evaluate named questions against given content and return calibrated numeric values (probabilities, scale points, categorical choices) without generating conversational text or tool calls. They are not deterministic: Jev gives the same question a few hundredths apart on repeated calls.

In real-world agent workflows, agents frequently need to evaluate large batches of context inputs (e.g., 50, 100, or 200 documents, snippets, tickets, or user messages) against the same questions or criteria. Rather than forcing the LLM to call the tool 200 times sequentially, the tools are designed **batch-first**:
1. **`evaluate_probabilities`**: Evaluates a batch of context items against $N$ questions in parallel with bounded concurrency and outputs calibrated probabilities ($p \in [0.0, 1.0]$) for each item and question (utilizing Jev's `noul` question type).
2. **`evaluate_scores`**: Evaluates a batch of context items against $N$ scoring dimensions along ordered qualitative or quantitative scales in parallel with bounded concurrency (utilizing Jev's `score` question type).

---

## 2. Architecture & Concurrency

```
+-----------------------------------------------------------+
| Agent Loop (LLM)                                         |
+-----------------------------+-----------------------------+
                              | Tool call with batch of items
                              v
+-----------------------------------------------------------+
| DecisionServer (SchemaBasedToolServer)                    |
| - src/plugins/decision/server.py                          |
| - Validates batch items (max_batch_size, context lengths) |
| - Validates questions / criteria                          |
| - Manages async parallel execution via asyncio.Semaphore  |
| - Updates status progress during batch                    |
| - Collects results, token usage, cost, and errors         |
+-----------------------------+-----------------------------+
                              | Parallel requests (bounded)
                              v
+-----------------------------------------------------------+
| DecisionsClient (src/plugins/llm_decisions/system_one.py)  |
| - POST <the profile's endpoint: OpenRouter, TypeSafe,     |
|   Laya, Ollama>                                           |
+-----------------------------------------------------------+
```

### Concurrency & Resilience
- Bounded concurrency via `asyncio.Semaphore(max_concurrency)` (default 10).
- If individual items fail within a large batch, the tool records the error under `errors[item_id]` and continues processing remaining items (partial success).
- Cancellation tokens are checked before starting each item and during the requests.
- Status progress updates periodically as items complete.

---

## 3. Tool Specifications

### 3.1 `evaluate_probabilities`
Evaluates binary or likelihood questions across a batch of context items.

- **Inputs**:
  - `items` (`array`, required): List of items to evaluate. Each item can be:
    - An object: `{"id": "doc1", "context": "..."}`
    - A plain string (auto-assigned `id = "item_1"`, `"item_2"`, etc.)
    *(Fallback: `context` parameter for single item)*.
  - `questions` (`array`, required): List of questions. Supported formats:
    - Simple string: `"Is this safe to run?"` (auto-assigned ID `q1`, `q2`, etc.).
    - Structured object:
      - `id` (`string`, optional): Identifier for the question (e.g. `"is_safe"`).
      - `question` (`string`, required): The instruction or question to judge.
      - `criteria_true` (`string`, optional): Clarification for what makes the answer true.
      - `criteria_false` (`string`, optional): Clarification for what makes the answer false.
      Both or neither: one side alone is refused with an error.
  - `max_concurrency` (`integer`, optional): Maximum concurrent requests (default and upper bound: the server's `max_concurrency`).
  - `include_details` (`boolean`, optional): Include raw distribution details per item (default: `false` to keep context small).
- **Output**:
  ```json
  {
    "status": "success",
    "results": {
      "doc_1": {
        "is_safe": 0.98,
        "requires_approval": 0.05
      },
      "doc_2": {
        "is_safe": 0.12,
        "requires_approval": 0.89
      }
    },
    "summary": {
      "total_items": 2,
      "successful_items": 2,
      "failed_items": 0,
      "total_cost": 0.000030,
      "total_input_tokens": 680,
      "total_output_tokens": 4,
      "model": "typesafe/jev-1.13"
    }
  }
  ```

### 3.2 `evaluate_scores`
Evaluates content along ordered scales for multiple criteria across a batch of context items.

- **Inputs**:
  - `items` (`array`, required): List of items to evaluate (`{"id": "...", "context": "..."}` or strings).
  - `criteria` (`array`, required): List of criteria specifications:
    - `id` (`string`, optional): Identifier (e.g. `"code_quality"`).
    - `question` (`string`, required): What to evaluate.
    - `scale` (`array` of strings, optional): Ordered levels from lowest to highest. Defaults to the server's `default_scale` (`["1", "2", "3", "4", "5"]`) if omitted.
  - A score is the continuous POSITION on the scale, counted from 0: on the default scale the numbers run from 0 to 4, and 3.0 means the label `"4"`. `include_details` adds the `legend` mapping positions to labels.
  - `max_concurrency` (`integer`, optional): Maximum concurrent requests.
  - `include_details` (`boolean`, optional): Include distributions and scale legends per item (default: `false`).
- **Output**:
  ```json
  {
    "status": "success",
    "results": {
      "func_1": {
        "quality": 3.5,
        "readability": 2.8
      },
      "func_2": {
        "quality": 1.1,
        "readability": 0.9
      }
    },
    "summary": {
      "total_items": 2,
      "successful_items": 2,
      "failed_items": 0,
      "total_cost": 0.000032,
      "total_input_tokens": 640,
      "total_output_tokens": 4,
      "model": "typesafe/jev-1.13"
    }
  }
  ```

---

## 4. Configuration Options

The shipped entry in `config/plugins.yaml` is only `type: decision` and `enabled: true`. Every other key is an optional override, written directly in the entry (not under a `config:` block, which the plugin does not read); the values shown are the defaults in code:
```yaml
plugins:
  servers:
    decision:
      type: decision
      enabled: true
      decision_profile: ""           # empty: llm_system.default_decision_profile
      max_batch_size: 250            # Maximum items evaluated in one batch tool call
      max_concurrency: 10            # Maximum parallel decision calls
      max_questions: 20              # Maximum questions/criteria per call
      max_context_length: 50000      # Maximum context length in characters per item
      default_scale: ["1", "2", "3", "4", "5"]
```
