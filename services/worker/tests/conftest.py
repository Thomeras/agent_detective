"""Shared test harness: in-memory fakes for every worker seam.

The suite is fully network-free. Fakes implement the same async protocols as the
real clients (Repo, ObjectStore, StreamPublisher/StreamConsumer, JudgeClient,
WebhookClient) and emulate Postgres idempotency semantics (ON CONFLICT job
claim, ``(graph_id, incident_key)`` uniqueness, versioned blame reports) so
tests assert on the state the real database would hold.

IMPORTANT (repo-wide gotcha): all service source roots share one ``.pth`` on
sys.path, so ``from tests.conftest import ...`` resolves to the wrong service.
Test modules must import these helpers as ``from conftest import ...`` and be
run from within the worker directory.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from blame_engine import NodeScore

from worker.config import Settings
from worker.judge_client import JudgeError
from worker.memory import InMemoryObjectStore, InMemoryRepo
from worker.types import EdgeRecord, GraphBundle, RunRecord, StreamMessage


def uid(n: int) -> UUID:
    """Deterministic UUID for a small integer (e.g. uid(1))."""
    return UUID(int=n)


def make_run(
    run_id: int,
    agent_name: str,
    *,
    graph_id: int = 1,
    status: str = "ok",
    input_inline: str | None = "input",
    output_inline: str | None = "a well formed output",
    agent_version: str | None = None,
    cost_usd: float | None = 0.0,
    tokens_out: int | None = None,
    end_time: float = 0.0,
    artifact_meta: str | None = None,
    tool_calls: str | None = None,
    tool_schema_hash: str | None = None,
) -> RunRecord:
    from datetime import datetime, timezone

    ended = datetime.fromtimestamp(end_time, tz=timezone.utc) if end_time else None
    return RunRecord(
        run_id=uid(run_id),
        graph_id=uid(graph_id),
        agent_name=agent_name,
        agent_version=agent_version,
        status=status,
        input_inline=input_inline,
        input_overflow_ref=None,
        output_inline=output_inline,
        output_overflow_ref=None,
        output_bytes=len(output_inline.encode()) if output_inline else 0,
        cost_usd=cost_usd,
        tokens_in=None,
        tokens_out=tokens_out,
        started_at=None,
        ended_at=ended,
        artifact_meta=artifact_meta,
        tool_calls=tool_calls,
        tool_schema_hash=tool_schema_hash,
    )


def make_bundle(
    runs: list[RunRecord],
    edges: list[tuple[int, int]],
    *,
    graph_id: int = 1,
    name: str | None = "test graph",
    graph_type: str | None = "pipeline",
    total_cost_usd: float | None = None,
    edge_type: str = "SPAWN",
) -> GraphBundle:
    return GraphBundle(
        graph_id=uid(graph_id),
        name=name,
        graph_type=graph_type,
        total_cost_usd=total_cost_usd,
        run_count=len(runs),
        runs=runs,
        edges=[EdgeRecord(from_run_id=uid(a), to_run_id=uid(b), type=edge_type) for a, b in edges],
    )


class FakeRepo(InMemoryRepo):
    """The shipped in-memory repo plus the one affordance only tests need.

    The behaviour under test — job-claim idempotency, incident uniqueness,
    blame-report versioning, the evidence hash chain, Welford baselines — lives
    in ``worker.memory.InMemoryRepo``, which is production code (the local-mode
    CLI runs the pipeline against it). Asserting against a parallel fake would
    only prove the fake agrees with itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_ping = False

    async def ping(self) -> None:
        if self.fail_ping:
            raise ConnectionError("postgres unreachable")


class FakeObjectStore(InMemoryObjectStore):
    pass


