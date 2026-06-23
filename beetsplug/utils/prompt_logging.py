"""Prompt-aware logging to prevent interleaved console output."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import List, Set

_lock = threading.Lock()
_prompt_active = False
_buffer: List[logging.LogRecord] = []
_installed_filters: Set[int] = set()  # Track handler IDs with filter installed


class _PromptAwareFilter(logging.Filter):
    """Filter that buffers log records during prompts."""

    def filter(self, record: logging.LogRecord) -> bool:
        with _lock:
            if _prompt_active:
                _buffer.append(record)
                return False  # Don't emit - we've buffered it
        return True  # Emit normally


# Single global filter instance
_filter = _PromptAwareFilter()


def install_prompt_filter(logger_names: List[str] | None = None) -> None:
    """Install the prompt-aware filter on handlers of specified loggers.

    This is idempotent - safe to call multiple times.

    Args:
        logger_names: Logger names to install filter on.
                      Defaults to ['beets', 'beets.plexsync', ''] if None.
                      '' means the root logger.
    """
    if logger_names is None:
        logger_names = ['beets', 'beets.plexsync', '']

    for name in logger_names:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler_id = id(handler)
            if handler_id not in _installed_filters:
                handler.addFilter(_filter)
                _installed_filters.add(handler_id)


@contextmanager
def prompt_guard():
    """Buffer logs while awaiting user input, then flush on exit.

    Usage:
        with prompt_guard():
            user_input = input("Enter choice: ")
    """
    global _prompt_active

    # Ensure filter is installed on first use (idempotent)
    install_prompt_filter()

    with _lock:
        _prompt_active = True
    try:
        yield
    finally:
        with _lock:
            _prompt_active = False
            buffered = list(_buffer)
            _buffer.clear()

        # Replay buffered records - they'll pass through the filter now
        # since _prompt_active is False
        for record in buffered:
            logger = logging.getLogger(record.name)
            logger.handle(record)
