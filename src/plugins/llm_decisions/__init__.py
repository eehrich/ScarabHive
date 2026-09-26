"""Clients for decision models — a questionnaire, not a conversation.

A decision model ("System One" in TypeSafe's words, and Jev is the first of
them) takes a piece of content plus NAMED QUESTIONS and answers each one with a
typed value and a probability. There is no message list, no prose and no tool
call, so nothing here is an ``LLMClient``: this package declares no ``provides``
at all and cannot be reached through ``chat()``. It is reached through its own
seam instead — ``provides_decisions`` in the manifest, an entry under
``llm_system.decision_models``, and ``create_decisions_from_profile`` — which
is the way the audio plugins are reached.

One wire, several hosts: TypeSafe's "System One" API, which OpenRouter serves
under ``/api/alpha/decisions`` (and ``/api/v1/systemone``), TypeSafe under
``https://api.typesafe.ai/v1/systemone``, and ``laya-serve`` on a machine that
runs the open Laya weights. One questionnaire against OpenRouter (both paths)
and laya-serve, and TypeSafe's reference for its own host (2026-09-25): the
same request, the same answer fields. So there is one client
(``system_one.py``), and what a host needs of its own is data -- a ``Host``
with the provider name its calls are booked under, its default endpoint, and
whether it takes OpenRouter's ``session_id``. Two manifest providers hand the
client one each: ``openrouter_decisions`` and ``systemone_decisions``.
"""
