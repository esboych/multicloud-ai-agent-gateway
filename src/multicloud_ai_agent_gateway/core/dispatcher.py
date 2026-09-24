from __future__ import annotations

import importlib
from typing import Any, Callable

ToolDispatcher = Callable[[str, dict[str, Any]], dict[str, Any]]


def load_dispatcher(spec: str | None) -> ToolDispatcher | None:
    """Resolve a 'module:attr' string to a callable. Returns None when spec is empty."""
    if not spec:
        return None
    if ":" not in spec:
        raise ValueError(f"Dispatcher spec must be 'module:attr', got {spec!r}")
    module_name, attr_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    target = getattr(module, attr_name, None)
    if target is None:
        raise AttributeError(f"Module {module_name!r} has no attribute {attr_name!r}")
    if not callable(target):
        raise TypeError(f"{spec} resolves to {type(target).__name__}, not a callable")
    return target
