"""Decision Tool Server implementation.

Provides tools for LLMs to evaluate context against questions for
calibrated probabilities (noul) and continuous scores on ordered scales (score)
using decision models (e.g. TypeSafe Jev via OpenRouter).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

DEFAULT_DEFAULT_SCALE = ["1", "2", "3", "4", "5"]
DEFAULT_MAX_QUESTIONS = 20
DEFAULT_MAX_CONTEXT_LENGTH = 50000
MAX_STATUS_MESSAGE_LEN = 140


def _cap_status_line(text: str, limit: int = MAX_STATUS_MESSAGE_LEN) -> str:
    """Ensure a status message fits within the status bus limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class DecisionServer(SchemaBasedToolServer):
    """Tool server exposing calibrated decision and scoring capabilities."""

    def __init__(
        self,
        name: str,
        system_config: AgentSystemConfig,
        server_config: ToolServerConfig,
    ) -> None:
        super().__init__(name, system_config, server_config)

        self.decision_profile: Optional[str] = (
            getattr(server_config, "decision_profile", None) or None
        )
        self.max_questions: int = int(
            getattr(server_config, "max_questions", DEFAULT_MAX_QUESTIONS)
        )
        self.max_context_length: int = int(
            getattr(server_config, "max_context_length", DEFAULT_MAX_CONTEXT_LENGTH)
        )
        scale_cfg = getattr(server_config, "default_scale", None)
        if scale_cfg and isinstance(scale_cfg, list) and len(scale_cfg) >= 2:
            self.default_scale = [str(s) for s in scale_cfg]
        else:
            self.default_scale = list(DEFAULT_DEFAULT_SCALE)

        self._client: Any = None
        self._cached_profile: Optional[str] = None

        logger.info(
            f"DecisionServer '{name}' initialized (profile={self.decision_profile or 'default'}, "
            f"max_questions={self.max_questions}, max_context_length={self.max_context_length})"
        )

    def get_template_vars(self) -> dict[str, Any]:
        """Provide template variables for schema.yaml rendering."""
        return {
            "name": self.name,
        }

    def _get_client(self, profile: Optional[str] = None) -> Any:
        """Resolve and cache the decisions client for the specified or configured profile."""
        target_profile = profile or self.decision_profile
        if self._client is not None and self._cached_profile == target_profile:
            return self._client

        from agent_system.llm.decisions import create_decisions_from_profile

        client = create_decisions_from_profile(self.system_config, target_profile)
        self._client = client
        self._cached_profile = target_profile
        return client

    def _validate_context(
        self, context: Any
    ) -> tuple[Optional[Union[str, Mapping[str, Any], Sequence[Any]]], Optional[str]]:
        """Validate and return normalized context or an error message."""
        if context is None:
            return None, "Context is required and must not be null."
        if not isinstance(context, (str, Mapping, Sequence)):
            return (
                None,
                f"Context must be a string, object, or array, not {type(context).__name__}.",
            )
        if isinstance(context, (bytes, bytearray)):
            return None, "Context must not be raw bytes."
        if isinstance(context, str):
            trimmed = context.strip()
            if not trimmed:
                return None, "Context must not be empty or whitespace only."
            if len(context) > self.max_context_length:
                return (
                    None,
                    f"Context length ({len(context)}) exceeds maximum allowed ({self.max_context_length}).",
                )
            return trimmed, None
        if len(context) == 0:
            return None, "Context must not be empty."
        try:
            serialized_len = len(json.dumps(context))
            if serialized_len > self.max_context_length:
                return (
                    None,
                    f"Context length ({serialized_len}) exceeds maximum allowed ({self.max_context_length}).",
                )
        except Exception:
            pass
        return context, None

    async def evaluate_probabilities(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate context against binary/likelihood questions returning probabilities."""
        status = params.get("_status")
        context_input = params.get("context")
        questions_raw = params.get("questions")
        cancellation_token = params.get("_cancellation_token")
        session_id = params.get("_session_id")

        context, ctx_err = self._validate_context(context_input)
        if ctx_err:
            if status is not None:
                await status.error(_cap_status_line(f"Context validation error: {ctx_err}"))
            return {"status": "error", "error": ctx_err}

        if not questions_raw or not isinstance(questions_raw, list):
            err_msg = "Questions must be a non-empty list."
            if status is not None:
                await status.error(_cap_status_line(f"Questions validation error: {err_msg}"))
            return {"status": "error", "error": err_msg}

        if len(questions_raw) > self.max_questions:
            err_msg = f"Question count ({len(questions_raw)}) exceeds limit ({self.max_questions})."
            if status is not None:
                await status.error(_cap_status_line(f"Questions limit error: {err_msg}"))
            return {"status": "error", "error": err_msg}

        questions_payload: dict[str, dict[str, Any]] = {}
        for idx, item in enumerate(questions_raw):
            if isinstance(item, str):
                qid = f"q{idx + 1}"
                q_text = item.strip()
                criteria = None
            elif isinstance(item, dict):
                qid = str(item.get("id") or f"q{idx + 1}").strip()
                q_text = str(item.get("question") or "").strip()
                crit_true = str(item.get("criteria_true") or "").strip()
                crit_false = str(item.get("criteria_false") or "").strip()
                if crit_true and crit_false:
                    criteria = {"true": crit_true, "false": crit_false}
                else:
                    criteria = None
            else:
                err_msg = f"Question at index {idx} must be a string or object."
                if status is not None:
                    await status.error(_cap_status_line(f"Question format error: {err_msg}"))
                return {"status": "error", "error": err_msg}

            if not q_text:
                err_msg = f"Question at index {idx} has empty text."
                if status is not None:
                    await status.error(_cap_status_line(f"Question content error: {err_msg}"))
                return {"status": "error", "error": err_msg}

            if qid in questions_payload:
                err_msg = f"Duplicate question ID '{qid}' at index {idx}."
                if status is not None:
                    await status.error(_cap_status_line(f"Duplicate question ID: {err_msg}"))
                return {"status": "error", "error": err_msg}

            q_spec: dict[str, Any] = {"type": "noul", "instructions": q_text}
            if criteria:
                q_spec["criteria"] = criteria
            questions_payload[qid] = q_spec

        try:
            client = self._get_client()
        except Exception as e:
            err_msg = f"Failed to initialize decision client: {e}"
            if status is not None:
                await status.error(_cap_status_line(err_msg))
            return {"status": "error", "error": err_msg}

        if status is not None:
            await status.progress(_cap_status_line(f"Evaluating {len(questions_payload)} probabilities"))

        try:
            result = await client.decide(
                state=context,
                questions=questions_payload,
                cancellation_token=cancellation_token,
                session_id=session_id,
            )
        except Exception as e:
            err_msg = f"Decision evaluation error: {e}"
            if status is not None:
                await status.error(_cap_status_line(err_msg))
            return {"status": "error", "error": err_msg}

        probabilities: dict[str, float] = {}
        details: dict[str, dict[str, Any]] = {}
        for qid, ans in result.answers.items():
            probabilities[qid] = float(ans.value)
            details[qid] = {
                "value": float(ans.value),
                "type": ans.type,
                "confidence": ans.confidence,
                "probabilities": ans.probabilities,
            }

        if status is not None:
            id_preview = ", ".join(list(probabilities.keys())[:3])
            if len(probabilities) > 3:
                id_preview += f" (+{len(probabilities) - 3} more)"
            end_msg = f"{len(probabilities)} probabilities evaluated for {id_preview}"
            await status.end(
                _cap_status_line(end_msg),
                meta={"count": len(probabilities), "model": result.model},
            )

        return {
            "status": "success",
            "probabilities": probabilities,
            "details": details,
            "model": result.model,
            "cost": result.cost,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    async def evaluate_scores(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate context against scoring dimensions returning continuous scale scores."""
        status = params.get("_status")
        context_input = params.get("context")
        criteria_raw = params.get("criteria")
        cancellation_token = params.get("_cancellation_token")
        session_id = params.get("_session_id")

        context, ctx_err = self._validate_context(context_input)
        if ctx_err:
            if status is not None:
                await status.error(_cap_status_line(f"Context validation error: {ctx_err}"))
            return {"status": "error", "error": ctx_err}

        if not criteria_raw or not isinstance(criteria_raw, list):
            err_msg = "Criteria must be a non-empty list."
            if status is not None:
                await status.error(_cap_status_line(f"Criteria validation error: {err_msg}"))
            return {"status": "error", "error": err_msg}

        if len(criteria_raw) > self.max_questions:
            err_msg = f"Criteria count ({len(criteria_raw)}) exceeds limit ({self.max_questions})."
            if status is not None:
                await status.error(_cap_status_line(f"Criteria limit error: {err_msg}"))
            return {"status": "error", "error": err_msg}

        questions_payload: dict[str, dict[str, Any]] = {}
        scales_by_id: dict[str, list[str]] = {}

        for idx, item in enumerate(criteria_raw):
            if not isinstance(item, dict):
                err_msg = f"Criterion at index {idx} must be an object."
                if status is not None:
                    await status.error(_cap_status_line(f"Criterion format error: {err_msg}"))
                return {"status": "error", "error": err_msg}

            cid = str(item.get("id") or f"c{idx + 1}").strip()
            instruction = str(
                item.get("question") or item.get("instruction") or ""
            ).strip()
            if not instruction:
                err_msg = f"Criterion at index {idx} has empty question/instruction."
                if status is not None:
                    await status.error(_cap_status_line(f"Criterion instruction error: {err_msg}"))
                return {"status": "error", "error": err_msg}

            if cid in questions_payload:
                err_msg = f"Duplicate criterion ID '{cid}' at index {idx}."
                if status is not None:
                    await status.error(_cap_status_line(f"Duplicate criterion ID: {err_msg}"))
                return {"status": "error", "error": err_msg}

            raw_scale = item.get("scale")
            if raw_scale is None:
                scale = list(self.default_scale)
            elif (
                isinstance(raw_scale, list)
                and len(raw_scale) >= 2
                and all(isinstance(s, (str, int, float)) for s in raw_scale)
            ):
                scale = [str(s) for s in raw_scale]
            else:
                err_msg = (
                    f"Criterion '{cid}' scale must be an ordered list of at least 2 levels."
                )
                if status is not None:
                    await status.error(_cap_status_line(f"Criterion scale error: {err_msg}"))
                return {"status": "error", "error": err_msg}

            scales_by_id[cid] = scale
            questions_payload[cid] = {
                "type": "score",
                "instructions": instruction,
                "criteria": scale,
            }

        try:
            client = self._get_client()
        except Exception as e:
            err_msg = f"Failed to initialize decision client: {e}"
            if status is not None:
                await status.error(_cap_status_line(err_msg))
            return {"status": "error", "error": err_msg}

        if status is not None:
            await status.progress(_cap_status_line(f"Evaluating {len(questions_payload)} scores"))

        try:
            result = await client.decide(
                state=context,
                questions=questions_payload,
                cancellation_token=cancellation_token,
                session_id=session_id,
            )
        except Exception as e:
            err_msg = f"Decision evaluation error: {e}"
            if status is not None:
                await status.error(_cap_status_line(err_msg))
            return {"status": "error", "error": err_msg}

        scores: dict[str, float] = {}
        details: dict[str, dict[str, Any]] = {}
        for cid, ans in result.answers.items():
            scores[cid] = float(ans.value)
            details[cid] = {
                "score": float(ans.value),
                "scale": scales_by_id.get(cid, self.default_scale),
                "confidence": ans.confidence,
                "probabilities": ans.probabilities,
                "legend": ans.legend,
            }

        if status is not None:
            id_preview = ", ".join(list(scores.keys())[:3])
            if len(scores) > 3:
                id_preview += f" (+{len(scores) - 3} more)"
            end_msg = f"{len(scores)} scores evaluated for {id_preview}"
            await status.end(
                _cap_status_line(end_msg),
                meta={"count": len(scores), "model": result.model},
            )

        return {
            "status": "success",
            "scores": scores,
            "details": details,
            "model": result.model,
            "cost": result.cost,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
