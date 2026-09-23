# Decision Plugin — Design Document

## 1. Overview & Motivation

The **Decision Plugin** provides LLM agents in ScarabHive with direct access to calibrated "System One" decision models (specifically TypeSafe's **Jev** via OpenRouter).

Unlike standard generative chat models that produce non-deterministic prose, decision models evaluate named questions against given content and return deterministic or calibrated numeric values (probabilities, scale points, categorical choices) without generating conversational text or tool calls.

While the low-level provider interface lives in `src/plugins/llm_decisions` and `agent_system.llm.decisions`, agent tools did not previously expose this capability to running agents. The Decision Plugin bridges this gap by exposing high-level, resilient tools:

1. **`evaluate_probabilities`**: Evaluates 1 context against $N$ questions and outputs calibrated probabilities ($p \in [0.0, 1.0]$) for each question (utilizing Jev's `noul` question type).
2. **`evaluate_scores`**: Evaluates 1 context against $N$ scoring dimensions along ordered qualitative or quantitative scales (utilizing Jev's `score` question type).

---

## 2. Architecture & Seam Integration

```
+-----------------------------------------------------------+
| Agent Loop (LLM)                                         |
+-----------------------------+-----------------------------+
                              | Tool call (JSON)
                              v
+-----------------------------------------------------------+
| DecisionServer (SchemaBasedToolServer)                    |
| - src/plugins/decision/server.py                          |
| - Validates input (context length, question count/shape)  |
| - Formats questions into Jev API schema                   |
| - Reports status via StatusScope (start, progress, end)   |
+-----------------------------+-----------------------------+
                              |
                              v
+-----------------------------------------------------------+
| agent_system.llm.decisions.create_decisions_from_profile   |
| (resolves profile e.g. "jev" -> DecisionsClient)          |
+-----------------------------+-----------------------------+
                              |
                              v
+-----------------------------------------------------------+
| DecisionsClient (src/plugins/llm_decisions/openrouter.py)  |
| - POST https://openrouter.ai/api/alpha/decisions          |
+-----------------------------------------------------------+
```

### Profile Resolution
The plugin retrieves the decision client via `create_decisions_from_profile(self.system_config, profile_name)`.
- If `profile` is configured in `server_config`, it uses that profile name.
- Otherwise, it falls back to `llm_system.default_decision_profile` (configured as `"jev"` in system configs).
- The client instance is cached per profile on the server instance.

---

## 3. Tool Specifications

### 3.1 `evaluate_probabilities`
Evaluates binary or likelihood questions against the provided context.

- **Inputs**:
  - `context` (`string | object | array`, required): The text or structured object to be judged.
  - `questions` (`array`, required): List of questions. Supported formats:
    - Simple string: `"Is this safe to run?"` (auto-assigned ID `q1`, `q2`, etc.).
    - Structured object:
      - `id` (`string`, optional): Identifier for the question (e.g. `"is_safe"`).
      - `question` (`string`, required): The instruction or question to judge.
      - `criteria_true` (`string`, optional): Clarification for what makes the answer true.
      - `criteria_false` (`string`, optional): Clarification for what makes the answer false.
- **Output**:
  ```json
  {
    "status": "success",
    "probabilities": {
      "is_safe": 0.98,
      "requires_approval": 0.05
    },
    "details": {
      "is_safe": {
        "value": 0.98,
        "question": "Is this safe to run?"
      }
    },
    "model": "typesafe/jev-1.13",
    "cost": 0.000015,
    "input_tokens": 340,
    "output_tokens": 2
  }
  ```

### 3.2 `evaluate_scores`
Evaluates content along ordered scales for multiple criteria.

- **Inputs**:
  - `context` (`string | object | array`, required): Content to score.
  - `criteria` (`array`, required): List of criteria specifications:
    - `id` (`string`, optional): Identifier (e.g. `"code_quality"`).
    - `question` (`string`, required): What to evaluate (e.g. `"Rate the readability and cleanliness of the code"`).
    - `scale` (`array` of strings, optional): Ordered levels from lowest to highest. Minimum 2 levels. Defaults to `["1", "2", "3", "4", "5"]` if omitted.
- **Output**:
  ```json
  {
    "status": "success",
    "scores": {
      "code_quality": 4.25
    },
    "details": {
      "code_quality": {
        "score": 4.25,
        "scale": ["1", "2", "3", "4", "5"],
        "probabilities": {"0": 0.0, "1": 0.0, "2": 0.05, "3": 0.65, "4": 0.30},
        "confidence": 0.85
      }
    },
    "model": "typesafe/jev-1.13",
    "cost": 0.000018
  }
  ```

---

## 4. Guardrails & Error Handling

1. **Context Validation**:
   - Must not be empty.
   - Must not exceed `max_context_length` (default: 50,000 characters).
2. **Question Count Validation**:
   - Must provide at least 1 question / criterion.
   - Must not exceed `max_questions` (default: 20) to prevent token exhaustion.
3. **Scale Validation for Scores**:
   - `scale` must contain at least 2 distinct levels.
4. **Error Formatting**:
   - Returns standard error dictionary on failure:
     `{"status": "error", "error": "<actionable explanation>"}`.
5. **Status Scope**:
   - Exactly one `status.end` or `status.error`.
   - Result message under 140 chars describing counts/IDs without pure filler words.
