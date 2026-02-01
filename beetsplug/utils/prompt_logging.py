"""Prompt-aware logging to prevent interleaved console output."""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager

_lock = threading.Lock()
_prompt_active = False
_buffer: list[logging.LogRecord] = []
_handler: "PromptAwareHandler | None" = None


class PromptAwareHandler(logging.Handler):
    """Logging handler that buffers records during prompts."""

    def __init__(self, stream=None) -> None:
        super().__init__()
        self._stream_handler = logging.StreamHandler(stream or sys.stderr)

    def setFormatter(self, fmt) -> None:
        self._stream_handler.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        with _lock:
            if _prompt_active:
                _buffer.append(record)
                return
        self._stream_handler.handle(record)

    def flush(self) -> None:
        self._stream_handler.flush()


def configure_prompt_logging(level: int = logging.WARNING, stream=None) -> PromptAwareHandler:
    """Configure root logging with a prompt-aware handler."""
    global _handler

    handler = PromptAwareHandler(stream or sys.stderr)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)

    _handler = handler
    return handler


@contextmanager
def prompt_guard():
    """Buffer logs while awaiting user input, then flush on exit."""
    global _prompt_active

    with _lock:
        _prompt_active = True
    try:
        yield
    finally:
        with _lock:
            _prompt_active = False
            buffered = list(_buffer)
            _buffer.clear()

        if _handler is None:
            for record in buffered:
                logging.getLogger(record.name).handle(record)
        else:
            for record in buffered:
                _handler._stream_handler.handle(record)
