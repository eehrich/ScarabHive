"""Decision Tool Server implementation.

Provides batch-first tools for LLMs to evaluate context items against questions
for calibrated probabilities (noul) and continuous scores on ordered scales (score)
using decision models (e.g. TypeSafe Jev via OpenRouter).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.llm_decisions.system_one import DecisionsResult

logger = logging.getLogger(__name__)

DEFAULT_DEFAULT_SCALE = ["1", "2", "3", "4", "5"]
DEFAULT_MAX_QUESTIONS = 20
DEFAULT_MAX_CONTEXT_LENGTH = 50000
DEFAULT_MAX_BATCH_SIZE = 250
DEFAULT_MAX_CONCURRENCY = 10
MAX_STATUS_MESSAGE_LEN = 140


def _spend(answered: Sequence[dict]) -> dict[str, Any]:
    """The summary's cost and tokens, from the usage of every call that was answered.

    A refused answer (``DecisionsError.usage``) was billed and counts like the
    rest. The cost is None when any of them carried none -- unknown, not free:
    a local Laya and TypeSafe direct report no cost, and a sum of the others
    would read as the whole.
    """
    costs = [u["cost"] for u in answered]
    return {
        "total_cost": None if any(c is None for c in costs) else round(sum(costs), 6),
        "total_input_tokens": sum(u["input_tokens"] for u in answered),
        "total_output_tokens": sum(u["output_tokens"] for u in answered),
    }


def _cap_status_line(text: str, limit: int = MAX_STATUS_MESSAGE_LEN) -> str:
    """Ensure a status message fits within the status bus limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class DecisionServer(SchemaBasedToolServer):
    """Tool server exposing calibrated batch decision and scoring capabilities."""

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
        self.max_batch_size: int = max(
            1, int(getattr(server_config, "max_batch_size", DEFAULT_MAX_BATCH_SIZE))
        )
        self.max_concurrency: int = max(
            1, int(getattr(server_config, "max_concurrency", DEFAULT_MAX_CONCURRENCY))
        )
        self.max_questions: int = max(
            1, int(getattr(server_config, "max_questions", DEFAULT_MAX_QUESTIONS))
        )
        self.max_context_length: int = max(
            1, int(getattr(server_config, "max_context_length", DEFAULT_MAX_CONTEXT_LENGTH))
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
            f"max_batch_size={self.max_batch_size}, max_concurrency={self.max_concurrency}, "
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

    def _normalize_items(
        self, params: dict[str, Any]
    ) -> tuple[Optional[list[tuple[str, Any]]], Optional[str]]:
        """Extract and validate the batch of items to evaluate."""
        items_raw = params.get("items")
        if items_raw is None and "context" in params:
            items_raw = [{"id": "item_1", "context": params["context"]}]

        if not items_raw or not isinstance(items_raw, list):
            return None, "Items must be a non-empty list of context items."

        if len(items_raw) > self.max_batch_size:
            return (
                None,
                f"Batch size ({len(items_raw)}) exceeds maximum allowed ({self.max_batch_size}).",
            )

        seen_ids: set[str] = set()
        normalized_items: list[tuple[str, Any]] = []

        for idx, item in enumerate(items_raw):
            if isinstance(item, str):
                iid = f"item_{idx + 1}"
                raw_ctx: Any = item
            elif isinstance(item, dict):
                iid = str(item.get("id") or f"item_{idx + 1}").strip()
                if not iid:
                    iid = f"item_{idx + 1}"
                raw_ctx = item.get("context")
            else:
                return None, f"Item at index {idx} must be a string or object."

            if iid in seen_ids:
                return None, f"Duplicate item ID '{iid}' at index {idx}."
            seen_ids.add(iid)

            valid_ctx, ctx_err = self._validate_context(raw_ctx)
            if ctx_err:
                return None, f"Item '{iid}' context validation error: {ctx_err}"

            normalized_items.append((iid, valid_ctx))

        return normalized_items, None

    async def evaluate_probabilities(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate a batch of context items against binary/likelihood questions in parallel."""
        status = params.get("_status")
        questions_raw = params.get("questions")
        cancellation_token = params.get("_cancellation_token")
        session_id = params.get("_session_id")
        include_details = bool(params.get("include_details", False))
        req_concurrency = params.get("max_concurrency")

        items, items_err = self._normalize_items(params)
        if items_err:
            if status is not None:
                await status.error(_cap_status_line(f"Items validation error: {items_err}"))
            return {"status": "error", "error": items_err}
        assert items is not None

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
                qid = str(item.get("id") or "").strip() or f"q{idx + 1}"
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

        concurrency = self.max_concurrency
        if req_concurrency is not None:
            try:
                concurrency = min(max(1, int(req_concurrency)), self.max_concurrency)
            except (ValueError, TypeError):
                pass
        semaphore = asyncio.Semaphore(concurrency)

        if status is not None:
            await status.progress(
                _cap_status_line(
                    f"Evaluating {len(items)} items against {len(questions_payload)} questions"
                )
            )

        completed_count = 0
        # usage of answers the client refused: billed, so part of the spend
        refused: list[dict] = []
        progress_lock = asyncio.Lock()

        async def _eval_item(
            iid: str, ctx: Any
        ) -> tuple[str, Optional[DecisionsResult], Optional[str]]:
            nonlocal completed_count
            if cancellation_token is not None and getattr(
                cancellation_token, "is_cancelled", False
            ):
                raise asyncio.CancelledError("Evaluation cancelled by user")
            outcome: tuple[str, Optional[DecisionsResult], Optional[str]]
            async with semaphore:
                if cancellation_token is not None and getattr(
                    cancellation_token, "is_cancelled", False
                ):
                    raise asyncio.CancelledError("Evaluation cancelled by user")
                try:
                    res: DecisionsResult = await client.decide(
                        state=ctx,
                        questions=questions_payload,
                        cancellation_token=cancellation_token,
                        session_id=session_id,
                    )
                    outcome = (iid, res, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = (iid, None, str(exc))
                    billed = getattr(exc, "usage", None)
                    if billed:
                        refused.append(billed)

            async with progress_lock:
                completed_count += 1
                if status is not None and (
                    completed_count == len(items)
                    or completed_count % max(1, len(items) // 5) == 0
                ):
                    await status.progress(
                        _cap_status_line(
                            f"Evaluated {completed_count}/{len(items)} items"
                        )
                    )
            return outcome

        eval_tasks = [asyncio.create_task(_eval_item(iid, ctx)) for iid, ctx in items]
        try:
            batch_outcomes = await asyncio.gather(*eval_tasks)
        except BaseException:
            for t in eval_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*eval_tasks, return_exceptions=True)
            raise

        results: dict[str, dict[str, float]] = {}
        details: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        answered: list[dict] = list(refused)
        model_name: Optional[str] = None

        for iid, res, err in batch_outcomes:
            if err is not None:
                errors[iid] = err
                continue
            assert res is not None
            if model_name is None:
                model_name = res.model
            answered.append({"input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                             "cost": res.cost})

            item_probs: dict[str, float] = {}
            item_details: dict[str, Any] = {}
            for qid, ans in res.answers.items():
                item_probs[qid] = float(ans.value)
                if include_details:
                    item_details[qid] = {
                        "value": float(ans.value),
                        "type": ans.type,
                        "confidence": ans.confidence,
                        "probabilities": ans.probabilities,
                    }
            results[iid] = item_probs
            if include_details:
                details[iid] = item_details

        success_count = len(results)
        fail_count = len(errors)

        if success_count == 0:
            first_err = next(iter(errors.values()), "Unknown error")
            err_msg = f"All {fail_count} items failed evaluation: {first_err}"
            if status is not None:
                await status.error(_cap_status_line(err_msg))
            return {
                "status": "error",
                "error": err_msg,
                "errors": errors,
            }

        if fail_count > 0:
            end_msg = f"{success_count}/{len(items)} items evaluated ({fail_count} failed)"
            run_status = "partial_success"
        else:
            end_msg = f"{success_count} items evaluated ({len(questions_payload)} questions each)"
            run_status = "success"

        if status is not None:
            await status.end(
                _cap_status_line(end_msg),
                meta={"count": success_count, "failed": fail_count, "model": model_name},
            )

        resp: dict[str, Any] = {
            "status": run_status,
            "results": results,
            "summary": {
                "total_items": len(items),
                "successful_items": success_count,
                "failed_items": fail_count,
                **_spend(answered),
                "model": model_name,
            },
        }
        if errors:
            resp["errors"] = errors
        if include_details:
            resp["details"] = details

        # Convenience backward-compatibility for single item calls
        if len(items) == 1 and success_count == 1:
            first_id = items[0][0]
            resp["probabilities"] = results[first_id]

        return resp

    async def evaluate_scores(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate a batch of context items against scoring dimensions in parallel."""
        status = params.get("_status")
        criteria_raw = params.get("criteria")
        cancellation_token = params.get("_cancellation_token")
        session_id = params.get("_session_id")
        include_details = bool(params.get("include_details", False))
        req_concurrency = params.get("max_concurrency")

        items, items_err = self._normalize_items(params)
        if items_err:
            if status is not None:
                await status.error(_cap_status_line(f"Items validation error: {items_err}"))
            return {"status": "error", "error": items_err}
        assert items is not None

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

            cid = str(item.get("id") or "").strip() or f"c{idx + 1}"
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

        concurrency = self.max_concurrency
        if req_concurrency is not None:
            try:
                concurrency = min(max(1, int(req_concurrency)), self.max_concurrency)
            except (ValueError, TypeError):
                pass
        semaphore = asyncio.Semaphore(concurrency)

        if status is not None:
            await status.progress(
                _cap_status_line(
                    f"Evaluating {len(items)} items against {len(questions_payload)} scores"
                )
            )

        completed_count = 0
        # usage of answers the client refused: billed, so part of the spend
        refused: list[dict] = []
        progress_lock = asyncio.Lock()

        async def _eval_item(
            iid: str, ctx: Any
        ) -> tuple[str, Optional[DecisionsResult], Optional[str]]:
            nonlocal completed_count
            if cancellation_token is not None and getattr(
                cancellation_token, "is_cancelled", False
            ):
                raise asyncio.CancelledError("Evaluation cancelled by user")
            outcome: tuple[str, Optional[DecisionsResult], Optional[str]]
            async with semaphore:
                if cancellation_token is not None and getattr(
                    cancellation_token, "is_cancelled", False
                ):
                    raise asyncio.CancelledError("Evaluation cancelled by user")
                try:
                    res: DecisionsResult = await client.decide(
                        state=ctx,
                        questions=questions_payload,
                        cancellation_token=cancellation_token,
                        session_id=session_id,
                    )
                    outcome = (iid, res, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = (iid, None, str(exc))
                    billed = getattr(exc, "usage", None)
                    if billed:
                        refused.append(billed)

            async with progress_lock:
                completed_count += 1
                if status is not None and (
                    completed_count == len(items)
                    or completed_count % max(1, len(items) // 5) == 0
                ):
                    await status.progress(
                        _cap_status_line(
                            f"Evaluated {completed_count}/{len(items)} items"
                        )
                    )
            return outcome

        eval_tasks = [asyncio.create_task(_eval_item(iid, ctx)) for iid, ctx in items]
        try:
            batch_outcomes = await asyncio.gather(*eval_tasks)
        except BaseException:
            for t in eval_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*eval_tasks, return_exceptions=True)
            raise

        results: dict[str, dict[str, float]] = {}
        details: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        answered: list[dict] = list(refused)
        model_name: Optional[str] = None

        for iid, res, err in batch_outcomes:
            if err is not None:
                errors[iid] = err
                continue
            assert res is not None
            if model_name is None:
                model_name = res.model
            answered.append({"input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                             "cost": res.cost})

            item_scores: dict[str, float] = {}
            item_details: dict[str, Any] = {}
            for cid, ans in res.answers.items():
                item_scores[cid] = float(ans.value)
                if include_details:
                    item_details[cid] = {
                        "score": float(ans.value),
                        "scale": scales_by_id.get(cid, self.default_scale),
                        "confidence": ans.confidence,
                        "probabilities": ans.probabilities,
                        "legend": ans.legend,
                    }
            results[iid] = item_scores
            if include_details:
                details[iid] = item_details

        success_count = len(results)
        fail_count = len(errors)

        if success_count == 0:
            first_err = next(iter(errors.values()), "Unknown error")
            err_msg = f"All {fail_count} items failed evaluation: {first_err}"
            if status is not None:
                await status.error(_cap_status_line(err_msg))
            return {
                "status": "error",
                "error": err_msg,
                "errors": errors,
            }

        if fail_count > 0:
            end_msg = f"{success_count}/{len(items)} items evaluated ({fail_count} failed)"
            run_status = "partial_success"
        else:
            end_msg = f"{success_count} items evaluated ({len(questions_payload)} scores each)"
            run_status = "success"

        if status is not None:
            await status.end(
                _cap_status_line(end_msg),
                meta={"count": success_count, "failed": fail_count, "model": model_name},
            )

        resp: dict[str, Any] = {
            "status": run_status,
            "results": results,
            "summary": {
                "total_items": len(items),
                "successful_items": success_count,
                "failed_items": fail_count,
                **_spend(answered),
                "model": model_name,
            },
        }
        if errors:
            resp["errors"] = errors
        if include_details:
            resp["details"] = details

        # Convenience backward-compatibility for single item calls
        if len(items) == 1 and success_count == 1:
            first_id = items[0][0]
            resp["scores"] = results[first_id]

        return resp
