"""Minimal Foundry tool dispatcher, for local runs and as a template.

The gateway's Foundry backend runs the function-calling loop itself: when a
Foundry agent asks for a tool, the gateway calls the dispatcher named in
``GATEWAY_FOUNDRY_DISPATCHER_PATH`` (a ``module:attr`` string) and feeds the
result back to the model. The contract is one synchronous callable:

    dispatch_function_call(name: str, arguments: dict) -> dict

Exceptions raised here are caught by the gateway and surfaced to the model
as a ``ToolEvent.error`` instead of failing the request.

This stub implements two toy tools so the loop can be exercised without any
cloud SDK:

    GATEWAY_FOUNDRY_DISPATCHER_PATH=examples.dispatcher_stub:dispatch_function_call

``examples/`` has to be importable (run from the repository root, or put it
on ``PYTHONPATH``). In a container, install your real dispatcher package
into the image instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def dispatch_function_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_current_time":
        return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if name == "add_numbers":
        a = float(arguments.get("a", 0))
        b = float(arguments.get("b", 0))
        return {"sum": a + b}
    raise ValueError(f"Unknown tool: {name!r}")
