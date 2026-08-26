"""Shared wire-format helpers used by more than one LLM provider plugin.

No ``plugin.toml`` on purpose: this is not a provider, the registry scan
skips it (same pattern as ``plugins_writer/writer_core``). Code that only
one provider needs lives in that provider's package instead.
"""
