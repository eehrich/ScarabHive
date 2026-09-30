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
drop it without a word. A declared config key is either honoured or it
fails loudly — a key that is accepted and then does nothing is the failure
this plugin was built to detect.

``safety_settings`` has no SDK parameter and is refused at construction.
Measured 2026-09-01: OpenRouter drops the field on ``/responses`` itself —
the same nonsense value that earns an HTTP 400 with the valid enum list on
``/chat/completions`` is swallowed with an HTTP 200 here, so the httpx route
loses it too, just silently. That measurement concedes the mechanism and
only disputes the damage; it used to buy a WARNING plus a silent rewrite to
``None``. It now buys a refusal, because "declared and ineffective" is the
one state a model entry must never be in. No catalogue entry is affected:
the Gemini entries carry a comment saying they deliberately omit the field.
An entry that needs it names ``provider: openai_responses`` — one word.

Everything the SDK CAN carry is carried: the declared client-side keys
(``tool_schema_dialect``, ``reasoning_details_mode``) shape the payload in
the inherited builder before the transport sees it, and their payload
results (``tools``, ``input``) travel as typed parameters.

The OpenAI-style cache marker this route actually uses —
``prompt_cache_breakpoint`` — IS in the SDK schema and travels unchanged
(measured). So does the top-level ``cache_control`` the inherited builder
sends for ``prompt_cache_marker_style: anthropic`` (measured 2026-09-30,
``ttl`` included).

A typed parameter that fails the SDK's validation either degrades to
``Unset`` and is left out whole, without an error (a numeric
``provider.max_price`` — the SDK types its prices as strings — takes the
entire routing object with it), or raises a pydantic ``ValidationError``
before anything is sent (an ``input_image`` without ``detail``, a
``function_call`` whose ``call_id`` is None). So: ``provider_routing`` and
``plugins`` are checked through the SDK's types when the client is built;
``detail`` is filled with the API's default ``auto``; ``_post`` compares the
bytes the SDK is about to send with what it was given; and whatever the SDK
still refuses becomes one loud ``ValueError`` — nothing leaves either way.

ONE SUBSTITUTED DEFAULT
=======================
The SDK adds three fields we never sent: ``store: false`` and ``stream:
false`` (both what this route wants anyway) and ``service_tier: "auto"``.
The last one means the flex-tier drop sends the standard tier EXPLICITLY
where the httpx route omits the key — the same effect, off the saturated
queue, but not a byte-identical request. Pinned by the tier-drop test.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx
import pydantic

from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

logger = logging.getLogger(__name__)

