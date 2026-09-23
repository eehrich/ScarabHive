# Decision Plugin

The **Decision Plugin** gives LLM agents access to calibrated "System One" decision models (such as TypeSafe's Jev served via OpenRouter).

Instead of running slow and expensive conversational chat loops to judge content, an agent calls decision tools to evaluate named questions against given context. The underlying model evaluates the questionnaire directly and returns exact probabilities ($[0.0, 1.0]$) and continuous score points on ordered scales.

---

## Tools

### 1. `decision_evaluate_probabilities`
Evaluates context against binary/likelihood questions (`noul` question type).
- **Parameters**:
  - `context` (`string`, required): Context, text, code, or statement to be evaluated.
  - `questions` (`array`, required): List of questions. Each item contains `question` (required), optional `id`, and optional `criteria_true` / `criteria_false`.
- **Returns**: `{"status": "success", "probabilities": {"q1": 0.95, ...}, "details": {...}, "model": "...", "cost": ...}`.

### 2. `decision_evaluate_scores`
Evaluates context against multiple scoring dimensions (`score` question type).
- **Parameters**:
  - `context` (`string`, required): Content or statement to score.
  - `criteria` (`array`, required): List of scoring dimensions. Each item contains `question` (required), optional `id`, and optional `scale` (ordered list of at least 2 levels, default: `["1", "2", "3", "4", "5"]`).
- **Returns**: `{"status": "success", "scores": {"c1": 4.25, ...}, "details": {...}, "model": "...", "cost": ...}`.

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
      max_questions: 20          # Maximum questions allowed per call
      max_context_length: 50000  # Maximum characters of context allowed
      default_scale: ["1", "2", "3", "4", "5"]
```

---

## Model Experience

### 1. What the model sees

#### Tool description verbatim:
```
Evaluate a piece of context/text against one or more binary/likelihood questions using a calibrated decision model (Jev).
Outputs calibrated probabilities (0.0 to 1.0) for each question.
```
```
Evaluate a piece of context/text against one or more scoring dimensions using a calibrated decision model (Jev).
Outputs continuous score points along ordered scales (e.g. 1-5 or custom levels).
```

#### Success response verbatim:
```json
{
  "status": "success",
  "probabilities": {
    "is_safe": 0.98,
    "needs_escalation": 0.02
  },
  "details": {
    "is_safe": {
      "value": 0.98,
      "type": "noul",
      "confidence": null,
      "probabilities": null
    }
  },
  "model": "typesafe/jev-1.13",
  "cost": 0.000015,
  "input_tokens": 340,
  "output_tokens": 2
}
```

#### Error strings verbatim:
- `"Context is required and must not be null."`
- `"Context must not be empty or whitespace only."`
- `"Context length (<len>) exceeds maximum allowed (<max>)."`
- `"Questions must be a non-empty list."`
- `"Question count (<len>) exceeds limit (<max>)."`
- `"Question at index <i> must be a string or object."`
- `"Question at index <i> has empty text."`
- `"Criteria must be a non-empty list."`
- `"Criteria count (<len>) exceeds limit (<max>)."`
- `"Criterion at index <i> has empty question/instruction."`
- `"Criterion '<id>' scale must be an ordered list of at least 2 levels."`
- `"Failed to initialize decision client: <err>"`
- `"Decision evaluation error: <err>"`

All errors return as `{"status": "error", "error": "<message>"}`.

### 2. Token and cache effect
- **History effect**: None. The tool call and its JSON response are appended to the conversation history like any standard tool call. No system prompts or earlier messages are mutated.
- **Provider-side caching**: Requests to the Decisions API send `state` and `questions`. Because questionnaire questions are typically reused across calls for consistent evaluations, OpenRouter/provider prompt prefix caching may be utilized on the underlying endpoint.
- **Token overhead**: Each call consumes tokens on the decision model according to context length + question instructions. Returns structured JSON containing only numbers and IDs, minimizing agent conversation token footprint.

### 3. Known gaps
- **No prose generation**: Decision models cannot generate explanatory text or conversational prose. If an agent needs justifications, it should use generative models or inspect question criteria breakdown.
- **Scale constraints**: Scores require an ordered discrete scale with at least 2 points (e.g. `["low", "high"]` or `["1", "2", "3", "4", "5"]`). Continuous unscaled regression is not supported by Jev.
- **Choice type omitted from top-level tools**: While Jev supports `choice`, binary probabilities (`noul`) and continuous scales (`score`) address 99% of evaluation needs. Discrete choice selection can be represented via `noul` over options or multi-level scoring.
