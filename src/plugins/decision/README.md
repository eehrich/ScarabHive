# Decision Plugin

The **Decision Plugin** gives LLM agents access to calibrated "System One" decision models (such as TypeSafe's Jev served via OpenRouter).

Instead of running slow and expensive conversational chat loops to judge content, an agent calls batch-first decision tools to evaluate named questions against batches of context inputs in parallel. The underlying model evaluates the questionnaire directly and returns exact probabilities ($[0.0, 1.0]$) and continuous score points on ordered scales.

---

## Tools

### 1. `decision_evaluate_probabilities`
Evaluates a batch of context items against binary/likelihood questions (`noul` question type) in parallel.
- **Parameters**:
  - `items` (`array`, required): List of items to evaluate. Each item is an object `{"id": "...", "context": "..."}` or a raw context string. (Supports up to `max_batch_size`, default 250 items).
  - `questions` (`array`, required): List of questions. Each item contains `question` (required), optional `id`, and optional `criteria_true` / `criteria_false`.
  - `max_concurrency` (`integer`, optional): Override default concurrency limit (1..50).
  - `include_details` (`boolean`, optional): Include raw distribution details per item (default: `false` to keep agent context compact).
- **Returns**:
  ```json
  {
    "status": "success",
    "results": {
      "item_1": {"is_safe": 0.98, "urgent": 0.05},
      "item_2": {"is_safe": 0.12, "urgent": 0.91}
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

### 2. `decision_evaluate_scores`
Evaluates a batch of context items against multiple scoring dimensions (`score` question type) along ordered scales in parallel.
- **Parameters**:
  - `items` (`array`, required): List of items to score (`{"id": "...", "context": "..."}` or strings).
  - `criteria` (`array`, required): List of scoring dimensions. Each item contains `question` (required), optional `id`, and optional `scale` (ordered list of at least 2 levels, default: `["1", "2", "3", "4", "5"]`).
  - `max_concurrency` (`integer`, optional): Override default concurrency limit.
  - `include_details` (`boolean`, optional): Include raw distributions and scale legends per item (default: `false`).
- **Returns**:
  ```json
  {
    "status": "success",
    "results": {
      "func_1": {"quality": 4.25, "readability": 3.8},
      "func_2": {"quality": 2.1, "readability": 1.9}
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

## Configuration

In `config/plugins.yaml` (or under `plugins.servers.decision` in agent configs):

```yaml
plugins:
  servers:
    decision:
      type: decision
      enabled: true
      decision_profile: jev       # Profile from llm_system.decision_profiles (default: system default)
      max_batch_size: 250        # Maximum items allowed in one batch call
      max_concurrency: 10        # Maximum concurrent requests to the decision API
      max_questions: 20          # Maximum questions allowed per call
      max_context_length: 50000  # Maximum characters of context allowed per item
      default_scale: ["1", "2", "3", "4", "5"]
```

---

## Model Experience

### 1. What the model sees

#### Tool description verbatim:
```
Evaluate a batch of context items against one or more binary/likelihood questions in parallel using a calibrated decision model (Jev).
Outputs calibrated probabilities (0.0 to 1.0) per item for each question.
```
```
Evaluate a batch of context items against one or more scoring dimensions in parallel using a calibrated decision model (Jev).
Outputs continuous score points along ordered scales (e.g. 1-5 or custom levels) per item.
```

#### Success response verbatim:
```json
{
  "status": "success",
  "results": {
    "doc_1": {
      "is_safe": 0.98,
      "needs_escalation": 0.02
    },
    "doc_2": {
      "is_safe": 0.05,
      "needs_escalation": 0.95
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

#### Error strings verbatim:
- `"Items must be a non-empty list of context items."`
- `"Batch size (<len>) exceeds maximum allowed (<max>)."`
- `"Item at index <i> must be a string or object."`
- `"Duplicate item ID '<id>' at index <i>."`
- `"Item '<id>' context validation error: <err>"`
- `"Context is required and must not be null."`
- `"Context must not be empty or whitespace only."`
- `"Context length (<len>) exceeds maximum allowed (<max>)."`
- `"Questions must be a non-empty list."`
- `"Question count (<len>) exceeds limit (<max>)."`
- `"Duplicate question ID '<id>' at index <i>."`
- `"Question at index <i> must be a string or object."`
- `"Question at index <i> has empty text."`
- `"Criteria must be a non-empty list."`
- `"Criteria count (<len>) exceeds limit (<max>)."`
- `"Duplicate criterion ID '<id>' at index <i>."`
- `"Criterion at index <i> has empty question/instruction."`
- `"Criterion '<id>' scale must be an ordered list of at least 2 levels."`
- `"Failed to initialize decision client: <err>"`
- `"All <count> items failed evaluation: <first_err>"`

All errors return as `{"status": "error", "error": "<message>"}`.

### 2. Token and cache effect
- **History effect**: None. The tool call and its compact JSON response are appended to the conversation history like any standard tool call.
- **Provider-side caching**: Requests to the Decisions API send `state` and `questions`. Because questionnaire questions are identical across batch items, OpenRouter/provider prompt prefix caching operates with high efficiency across the batch.
- **Token overhead**: Each call consumes tokens on the decision model according to context length + question instructions. Returns structured JSON containing only numbers and IDs (`include_details=false` by default), preventing context window blowup even when evaluating 200 items in a single call.

### 3. Known gaps
- **No prose generation**: Decision models cannot generate explanatory text or conversational prose. If an agent needs justifications, it should use generative models or inspect question criteria breakdown.
- **Scale constraints**: Scores require an ordered discrete scale with at least 2 points (e.g. `["low", "high"]` or `["1", "2", "3", "4", "5"]`). Continuous unscaled regression is not supported by Jev.
- **Choice type omitted from top-level tools**: While Jev supports `choice`, binary probabilities (`noul`) and continuous scales (`score`) address 99% of evaluation needs. Discrete choice selection can be represented via `noul` over options or multi-level scoring.
