"""Batch Queue Manager.

Manages the collection and grouping of LLM requests for batch processing.
Requests are accumulated during a configurable time window, then submitted
as batch jobs to the respective provider APIs.

`BatchQueueManager` is one object over one shared state -- the queues, the
jobs, the callers' futures, the metrics -- and its methods are grouped by
concern into the modules of this package:

- ``core``: that state, the status reports to the waiting callers and the
  access to the batch clients
- ``clients``: `BatchClientRegistry`, which client serves which provider
- ``submission``: queuing a request, the collection window, submitting a batch
- ``polling``: the polling loop, a job's status, retries
- ``outcome``: results, completing a job, cancellation
- ``manager``: `BatchQueueManager` itself -- start, recovery, stop, metrics
"""

from .manager import BatchQueueManager

__all__ = ["BatchQueueManager"]