#: Payload keys the SDK's ``responses.send_async`` accepts under the same
#: name. Anything the payload builder produces that is NOT listed here is
#: dropped by the typed signature, so it gets a warning instead of silence.
_SDK_PARAMS = frozenset({
    "cache_control", "input", "instructions", "max_output_tokens", "metadata", "model",
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


def _first_loss(ours: Any, sent: Any, path: str) -> Optional[str]:
    """The first path of ``ours`` the sent body does not carry as given.

    A ``None`` of ours counts as nothing to carry: the SDK leaves nulls out.
    """
    if ours is None:
        return None
    if isinstance(ours, dict):
        if not isinstance(sent, dict):
            return path
        for key, value in ours.items():
            lost = _first_loss(value, sent.get(key), f"{path}.{key}")
            if lost:
                return lost
        return None
    if isinstance(ours, (list, tuple)):
        if not isinstance(sent, list) or len(sent) != len(ours):
            return path
        for i, (mine, theirs) in enumerate(zip(ours, sent)):
            lost = _first_loss(mine, theirs, f"{path}[{i}]")
            if lost:
                return lost
        return None
    return None if ours == sent else path


def _with_image_detail(item: Any) -> Any:
    """``detail: "auto"`` on every ``input_image`` of a message item.

    The API defaults a missing ``detail`` to ``auto``; the SDK's typed part
    requires the field, and without it refuses the whole ``input`` -- no
    image could ever reach this route.
    """
    content = item.get("content") if isinstance(item, dict) else None
    if not isinstance(content, list):
        return item
    return {**item, "content": [
        {**part, "detail": "auto"}
        if isinstance(part, dict) and part.get("type") == "input_image"
        and not part.get("detail") else part
        for part in content]}


def _refuse_what_the_sdk_cannot_type(model: str, name: str, value: Any) -> None:
    """Build-time check of a config-derived parameter through the SDK's type.

    What the SDK would drop or refuse here it would drop or refuse on every
    request; the entry is refused once, when the client is built.
    """
    from typing import List

    from openrouter import components, utils
    from openrouter.types import OptionalNullable

    typ = (OptionalNullable[components.ProviderPreferences] if name == "provider_routing"
           else Optional[List[components.ResponsesRequestPlugin]])
    try:
        lost = _first_loss(value, json.loads(
            utils.marshal_json(utils.get_pydantic_model(value, typ), typ)), name)
    except pydantic.ValidationError:
        lost = name
    if lost:
        raise ValueError(
            f"provider 'openrouter_sdk' cannot send {lost!r} as declared "
            f"(model={model!r}): the SDK would drop or refuse it (it types "
            f"max_price values as strings, e.g. \"1\"). Fix the entry or use "
            f"provider: openai_responses.")


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

        This is the BACKSTOP, for a payload key a future builder change adds.
        A key that a MODEL ENTRY causes (safety_settings) never gets this
        far: the factory refuses to build such a client at all, because that
        loss is a config error with a one-line fix, and mid-run is the wrong
        moment to learn about it.
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
        if isinstance(kwargs.get("input"), list):
            kwargs["input"] = [_with_image_detail(item) for item in kwargs["input"]]
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

        kwargs = self._to_sdk_kwargs(payload)

        async def _refuse_a_loss(request: httpx.Request) -> None:
            # A typed parameter the SDK cannot validate degrades to Unset and
            # is left out WITHOUT an error -- the whole object, not just the
            # bad field (a numeric provider.max_price takes the pin, `only`
            # and `data_collection` with it). Checked on the bytes that would
            # travel, before they do.
            lost = _first_loss(kwargs, json.loads(await request.aread()), "")
            if lost:
                raise ValueError(
                    f"openrouter_sdk: the SDK would not send {lost.lstrip('.')!r} "
                    f"as given (model={self.model}); refused instead of sending "
                    f"the request without it. The httpx route "
                    f"(provider: openai_responses) sends it.")

        previous_hooks = dict(client.event_hooks)
        client.event_hooks = {
            **previous_hooks,
            "request": [*previous_hooks.get("request", []), _refuse_a_loss],
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
                    # which backend answered when it is asked to. OpenRouter
                    # only, as on the httpx route's _headers().
                    x_open_router_metadata="enabled" if self._is_openrouter else None,
                    **kwargs)
            except pydantic.ValidationError as e:
                # Raised while the SDK types our arguments, before anything is
                # sent. Untyped, 100+ lines of union noise, and it would end
                # the run all the same: the loud refusal instead.
                first = e.errors()[0] if e.errors() else {}
                raise ValueError(
                    f"openrouter_sdk: the SDK refuses the request as built "
                    f"(model={self.model}), nothing was sent: "
                    f"{str(first.get('input'))[:200]}. The httpx route "
                    f"(provider: openai_responses) sends it.") from None
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
    """Construct the client, refusing safety_settings, which the SDK cannot send.

    A declared key that cannot travel must not be accepted, because the loss
    has no error to show for it. The operator moves the entry back to
    ``provider: openai_responses`` in one line — see the module docstring.
    """
    if safety_settings:
        raise ValueError(
            f"provider 'openrouter_sdk' cannot send safety_settings "
            f"(model={model!r}): the SDK has no such parameter, so the "
            f"declared thresholds would silently not apply. Use provider: "
            f"openai_responses — or drop the field, which is what the "
            f"Gemini entries do (OpenRouter ignores it on /responses).")
    for name, value in (("provider_routing", kwargs.get("provider_routing")),
                        ("plugins", kwargs.get("plugins"))):
        if value:
            _refuse_what_the_sdk_cannot_type(model, name, value)
    return OpenRouterSDKClient(
        model=model, api_key=api_key, base_url=base_url,
        safety_settings=None, prompt_cache_marker_style=prompt_cache_marker_style,
        **kwargs)
