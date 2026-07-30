"""Row-to-JSON serialization shared by the routers.

Repository methods return plain mappings whose values may be UUID, Decimal,
datetime or lists thereof (asyncpg native types); these helpers convert them
into JSON-serializable dicts.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID


def json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def json_row(row: Mapping[str, Any], fields: list[str]) -> dict[str, Any]:
    return {field: json_value(row.get(field)) for field in fields}


def graph_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return json_row(
        row,
        [
            "graph_id", "name", "graph_type", "status", "started_at", "ended_at",
            "total_cost_usd", "run_count", "late_spans_count", "late_spans_last_at",
        ],
    ) | {"id": str(row["graph_id"])}


def run_node(row: Mapping[str, Any]) -> dict[str, Any]:
    """Cytoscape node element for one agent run."""
    data = json_row(
        row,
        [
            "agent_name",
            "agent_version",
            "model_name",
            "prompt_hash",
            "tool_schema_hash",
            "parent_run_id",
            "trace_id",
            "status",
            "quality_score",
            "score_components",
            "unscored_reason",
            "input_flawed",
            "cost_usd",
            "tokens_in",
            "tokens_out",
            "started_at",
            "ended_at",
            "input_summary",
            "output_summary",
        ],
    )
    if data["quality_score"] is None and data["unscored_reason"] is None:
        # The engine never writes NULL+NULL, so this run was never analyzed.
        data["unscored_reason"] = "not_analyzed"
    data["id"] = str(row["run_id"])
    return {"data": data}


def run_edge(row: Mapping[str, Any]) -> dict[str, Any]:
    """Cytoscape edge element."""
    return {
        "data": {
            "id": str(row["id"]),
            "source": json_value(row["from_run_id"]),
            "target": json_value(row["to_run_id"]),
            "type": row["type"],
            "detection_method": row["detection_method"],
        }
    }


def report_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return json_row(row, ["report_type", "culprit_run_ids", "confidence", "downstream_cost_usd"])


def report_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    return json_row(
        row,
        [
            "id",
            "incident_id",
            "graph_id",
            "version",
            "is_latest",
            "report_type",
            "culprit_run_ids",
            "propagation_path",
            "confidence",
            "downstream_cost_usd",
            "unscored_run_ids",
            "evidence",
            "created_at",
        ],
    )


def incident_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Inbox row; `latest_report` holds the report summary or None."""
    data = json_row(row, ["id", "graph_id", "incident_key", "trigger", "status", "created_at", "updated_at"])
    # `report_id` is selected by the inbox query; report_type itself may be NULL.
    latest = report_summary(row) if row.get("report_id") is not None else None
    return data | {"latest_report": latest}


def incident_detail(row: Mapping[str, Any], report: Mapping[str, Any] | None) -> dict[str, Any]:
    data = json_row(row, ["id", "graph_id", "incident_key", "trigger", "status", "created_at", "updated_at"])
    return data | {"latest_report": report_detail(report) if report is not None else None}