class FakeStreams:
    """Implements both StreamPublisher and StreamConsumer protocols."""

    def __init__(self) -> None:
        self.published: dict[str, list[dict[str, Any]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self._feed: dict[str, list[StreamMessage]] = {}
        self.acked: dict[str, list[str]] = {}
        self.pending_rows: dict[str, list[Any]] = {}
        # Pending entries whose real payload can be XCLAIMed back (reclaim
        # path): stream -> {message_id: StreamMessage}. An id present here makes
        # ``claim`` return the actual message instead of the poison default.
        self.pending_data: dict[str, dict[str, StreamMessage]] = {}
        self.claimed: list[str] = []
        self._id = 0

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str:
        self.published.setdefault(stream, []).append(message)
        self._id += 1
        return f"0-{self._id}"

    def messages(self, stream: str) -> list[dict[str, Any]]:
        return self.published.get(stream, [])

    def feed(self, stream: str, data: dict[str, Any]) -> str:
        self._id += 1
        msg = StreamMessage(id=f"0-{self._id}", data=data)
        self._feed.setdefault(stream, []).append(msg)
        return msg.id

    def feed_pending(
        self, stream: str, data: dict[str, Any], *, delivery_count: int = 1
    ) -> str:
        """Seed an already-delivered, un-acked pending entry (the orphan a
        killed worker leaves behind) — NOT a new ``>`` message. ``read`` never
        returns it; only ``reclaim_pending_messages`` (via pending + claim) can.
        """
        from worker.types import PendingEntry

        self._id += 1
        mid = f"0-{self._id}"
        self.pending_rows.setdefault(stream, []).append(
            PendingEntry(id=mid, delivery_count=delivery_count)
        )
        self.pending_data.setdefault(stream, {})[mid] = StreamMessage(id=mid, data=data)
        return mid

    async def ensure_group(self, stream: str, group: str) -> None:
        self.groups.add((stream, group))

    async def read(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[StreamMessage]:
        messages = self._feed.pop(stream, [])
        return messages[:count]

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        self.acked.setdefault(stream, []).append(message_id)
        # XACK removes the entry from the PEL — drop it from both mirrors so a
        # later reclaim pass does not re-yield an already-processed message.
        rows = self.pending_rows.get(stream)
        if rows:
            self.pending_rows[stream] = [e for e in rows if e.id != message_id]
        self.pending_data.get(stream, {}).pop(message_id, None)

    async def pending(self, stream, group, min_idle_ms, count):
        return self.pending_rows.get(stream, [])

    async def claim(self, stream, group, consumer, message_id, min_idle_ms):
        self.claimed.append(message_id)
        seeded = self.pending_data.get(stream, {}).get(message_id)
        if seeded is not None:
            return seeded
        # Default (used by the reaper path, which only needs *some* payload to
        # move to the DLQ): a placeholder poison message.
        return StreamMessage(id=message_id, data={"payload": "poison"})

    async def ping(self) -> None:
        pass

    async def close(self) -> None:
        pass


_AGENT_RE = re.compile(r"agent named `([^`]+)`")


class FakeJudge:
    """Canned judge keyed on prompt content.

    - per-node judge prompt ("quality judge for a single step") -> the mapping
      in ``node_verdicts[agent_name]`` (default task_score 1.0);
    - terminal judge prompt ("final quality gate") -> ``terminal``;
    - claims prompt ("auditing one step") -> ``{"claims": claims}``.

    Set ``fail=True`` to always raise (drives the "judge -> None" paths).
    """

    def __init__(
        self,
        *,
        node_verdicts: dict[str, dict[str, Any]] | None = None,
        terminal: dict[str, Any] | None = None,
        claims: list[str] | None = None,
        fail: bool = False,
    ) -> None:
        self.node_verdicts = node_verdicts or {}
        self.terminal = terminal or {"verdict": "ok", "score": 0.95, "reasoning": "looks good"}
        self.claims = claims or []
        self.fail = fail
        self.calls: list[str] = []

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        if self.fail:
            raise JudgeError("forced failure")
        if "final quality gate" in prompt:
            self.calls.append("terminal")
            return dict(self.terminal)
        if "auditing one step" in prompt:
            self.calls.append("claims")
            return {"claims": list(self.claims)}
        # per-node judge
        match = _AGENT_RE.search(prompt)
        agent = match.group(1) if match else "unknown"
        self.calls.append(f"judge:{agent}")
        return dict(
            self.node_verdicts.get(
                agent, {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"}
            )
        )


class FakeWebhook:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, payload: dict[str, Any]) -> None:
        self.posts.append((url, payload))


def make_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


def node_score(run_id: int, score: float | None, **kw: Any) -> NodeScore:
    return NodeScore(
        run_id=str(uid(run_id)),
        score=score,
        components=kw.get("components", {"schema": None, "judge": score, "heuristics": None}),
        input_flawed=kw.get("input_flawed"),
        unscored_reason=kw.get("unscored_reason"),
        judge_note=kw.get("judge_note"),
    )
