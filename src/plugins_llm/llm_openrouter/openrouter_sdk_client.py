"""OpenRouter's official Python SDK as a transport for the Responses route.

WHY THIS EXISTS
===============
``openai_responses`` (llm_openai_compat) talks to OpenRouter's ``/responses``
endpoint over plain httpx. OpenRouter now ships an official, Speakeasy-
generated SDK that models the same endpoint with typed parameters. This
client is the A/B candidate: it reuses the sibling's payload builder, parser
and healing loop VERBATIM (it subclasses it) and swaps only ``_post``, so a
comparison measures the transport and nothing else.

WHAT THE SDK IS USED FOR — AND WHAT IT IS NOT
=============================================
Used for the REQUEST: typed parameters, URL, auth, its own error taxonomy.

NOT used for the RESPONSE. The first reason is structural: the inherited
loop needs the raw body anyway. It decides on ``status_code``, detects
body-level errors inside an HTTP 200, and matches reasoning-artifact
rejections against the body TEXT. Going through the typed result would mean
dumping it back to a dict and STILL reading the raw body on every error
path — more moving parts for the same outcome.

The second reason is a fragility of the typed model, reproduced against
openrouter 1.1.108 (2026-09-01) but NOT observed live:

* ``OpenResponsesResult.usage`` is ``OptionalNullable``, and its
  ``UsageCostDetails`` REQUIRES ``upstream_inference_input_cost`` and
  ``upstream_inference_output_cost``. Hand it a ``cost_details`` without
  them and the sub-model fails — whereupon the whole ``usage`` object
  degrades to ``Unset()`` instead of raising. Token counts, ``cost`` and
  cached-token figures would vanish without a word.
* The result model requires a dozen further fields (``completed_at``,
  ``error``, ``frequency_penalty``, ``instructions``, ``metadata``,
  ``tool_choice``, …); a body missing one raises instead of answering.

Honesty about the evidence: every live body sampled on 2026-09-01
(deepseek-v4-flash via deepinfra, gemini-3.5-flash-lite via google) carried
all of those fields, and the typed model parsed both correctly. The two
points above are therefore a KNOWN way for this route to lose data silently,
not a failure anyone has seen — cheap to sidestep, so sidestepped.

``_post`` hands the RAW ``httpx.Response`` back to the inherited loop, which
parses the body itself, exactly as on the httpx route. A validation error is
then not an error at all: its ``raw_response`` carries the body we wanted.

REQUEST-SIDE GAPS, DELIBERATELY LOUD
====================================
Typed parameters cannot carry what the SDK's schema does not know, and they
drop it without a word. Two payload keys this house sends have no SDK
parameter.

``prompt_cache_marker_style: anthropic`` (per-part ``cache_control``) is
REFUSED at construction: the httpx route does send it, so losing it here
would cost cache hits with no error to show for it.

``safety_settings`` only WARNS. Measured 2026-09-01: OpenRouter drops the
field on ``/responses`` itself — the same nonsense value that earns an HTTP
400 with the valid enum list on ``/chat/completions`` is swallowed with an
HTTP 200 here. The httpx route therefore loses it too, just silently.
Refusing to build would invent a difference between the routes that does not
exist and would lock the Gemini entries out for nothing.

The OpenAI-style cache marker this route actually uses —
``prompt_cache_breakpoint`` — IS in the SDK schema and travels unchanged
(measured).

ONE SUBSTITUTED DEFAULT
=======================
The SDK adds three fields we never sent: ``store: false`` and ``stream:
false`` (both what this route wants anyway) and ``service_tier: "auto"``.
The last one means the flex-tier drop sends the standard tier EXPLICITLY
where the httpx route omits the key — the same effect, off the saturated
queue, but not a byte-identical request. Pinned by the tier-drop test.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from plugins_llm.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

logger = logging.getLogger(__name__)

#: Payload keys the SDK's ``responses.send_async`` accepts under the same
#: name. Anything the payload builder produces that is NOT listed here is
#: dropped by the typed signature, so it gets a warning instead of silence.
_SDK_PARAMS = frozenset({
    "input", "instructions", "max_output_tokens", "metadata", "model",
    "models", "parallel_tool_calls", "plugins", "presence_penalty",
    "previous_response_id", "prompt_cache_key", "prompt_cache_options",
    "provider", "reasoning", "safety_identifier", "service_tier", "session_id",
    "temperature", "text", "tool_choice", "tools", "top_p", "truncation",
    "user",
})

#: Keys dropped ON PURPOSE, with the reason. Not warned about — the SDK
#: covers them itself.
_SDK_HANDLES_ITSELF = {
    "store": "the SDK always sends store=false on this endpoint",
}


class OpenRouterSDKClient(OpenAIResponsesClient):
    """Responses client whose POST travels through the OpenRouter SDK.

    Everything else — input building, cache breakpoints, tool conversion,
    response parsing, retry/healing, hook notification — is the sibling's,
    unchanged. See the module docstring for what the SDK is and is not
    trusted with.
    """

    _PROVIDER = "openrouter_sdk"

    #: No event stream on this route. The sibling's reader speaks to httpx
    #: directly, so streaming here would travel past the very transport this
    #: class exists to exercise — and the A/B comparison would silently stop
    #: comparing. Models configured for this provider keep the ``chat_tools``
    #: path regardless of ``capabilities.streaming``.
    _STREAMS_SSE = False

    #: Warned-about payload keys, per class: the cause is a config or a
    #: payload-builder change, both global. One line is the point.
    _unmapped_reported: set = set()

    def _to_sdk_kwargs(self, payload: dict) -> dict:
        """Payload dict -> typed SDK keyword arguments.

        The signature is a closed set, so a key the SDK does not know cannot
        even be passed — it would raise TypeError. Dropping it here keeps the
        call alive but says so, once: a payload field that silently stops
        travelling is the failure this plugin was built to detect.
        """
        kwargs: dict = {}
        for key, value in payload.items():
            if key in _SDK_PARAMS:
                kwargs[key] = value
            elif key in _SDK_HANDLES_ITSELF:
                continue
            elif key not in OpenRouterSDKClient._unmapped_reported:
                OpenRouterSDKClient._unmapped_reported.add(key)
                logger.warning(
                    "openrouter_sdk: payload field %r has no SDK parameter and "
                    "is NOT being sent (model=%s). The httpx route "
                    "(provider: openai_responses) does send it.", key, self.model)
        return kwargs

    async def _post(self, client: httpx.AsyncClient, url: str,
                    payload: dict) -> httpx.Response:
        """One request through the SDK, raw ``httpx.Response`` back.

        ``url`` is unused: the SDK builds it from ``server_url``. It stays in
        the signature because the inherited loop owns the seam.
        """
        from openrouter import OpenRouter
        from openrouter import errors as or_errors
        from openrouter.utils.retries import BackoffStrategy, RetryConfig

        # Retries belong to the inherited loop — it is the one that knows
        # about the flex-tier drop and the reasoning-artifact heal, and that
        # notifies the hooks per attempt. A second retry layer underneath
        # would multiply attempts and hide them from the message debugger.
        no_retries = RetryConfig("none", BackoffStrategy(1, 1, 1.0, 1), False)

        # The response is captured off the httpx client rather than off the
        # SDK's return value, because the typed result cannot carry `usage`
        # (see module docstring). httpx event hooks are restored afterwards:
        # the client belongs to the caller and survives across attempts.
        captured: list[httpx.Response] = []

        async def _capture(response: httpx.Response) -> None:
            captured.append(response)

        previous_hooks = dict(client.event_hooks)
        client.event_hooks = {
            **previous_hooks,
            "response": [*previous_hooks.get("response", []), _capture],
        }
        try:
            sdk = OpenRouter(
                api_key=self.api_key,
                async_client=client,
                server_url=self.base_url,
                retry_config=no_retries,
            )
            try:
                await sdk.responses.send_async(
                    # Header, not a body field — the gateway only reports
                    # which backend answered when it is asked to.
                    x_open_router_metadata="enabled",
                    **self._to_sdk_kwargs(payload))
            except or_errors.OpenRouterError as e:
                # Every SDK error carries the response it was raised from —
                # including ResponseValidationError, which is how a perfectly
                # good HTTP 200 arrives when the typed model refuses it. The
                # loop above only ever wanted status code and body.
                raw = getattr(e, "raw_response", None)
                if raw is None:
                    # No response at all (connection died before headers).
                    # Reported as a transport error so the inherited retry
                    # branch — which catches exactly those — takes it.
                    raise httpx.TransportError(f"{type(e).__name__}: {e}") from e
                return raw
        finally:
            client.event_hooks = previous_hooks

        if not captured:  # pragma: no cover — SDK always goes through httpx
            raise httpx.TransportError(
                "openrouter_sdk: request produced no httpx response")
        return captured[-1]


def build_openrouter_sdk_client(
    *,
    model: str,
    api_key: str,
    base_url: str,
    safety_settings: Optional[dict],
    prompt_cache_marker_style: Optional[str],
    **kwargs: Any,
) -> OpenRouterSDKClient:
    """Construct the client, handling the two fields the SDK cannot send.

    The cache-marker refusal is a real gap: the httpx route sends that field
    and this one cannot, so a run would lose cache hits with nothing to show
    for it. Raising beats that — the operator moves the entry back to
    ``provider: openai_responses`` in one line.

    ``safety_settings`` is a different case and only warns; see the module
    docstring for the measurement.
    """
    from agent_system.llm.cache_key import MARKER_STYLE_ANTHROPIC

    if safety_settings:
        logger.warning(
            "safety_settings are not sent for model=%s — OpenRouter ignores "
            "them on /responses either way. The entry can drop the field.",
            model)
    if prompt_cache_marker_style == MARKER_STYLE_ANTHROPIC:
        raise ValueError(
            f"provider 'openrouter_sdk' cannot send Anthropic-style per-part "
            f"cache_control (model={model!r}): the SDK's content-part schema "
            f"has no such field and drops it silently, which costs cache hits "
            f"without any error. Use provider: openai_responses.")
    return OpenRouterSDKClient(
        model=model, api_key=api_key, base_url=base_url,
        safety_settings=None, prompt_cache_marker_style=prompt_cache_marker_style,
        **kwargs)
