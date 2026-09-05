"""Bounded retries for interactive query and evidence embeddings."""

import logging
import random
import re
import time
from typing import Callable, Optional, TypeVar

from butly_core.settings.defaults import RUNTIME_EMBEDDING_RETRY

logger = logging.getLogger(__name__)
T = TypeVar("T")


def transient_embedding_status(error: Exception) -> Optional[int]:
    """Read SDK status fields; only explicit transient HTTP codes are retried."""
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value if value in (429, 500, 502, 503, 504) else None
    match = re.match(r"^(429|500|502|503|504)\b", str(error))
    return int(match[1]) if match else None


def embed_with_retry(call: Callable[[], T], diagnostics: dict) -> T:
    """Retry transient SDK failures and preserve the original terminal exception.

    The delay limit covers application backoff, not SDK/network request time.
    Diagnostics belong to one invocation, never to a shared provider instance.
    """
    policy = RUNTIME_EMBEDDING_RETRY
    diagnostics.update(
        attempts=0, retry_count=0, rate_limit_count=0,
        retry_wait_ms=0, retry_exhausted=False,
    )
    for attempt in range(1, policy["max_attempts"] + 1):
        diagnostics["attempts"] = attempt
        try:
            result = call()
        except Exception as exc:
            status = transient_embedding_status(exc)
            if status == 429:
                diagnostics["rate_limit_count"] += 1
            if status is None:
                raise
            if attempt == policy["max_attempts"]:
                diagnostics["retry_exhausted"] = True
                logger.warning(
                    "Embedding retry exhausted: status=%s attempts=%s",
                    status, attempt,
                )
                raise
            delay = min(
                policy["initial_delay_seconds"] * 2 ** (attempt - 1),
                policy["max_delay_seconds"],
            )
            delay += random.uniform(0, policy["jitter_seconds"])
            diagnostics["retry_count"] += 1
            diagnostics["retry_wait_ms"] += round(delay * 1000)
            logger.warning(
                "Embedding retry: status=%s attempt=%s/%s wait=%.2fs",
                status, attempt, policy["max_attempts"], delay,
            )
            time.sleep(delay)
        else:
            if diagnostics["retry_count"]:
                logger.info(
                    "Embedding recovered after %s retries", diagnostics["retry_count"]
                )
            return result
    raise AssertionError("Embedding retry policy must allow at least one attempt")
