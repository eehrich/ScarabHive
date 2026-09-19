"""Clients for decision models — a questionnaire, not a conversation.

A decision model ("System One" in TypeSafe's words, and Jev is the first of
them) takes a piece of content plus NAMED QUESTIONS and answers each one with a
typed value and a probability. There is no message list, no prose and no tool
call, so nothing here is an ``LLMClient``: the registry skips this package, and
a caller imports the client it wants, the way the audio plugins import a TTS
client.

One host today — ``openrouter`` — and the shape is OpenRouter's own
(``POST /api/alpha/decisions``, the single ``/api/`` path in their whole
specification, with their provider-routing fields). TypeSafe serves the same
models directly under a different path, and other gateways have picked them up
as well; when a second one is built here, what the two share (the three
question types, their validation, the answer types) moves into this package's
own module. Generalising before that would be guessing which half is shared.
"""
