"""Shared wire-format helpers used by more than one LLM provider plugin.

Not a provider: its ``plugin.toml`` declares ``type = ["library"]`` with no
``provides``, so the registry scan skips it — the manifest exists only so the
dependency and import guards see this package. Code that only one provider
needs lives in that provider's package instead.
"""
