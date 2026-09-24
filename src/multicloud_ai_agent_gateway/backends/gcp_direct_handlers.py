"""DirectBackend handlers for Google Cloud.

These handlers are deterministic, no-LLM building blocks for the GCP side of
the gateway. They use Application Default Credentials locally and whatever
Google credentials are wired into the pod in-cluster.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from multicloud_ai_agent_gateway.backends.direct import DirectHandler
from multicloud_ai_agent_gateway.core.backend import AgentReply, ToolEvent

log = logging.getLogger(__name__)


BigQueryClientFactory = Callable[[], Any]
AssetClientFactory = Callable[[], Any]


class NoBillingExportTables(RuntimeError):
    """The configured BigQuery dataset does not contain billing export tables."""


def build_gcp_cost_mtd_by_service_handler(
    *,
    project_id: str,
    billing_dataset: str,
    billing_table: str = "",
    location: str = "EU",
    client_factory: BigQueryClientFactory | None = None,
) -> DirectHandler:
    """Return a handler that reads current-month GCP cost from BigQuery export."""
    if not project_id:
        raise ValueError("GCP cost handler requires project_id.")
    if not billing_dataset:
        raise ValueError("GCP cost handler requires billing_dataset.")

    factory = client_factory or (lambda: _default_bigquery_client(project_id))

    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        period = _month_to_date_period()
        try:
            groups, source_table = await asyncio.to_thread(
                _query_gcp_cost_mtd_sync,
                factory,
                project_id,
                billing_dataset,
                billing_table,
                location,
                period,
            )
            markdown = render_gcp_cost_markdown(groups, period, source_table)
            result: dict[str, Any] | None = {
                "cost_groups": groups,
                "period": period,
                "source_table": source_table,
            }
            error = None
        except NoBillingExportTables as exc:
            markdown = (
                "No GCP Cloud Billing export tables were found in "
                f"`{project_id}.{billing_dataset}`.\n\n"
                "Enable Detailed usage cost export for the billing account and "
                "point it at this dataset; after data starts flowing I can return "
                "month-to-date spend by service."
            )
            result = {"period": period, "dataset": f"{project_id}.{billing_dataset}"}
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - SDKs expose several exception classes
            log.exception("GCP cost direct handler failed")
            markdown = (
                "I could not read GCP billing export data. "
                f"Backend error: `{type(exc).__name__}: {exc}`"
            )
            result = {"period": period, "dataset": f"{project_id}.{billing_dataset}"}
            error = str(exc)

        return AgentReply(
            output_text=markdown,
            conversation_id=None,
            tool_events=[
                ToolEvent(
                    name="gcp_cost_mtd_by_service",
                    arguments={
                        "project_id": project_id,
                        "billing_dataset": billing_dataset,
                        "billing_table": billing_table or None,
                        "location": location,
                        "period": period,
                    },
                    result=result,
                    error=error,
                )
            ],
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            finish_reason="completed",
        )

    return handler


def build_gcp_asset_inventory_summary_handler(
    *,
    project_id: str,
    client_factory: AssetClientFactory | None = None,
    page_size: int = 1000,
) -> DirectHandler:
    """Return a handler that summarizes project assets via Cloud Asset Inventory."""
    if not project_id:
        raise ValueError("GCP inventory handler requires project_id.")
    factory = client_factory or _default_asset_client

    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        try:
            summary = await asyncio.to_thread(
                _list_asset_inventory_sync, factory, project_id, page_size
            )
            markdown = render_gcp_asset_inventory_markdown(project_id, summary)
            result: dict[str, Any] | None = summary
            error = None
        except Exception as exc:  # noqa: BLE001
            log.exception("GCP asset inventory direct handler failed")
            markdown = (
                "I could not read GCP Asset Inventory. "
                f"Backend error: `{type(exc).__name__}: {exc}`"
            )
            result = {"project_id": project_id}
            error = str(exc)

        return AgentReply(
            output_text=markdown,
            conversation_id=None,
            tool_events=[
                ToolEvent(
                    name="gcp_asset_inventory_summary",
                    arguments={"project_id": project_id, "page_size": page_size},
                    result=result,
                    error=error,
                )
            ],
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            finish_reason="completed",
        )

    return handler


def build_gcp_agent_handler(
    *,
    cost_handler: DirectHandler,
    inventory_handler: DirectHandler,
) -> DirectHandler:
    """Return one user-facing GCP handler that selects internal GCP tools.

    This keeps the gateway registry simple: clients see one ``GcpAgent`` while
    implementation details such as FinOps vs inventory remain tool choices.
    """

    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        selected_tools = _select_gcp_tools(prompt)
        tool_events = [
            ToolEvent(
                name="gcp_agent_router",
                arguments={"prompt": prompt},
                result={"selected_tools": selected_tools, "method": "deterministic_keywords"},
                error=None,
            )
        ]
        replies: list[AgentReply] = []

        for tool in selected_tools:
            if tool == "cost":
                reply = await cost_handler(prompt, conversation_id)
            elif tool == "inventory":
                reply = await inventory_handler(prompt, conversation_id)
            else:  # pragma: no cover - guarded by _select_gcp_tools
                continue
            replies.append(reply)
            tool_events.extend(reply.tool_events)

        output_text = _merge_gcp_agent_outputs(selected_tools, replies)
        return AgentReply(
            output_text=output_text,
            conversation_id=None,
            tool_events=tool_events,
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            finish_reason="completed",
        )

    return handler


def _default_bigquery_client(project_id: str) -> Any:
    from google.cloud import bigquery

    return bigquery.Client(project=project_id)


def _default_asset_client() -> Any:
    from google.cloud import asset_v1

    return asset_v1.AssetServiceClient()


def _month_to_date_period() -> dict[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    end_exclusive = today + timedelta(days=1)
    return {"Start": start.isoformat(), "End": end_exclusive.isoformat()}


def _query_gcp_cost_mtd_sync(
    client_factory: BigQueryClientFactory,
    project_id: str,
    dataset_id: str,
    configured_table: str,
    location: str,
    period: dict[str, str],
) -> tuple[list[dict[str, Any]], str]:
    client = client_factory()
    table_id = configured_table or _discover_billing_export_table(client, project_id, dataset_id)
    source_table = f"{project_id}.{dataset_id}.{table_id}"
    query = f"""
    SELECT
      COALESCE(service.description, service.id, '(unknown service)') AS service,
      COALESCE(project.id, '(no project)') AS project_id,
      SUM(CAST(cost AS NUMERIC)) AS amount,
      ANY_VALUE(currency) AS currency
    FROM `{source_table}`
    WHERE DATE(usage_start_time) >= DATE '{period["Start"]}'
      AND DATE(usage_start_time) < DATE '{period["End"]}'
    GROUP BY service, project_id
    HAVING amount != 0
    ORDER BY amount DESC
    LIMIT 25
    """
    rows = list(client.query(query, location=location).result())
    groups: list[dict[str, Any]] = []
    for row in rows:
        amount = _row_value(row, "amount")
        groups.append(
            {
                "service": str(_row_value(row, "service") or "(unknown service)"),
                "project_id": str(_row_value(row, "project_id") or "(no project)"),
                "amount": float(amount or 0),
                "currency": str(_row_value(row, "currency") or ""),
            }
        )
    groups.sort(key=lambda item: item["amount"], reverse=True)
    return groups, source_table


def _discover_billing_export_table(client: Any, project_id: str, dataset_id: str) -> str:
    dataset = f"{project_id}.{dataset_id}"
    table_ids = [table.table_id for table in client.list_tables(dataset)]
    if not table_ids:
        raise NoBillingExportTables(f"No tables in {dataset}.")

    prefixes = (
        "gcp_billing_export_resource_v1_",
        "gcp_billing_export_v1_",
        "gcp_billing_export_v2_",
    )
    for prefix in prefixes:
        matches = sorted(table_id for table_id in table_ids if table_id.startswith(prefix))
        if matches:
            return matches[0]
    raise NoBillingExportTables(
        f"No recognized Cloud Billing export table in {dataset}; found: {', '.join(sorted(table_ids))}."
    )


def _list_asset_inventory_sync(
    client_factory: AssetClientFactory,
    project_id: str,
    page_size: int,
) -> dict[str, Any]:
    from google.cloud import asset_v1

    client = client_factory()
    parent = f"projects/{project_id}"
    request = asset_v1.ListAssetsRequest(
        parent=parent,
        content_type=asset_v1.ContentType.RESOURCE,
        page_size=page_size,
    )
    counts: Counter[str] = Counter()
    samples: list[dict[str, str]] = []
    total = 0
    for asset in client.list_assets(request=request):
        total += 1
        asset_type = str(asset.asset_type)
        counts[asset_type] += 1
        if len(samples) < 20:
            samples.append({"asset_type": asset_type, "name": str(asset.name)})

    return {
        "project_id": project_id,
        "total_assets": total,
        "asset_type_counts": dict(counts.most_common()),
        "samples": samples,
    }


def render_gcp_cost_markdown(
    cost_groups: list[dict[str, Any]],
    period: dict[str, str],
    source_table: str,
) -> str:
    if not cost_groups:
        return (
            f"No GCP spend rows were found between {period['Start']} and {period['End']}.\n\n"
            f"_Source: `{source_table}`, deterministic GCP direct agent, no LLM tokens used._"
        )

    total = sum(Decimal(str(group["amount"])) for group in cost_groups)
    currency = str(cost_groups[0].get("currency") or "")
    lines = [
        f"### GCP costs {period['Start']} to {period['End']}",
        "",
        f"Total: **{total:,.2f} {currency}** across {len(cost_groups)} service/project groups.",
        "",
        "| Service | Project | Spend |",
        "| --- | --- | ---: |",
    ]
    for group in cost_groups[:20]:
        lines.append(
            f"| {group['service']} | {group['project_id']} | "
            f"{Decimal(str(group['amount'])):,.2f} {group.get('currency', '')} |"
        )
    if len(cost_groups) > 20:
        rest = sum(Decimal(str(group["amount"])) for group in cost_groups[20:])
        lines.append(f"| _other ({len(cost_groups) - 20} groups)_ |  | {rest:,.2f} {currency} |")
    lines += [
        "",
        f"_Source: `{source_table}`, deterministic GCP direct agent, no LLM tokens used._",
    ]
    return "\n".join(lines)


def render_gcp_asset_inventory_markdown(project_id: str, summary: dict[str, Any]) -> str:
    counts = summary.get("asset_type_counts") or {}
    total = int(summary.get("total_assets") or 0)
    if total == 0:
        return f"No GCP assets were returned for project `{project_id}`."

    lines = [
        f"### GCP asset inventory for `{project_id}`",
        "",
        f"Total assets returned: **{total}**.",
        "",
        "| Asset type | Count |",
        "| --- | ---: |",
    ]
    for asset_type, count in list(counts.items())[:20]:
        lines.append(f"| `{asset_type}` | {count} |")

    samples = summary.get("samples") or []
    if samples:
        lines += ["", "Sample assets:"]
        for sample in samples[:8]:
            lines.append(f"- `{sample['asset_type']}`: `{sample['name']}`")
    lines += ["", "_Source: Cloud Asset Inventory, deterministic GCP direct agent._"]
    return "\n".join(lines)


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:  # noqa: BLE001
        return getattr(row, key, None)


_GCP_COST_TERMS = {
    "bill",
    "billing",
    "budget",
    "cost",
    "costs",
    "driver",
    "drivers",
    "expense",
    "expenses",
    "finops",
    "invoice",
    "price",
    "spend",
    "spending",
    "usd",
}

_GCP_INVENTORY_TERMS = {
    "asset",
    "assets",
    "bucket",
    "buckets",
    "cloudrun",
    "dataset",
    "datasets",
    "inventory",
    "list",
    "resource",
    "resources",
    "service",
    "services",
    "storage",
    "vm",
    "vms",
}

_GCP_BROAD_TERMS = {
    "all",
    "everything",
    "overview",
    "status",
    "summary",
    "summarize",
}


def _select_gcp_tools(prompt: str) -> list[str]:
    tokens = set(_tokenize_prompt(prompt))
    wants_cost = bool(tokens & _GCP_COST_TERMS)
    wants_inventory = bool(tokens & _GCP_INVENTORY_TERMS)
    wants_broad = bool(tokens & _GCP_BROAD_TERMS)

    if wants_cost and not wants_inventory:
        return ["cost"]
    if wants_inventory and not wants_cost:
        return ["inventory"]
    if wants_cost and wants_inventory:
        return ["cost", "inventory"]
    if wants_broad:
        return ["cost", "inventory"]
    return ["inventory"]


def _tokenize_prompt(prompt: str) -> list[str]:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in prompt)
    tokens = normalized.split()
    # Include a few joined bigrams/trigrams so "cloud run" matches cloudrun
    # style keywords without adding fuzzy matching complexity.
    joined = ["".join(tokens[index : index + 2]) for index in range(max(len(tokens) - 1, 0))]
    return tokens + joined


def _merge_gcp_agent_outputs(selected_tools: list[str], replies: list[AgentReply]) -> str:
    if not replies:
        return "No GCP tool was selected for this request."

    if len(replies) == 1:
        prefix = f"_GcpAgent selected `{selected_tools[0]}` tool._\n\n"
        return prefix + replies[0].output_text

    sections = ["_GcpAgent selected multiple tools: cost and inventory._"]
    for tool, reply in zip(selected_tools, replies):
        sections.extend(["", f"## {tool.title()}", "", reply.output_text])
    return "\n".join(sections)


__all__ = [
    "NoBillingExportTables",
    "build_gcp_agent_handler",
    "build_gcp_asset_inventory_summary_handler",
    "build_gcp_cost_mtd_by_service_handler",
    "render_gcp_asset_inventory_markdown",
    "render_gcp_cost_markdown",
]
