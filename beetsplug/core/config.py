from __future__ import annotations

from typing import Any, Sequence

import confuse
from beets import config


def get_config_value(item_cfg: Any, defaults_cfg: Any, key: str, code_default: Any):
    """Get a config value from item or defaults with a code fallback."""
    if key in item_cfg:
        val = item_cfg[key]
        return val.get() if hasattr(val, "get") else val
    if key in defaults_cfg:
        val = defaults_cfg[key]
        return val.get() if hasattr(val, "get") else val
    return code_default


def get_plexsync_config(path: str | Sequence[str], cast=None, default=None):
    """Safely fetch a plexsync config value with consistent defaults."""
    segments = (path,) if isinstance(path, str) else tuple(path)
    node = config['plexsync']
    try:
        for segment in segments:
            node = node[segment]
    except (confuse.NotFoundError, KeyError, TypeError):
        return default

    try:
        return node.get(cast) if cast is not None else node.get()
    except (confuse.NotFoundError, confuse.ConfigValueError, TypeError):
        return default
