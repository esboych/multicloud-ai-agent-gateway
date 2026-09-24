from __future__ import annotations

from typing import Any

from multicloud_ai_agent_gateway.backends.gcp_direct_handlers import (
    build_gcp_agent_handler,
    build_gcp_cost_mtd_by_service_handler,
    render_gcp_asset_inventory_markdown,
    render_gcp_cost_markdown,
)
from multicloud_ai_agent_gateway.core.backend import AgentReply, ToolEvent


class _FakeTable:
    def __init__(self, table_id: str) -> None:
        self.table_id = table_id


class _FakeJob:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def result(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeBigQueryClient:
    def __init__(self, rows: list[dict[str, Any]] | None = None, tables: list[str] | None = None) -> None:
        self.rows = rows or []
        self.tables = tables or []
        self.queries: list[tuple[str, str | None]] = []

    def list_tables(self, dataset: str) -> list[_FakeTable]:
        return [_FakeTable(table_id) for table_id in self.tables]

    def query(self, query: str, *, location: str | None = None) -> _FakeJob:
        self.queries.append((query, location))
        return _FakeJob(self.rows)


def test_render_gcp_cost_markdown_handles_empty_rows() -> None:
    out = render_gcp_cost_markdown(
        [],
        {"Start": "2026-06-01", "End": "2026-06-25"},
        "project.dataset.table",
    )
    assert "No GCP spend rows" in out
    assert "project.dataset.table" in out
    assert "no LLM tokens used" in out


def test_render_gcp_cost_markdown_groups_total() -> None:
    out = render_gcp_cost_markdown(
        [
            {"service": "Compute Engine", "project_id": "p", "amount": 10.5, "currency": "EUR"},
            {"service": "BigQuery", "project_id": "p", "amount": 2.25, "currency": "EUR"},
        ],
        {"Start": "2026-06-01", "End": "2026-06-25"},
        "project.dataset.table",
    )
    assert "Total: **12.75 EUR**" in out
    assert "Compute Engine" in out
    assert "BigQuery" in out


async def test_gcp_cost_handler_discovers_billing_table_and_returns_tool_event() -> None:
    fake = _FakeBigQueryClient(
        tables=["gcp_billing_export_v1_015D4A_2DD771_F3A940"],
        rows=[
            {
                "service": "Cloud Run",
                "project_id": "my-gcp-project",
                "amount": 1.23,
                "currency": "EUR",
            }
        ],
    )
    handler = build_gcp_cost_mtd_by_service_handler(
        project_id="my-gcp-project",
        billing_dataset="billing_export",
        location="EU",
        client_factory=lambda: fake,
    )

    reply = await handler("show gcp costs", None)

    assert "Cloud Run" in reply.output_text
    assert reply.tool_events[0].name == "gcp_cost_mtd_by_service"
    assert reply.tool_events[0].error is None
    assert reply.tool_events[0].result is not None
    assert reply.tool_events[0].result["source_table"].endswith(
        ".billing_export.gcp_billing_export_v1_015D4A_2DD771_F3A940"
    )
    assert fake.queries[0][1] == "EU"


async def test_gcp_cost_handler_degrades_when_export_table_missing() -> None:
    fake = _FakeBigQueryClient(tables=[])
    handler = build_gcp_cost_mtd_by_service_handler(
        project_id="my-gcp-project",
        billing_dataset="billing_export",
        client_factory=lambda: fake,
    )

    reply = await handler("show gcp costs", None)

    assert "No GCP Cloud Billing export tables" in reply.output_text
    assert reply.tool_events[0].error is not None


async def test_gcp_agent_handler_routes_cost_prompt_to_cost_tool() -> None:
    handler = build_gcp_agent_handler(
        cost_handler=_fake_gcp_child_handler("cost"),
        inventory_handler=_fake_gcp_child_handler("inventory"),
    )

    reply = await handler("show current month gcp costs", None)

    assert "selected `cost` tool" in reply.output_text
    assert "cost output" in reply.output_text
    assert "inventory output" not in reply.output_text
    assert reply.tool_events[0].name == "gcp_agent_router"
    assert reply.tool_events[0].result == {
        "selected_tools": ["cost"],
        "method": "deterministic_keywords",
    }


async def test_gcp_agent_handler_routes_inventory_prompt_to_inventory_tool() -> None:
    handler = build_gcp_agent_handler(
        cost_handler=_fake_gcp_child_handler("cost"),
        inventory_handler=_fake_gcp_child_handler("inventory"),
    )

    reply = await handler("list gcp resources", None)

    assert "selected `inventory` tool" in reply.output_text
    assert "inventory output" in reply.output_text
    assert "cost output" not in reply.output_text
    assert reply.tool_events[0].result["selected_tools"] == ["inventory"]


async def test_gcp_agent_handler_runs_both_tools_for_overview_prompt() -> None:
    handler = build_gcp_agent_handler(
        cost_handler=_fake_gcp_child_handler("cost"),
        inventory_handler=_fake_gcp_child_handler("inventory"),
    )

    reply = await handler("give me a gcp overview", None)

    assert "selected multiple tools" in reply.output_text
    assert "cost output" in reply.output_text
    assert "inventory output" in reply.output_text
    assert reply.tool_events[0].result["selected_tools"] == ["cost", "inventory"]


def test_render_gcp_asset_inventory_markdown() -> None:
    out = render_gcp_asset_inventory_markdown(
        "my-gcp-project",
        {
            "total_assets": 2,
            "asset_type_counts": {"storage.googleapis.com/Bucket": 1, "run.googleapis.com/Service": 1},
            "samples": [
                {"asset_type": "storage.googleapis.com/Bucket", "name": "//storage.googleapis.com/bucket"}
            ],
        },
    )
    assert "Total assets returned: **2**" in out
    assert "storage.googleapis.com/Bucket" in out


def _fake_gcp_child_handler(name: str):
    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        return AgentReply(
            output_text=f"{name} output",
            conversation_id=conversation_id,
            tool_events=[
                ToolEvent(
                    name=f"gcp_{name}",
                    arguments={"prompt": prompt},
                    result={"ok": True},
                    error=None,
                )
            ],
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    return handler
