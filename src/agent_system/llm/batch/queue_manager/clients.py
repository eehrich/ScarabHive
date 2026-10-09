"""Which batch client serves which batch provider, each built on first use.

Its own component because its state -- the clients and the factories not yet
built -- belongs to nothing else of the queue manager, and because building on
first use has rules of its own: one attempt per provider, never an exception.
`BatchQueueManager` keeps the public registration API and asks this registry
for a client wherever it submits, polls or cancels.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


class BatchClientRegistry:
    """The batch clients of one queue manager, by batch provider name."""

    def __init__(self) -> None:
        self._clients: Dict[str, Any] = {}  # provider -> client
        self._factories: Dict[str, Callable[[], Any]] = {}  # provider -> builder

    def register(self, provider: str, client: Any) -> None:
        """Register *client* under *provider* (`BatchQueueManager.register_batch_client`)."""
        self._clients[provider] = client
        # The tracker is keyed by THIS name (add_job in _submit_batch), so the
        # client must not carry its own idea of what it is called — startup
        # cancellation looks its jobs up by exactly this key.
        client.provider_name = provider
        logger.info(f"Registered batch client for provider: {provider}")

    def register_factory(self, provider: str, factory: Callable[[], Any]) -> None:
        """Register *factory* to build the client of *provider* on first use."""
        self._factories[provider] = factory
        logger.info(f"Registered batch backend for provider {provider} (built on first use)")

    def providers(self) -> list[str]:
        """Every provider name that has a client or can still build one."""
        return list(dict.fromkeys([*self._clients, *self._factories]))

    def client_for(self, provider: str) -> Any:
        """The provider's client, building it on first use; None if unknown,
        if its factory declined (no API key) or if building it failed. Either
        outcome is logged once and the factory is forgotten, so neither the
        poll loop nor the next submit repeats the attempt every time.

        Never raises: this runs inside _submit_batch, after the job is in
        _active_jobs -- an exception there is swallowed by the collection
        task and leaves the caller waiting for its full timeout."""
        client = self._clients.get(provider)
        if client is not None:
            return client
        factory = self._factories.pop(provider, None)
        if factory is None:
            return None
        try:
            client = factory()
        except Exception as e:
            logger.error("Failed to build %s batch client: %s", provider, e, exc_info=True)
            return None
        if client is None:
            logger.info("No API key found, skipping %s batch client", provider)
            return None
        client.provider_name = provider
        self._clients[provider] = client
        return client
