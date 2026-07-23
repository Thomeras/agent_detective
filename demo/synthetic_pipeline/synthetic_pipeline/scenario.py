"""The five-agent demo scenario and its OpenInference span structure.

Graph shape (build spec 6.5). One trace, one execution graph. Edges as
reconstructed by ``packages/otel_mapper``:

    orchestrator --SPAWN--> scraper-agent
    orchestrator --SPAWN--> translator-agent
    orchestrator --SPAWN--> compliance-agent
    orchestrator --SPAWN--> publisher-agent
    scraper-agent    --TOOL_DELEGATION--> compliance-agent
    translator-agent --TOOL_DELEGATION--> compliance-agent
    compliance-agent --TOOL_DELEGATION--> publisher-agent
    compliance-agent --SPAWN--> scraper-agent (retry run)
    scraper-agent (retry run) --A2A_MESSAGE--> compliance-agent

The propagation path ``scraper -> compliance -> publisher`` therefore exists,
which is what the M6 acceptance test checks. SPAWN comes from each child AGENT
span being parented by the spawning agent's AGENT run span; TOOL_DELEGATION
comes from TOOL spans carrying ``gen_ai.tool.target_agent`` (the edge points
from the target's run to the caller, the direction of data flow).

Retry loop and A2A message: compliance-agent finds the price data suspicious
(faulted) or unavailable (happy) and sends an A2A task back to scraper-agent
asking for a re-scrape. The retry executes as a second ``scraper-agent`` AGENT
span parented under compliance's run span (a separate run node — the mapper
keys runs on the opening AGENT span, not the agent name), so SPAWN
compliance -> retry is emitted (names differ). Inside the retry run a SERVER
span carrying ``a2a.task_id`` + ``a2a.peer_agent=compliance-agent`` yields the
A2A_MESSAGE edge retry -> compliance (server side: the owner's output flows to
the peer). Together these two edges form a cycle among the run nodes —
the graph-model shape the UI marks as a retry loop.

The scenario models a localized-product publishing pipeline whose source pages
list no prices. In a clean run every agent reports prices as unavailable. Under
``scraper_hallucinate`` the scraper fabricates concrete prices and every
downstream agent faithfully carries them forward -- a silent hallucination.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, set_span_in_context

from . import conventions as C
from .exporter import CollectingSpanExporter, DeterministicIdGenerator
from .llm_client import LLMClient

# Deterministic base: 2026-07-01T00:00:00Z in unix nanoseconds.
_DETERMINISTIC_BASE_NANOS = 1_751_328_000_000_000_000

_PRODUCTS = [
    {"sku": "SKU-001", "name": "Wireless Mouse", "name_pl": "Mysz bezprzewodowa"},
    {"sku": "SKU-002", "name": "USB-C Hub", "name_pl": "Koncentrator USB-C"},
    {"sku": "SKU-003", "name": "Laptop Stand", "name_pl": "Podstawka pod laptopa"},
]

# Fabricated prices used only when hallucinating. The source pages list none.
_FABRICATED_PRICES = ["$24.99", "$39.50", "$15.00"]

# Deterministic A2A task id for the compliance -> scraper re-scrape request.
_RESCRAPE_TASK_ID = "a2a-task-rescrape-0001"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    target_agent: str
    start: float
    end: float
    error: bool = False


@dataclass(frozen=True)
class A2ASpec:
    """An A2A task-handling span emitted inside an agent's run.

    Emitted as a SERVER-kind span carrying ``a2a.task_id`` and
    ``a2a.peer_agent`` so the mapper derives an A2A_MESSAGE edge from the
    owning run to the peer (server side: the handler's output flows back to
    the requesting peer).
    """

    name: str
    peer_agent: str
    task_id: str
    start: float
    end: float


@dataclass(frozen=True)
class AgentSpec:
    agent_name: str
    version: str
    span_name: str
    input_value: str
    output_value: str
    tokens_in: int
    tokens_out: int
    cost: float
    start: float
    end: float
    llm_span_name: str
    llm_prompt: str
    llm_start: float
    llm_end: float
    llm_tokens_in: int
    llm_tokens_out: int
    is_root: bool = False
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)
    a2a: tuple[A2ASpec, ...] = field(default_factory=tuple)
    # Agent name whose AGENT span parents this one. None => the root
    # orchestrator span (or no parent when is_root). Must name an agent whose
    # spec appears earlier in the list.
    parent_agent: str | None = None


def _scraper_products(hallucinate: bool) -> list[dict]:
    out = []
    for i, p in enumerate(_PRODUCTS):
        item = {"sku": p["sku"], "name": p["name"]}
        if hallucinate:
            item["price"] = _FABRICATED_PRICES[i]
        else:
            item["price"] = None
            item["price_status"] = "unavailable"
        out.append(item)
    return out


def build_agent_specs(hallucinate: bool) -> list[AgentSpec]:
    """Construct the agent specs (five agents + scraper retry run) for the
    given fault mode."""
    scraped = _scraper_products(hallucinate)
    price_by_sku = {p["sku"]: p.get("price") for p in scraped}

    scraper_out = {
        "stage": "scrape",
        "products": scraped,
        "note": "prices extracted" if hallucinate else "source pages list no prices",
    }
    translator_out = {
        "stage": "translate",
        "products": [
            {"sku": p["sku"], "name_pl": p["name_pl"]} for p in _PRODUCTS
        ],
        "language": "pl",
    }
    # Compliance is not sure about the price data (present but unverifiable
    # when hallucinating, absent otherwise) and asks the scraper — over A2A —
    # to re-scrape. The retry's answer is what compliance records here.
    rescrape_out = {
        "stage": "re-scrape",
        "task_id": _RESCRAPE_TASK_ID,
        "requested_by": "compliance-agent",
        "products": scraped,
        "note": (
            "re-extracted the same prices from the pages"
            if hallucinate
            else "re-checked all three pages: no prices listed anywhere"
        ),
    }
    compliance_out = {
        "stage": "compliance",
        "products": [
            {
                "sku": p["sku"],
                "name_pl": p["name_pl"],
                "price": price_by_sku.get(p["sku"]),
                "compliant": True,
            }
            for p in _PRODUCTS
        ],
        "price_recheck": {
            "requested_from": "scraper-agent",
            "task_id": _RESCRAPE_TASK_ID,
            "reason": (
                "price provenance could not be verified against source pages"
                if hallucinate
                else "prices missing from scraped data"
            ),
            "result": (
                "scraper re-confirmed the extracted prices"
                if hallucinate
                else "scraper re-confirmed prices are unavailable; passing without prices"
            ),
        },
        "verdict": "pass",
    }
    publisher_out = {
        "stage": "publish",
        "published": [
            {
                "sku": p["sku"],
                "name_pl": p["name_pl"],
                "price": price_by_sku.get(p["sku"]),
            }
            for p in _PRODUCTS
        ],
        "status": "published",
    }
    orchestrator_out = {
        "stage": "orchestrate",
        "summary": "Localized and published 3 products.",
        "agents": ["scraper-agent", "translator-agent", "compliance-agent", "publisher-agent"],
    }

    return [
        AgentSpec(
            agent_name="orchestrator",
            version="1.4.0",
            span_name="orchestrator.run",
            input_value="Publish three localized products. Source pages do not list prices.",
            output_value=json.dumps(orchestrator_out),
            tokens_in=1200,
            tokens_out=300,
            cost=0.012,
            start=0.0,
            end=20.0,
            llm_span_name="orchestrator.plan_llm",
            llm_prompt="Plan the multi-agent localization pipeline for three products.",
            llm_start=0.2,
            llm_end=0.9,
            llm_tokens_in=140,
            llm_tokens_out=60,
            is_root=True,
        ),
        AgentSpec(
            agent_name="scraper-agent",
            version="0.9.2",
            span_name="scraper.run",
            input_value="Scrape three product pages. The pages do not list prices.",
            output_value=json.dumps(scraper_out),
            tokens_in=800,
            tokens_out=220,
            cost=0.006,
            start=1.0,
            end=6.0,
            llm_span_name="scraper.extract_llm",
            llm_prompt="Extract product fields from the fetched pages. "
            + json.dumps(scraper_out),
            llm_start=1.5,
            llm_end=4.0,
            llm_tokens_in=620,
            llm_tokens_out=160,
        ),
        AgentSpec(
            agent_name="translator-agent",
            version="1.1.0",
            span_name="translator.run",
            input_value="Translate the three product names to Polish.",
            output_value=json.dumps(translator_out),
            tokens_in=400,
            tokens_out=260,
            cost=0.004,
            start=2.0,
            end=7.0,
            llm_span_name="translator.translate_llm",
            llm_prompt="Translate product names to Polish. " + json.dumps(translator_out),
            llm_start=2.5,
            llm_end=5.0,
            llm_tokens_in=300,
            llm_tokens_out=200,
        ),
        AgentSpec(
            agent_name="compliance-agent",
            version="2.0.1",
            span_name="compliance.run",
            input_value="Validate localized product data against the compliance policy.",
            output_value=json.dumps(compliance_out),
            tokens_in=700,
            tokens_out=180,
            cost=0.005,
            start=8.0,
            end=14.0,
            llm_span_name="compliance.check_llm",
            llm_prompt="Check localized products for compliance. " + json.dumps(compliance_out),
            llm_start=10.0,
            llm_end=12.0,
            llm_tokens_in=560,
            llm_tokens_out=140,
            tools=(
                ToolSpec("fetch_product_data", "scraper-agent", 8.5, 9.0),
                ToolSpec("fetch_translations", "translator-agent", 9.2, 9.7),
            ),
        ),
        # The retry run: scraper-agent handles the A2A re-scrape task from
        # compliance-agent. Parented under compliance's run span (SPAWN
        # compliance -> retry) and carrying the A2A SERVER span (A2A_MESSAGE
        # retry -> compliance), which together close a retry-loop cycle.
        AgentSpec(
            agent_name="scraper-agent",
            version="0.9.2",
            span_name="scraper.retry_run",
            input_value=(
                "A2A task from compliance-agent: re-scrape the three product "
                "pages and verify the price data."
            ),
            output_value=json.dumps(rescrape_out),
            tokens_in=450,
            tokens_out=140,
            cost=0.003,
            start=12.2,
            end=13.6,
            llm_span_name="scraper.reextract_llm",
            llm_prompt="Re-extract product prices from the fetched pages. "
            + json.dumps(rescrape_out),
            llm_start=12.4,
            llm_end=13.2,
            llm_tokens_in=360,
            llm_tokens_out=100,
            parent_agent="compliance-agent",
            a2a=(
                A2ASpec(
                    name="scraper.handle_a2a_rescrape",
                    peer_agent="compliance-agent",
                    task_id=_RESCRAPE_TASK_ID,
                    start=12.25,
                    end=13.55,
                ),
            ),
        ),
        AgentSpec(
            agent_name="publisher-agent",
            version="1.0.3",
            span_name="publisher.run",
            input_value="Publish the compliant, localized products to the storefront.",
            output_value=json.dumps(publisher_out),
            tokens_in=500,
            tokens_out=300,
            cost=0.007,
            start=15.0,
            end=19.0,
            llm_span_name="publisher.publish_llm",
            llm_prompt="Render storefront listings. " + json.dumps(publisher_out),
            llm_start=16.0,
            llm_end=18.0,
            llm_tokens_in=420,
            llm_tokens_out=220,
            tools=(ToolSpec("fetch_compliance_report", "compliance-agent", 15.2, 15.7),),
        ),
    ]


def _nanos(base: int, offset_s: float) -> int:
    return base + int(offset_s * 1_000_000_000)


def build_spans(
    specs: list[AgentSpec],
    graph_id: str,
    llm: LLMClient,
    *,
    deterministic: bool,
    base_nanos: int,
) -> list:
    """Emit the OpenInference spans for the scenario and return ReadableSpans."""
    exporter = CollectingSpanExporter()
    # A plain Resource (not Resource.create) keeps attributes fixed and avoids
    # the random service.instance.id, so deterministic runs are reproducible.
    resource = Resource(attributes={"service.name": "agent-detective-demo"})
    provider_kwargs = {"resource": resource}
    if deterministic:
        # Seed ids with the graph id so different scenarios get disjoint
        # trace/span ids (otherwise ingest drops the second graph's runs).
        provider_kwargs["id_generator"] = DeterministicIdGenerator(seed=graph_id)
    provider = TracerProvider(**provider_kwargs)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("openinference.instrumentation.demo")

    root_ctx = None
    root_span = None
    # First AGENT span context per agent name, for parent_agent resolution.
    # First-wins so a retry run never becomes the parenting anchor.
    ctx_by_agent: dict[str, object] = {}

    for spec in specs:
        if spec.is_root:
            parent_ctx = None
        elif spec.parent_agent is not None:
            parent_ctx = ctx_by_agent[spec.parent_agent]
        else:
            parent_ctx = root_ctx
        agent_span = tracer.start_span(
            spec.span_name,
            context=parent_ctx,
            start_time=_nanos(base_nanos, spec.start),
        )
        agent_span.set_attribute(C.SPAN_KIND, C.KIND_AGENT)
        agent_span.set_attribute(C.AGENT_NAME, spec.agent_name)
        agent_span.set_attribute(C.AGENT_VERSION, spec.version)
        agent_span.set_attribute(C.INPUT_VALUE, spec.input_value)
        agent_span.set_attribute(C.OUTPUT_VALUE, spec.output_value)
        agent_span.set_attribute(C.USAGE_INPUT_TOKENS, spec.tokens_in)
        agent_span.set_attribute(C.USAGE_OUTPUT_TOKENS, spec.tokens_out)
        agent_span.set_attribute(C.USAGE_COST, spec.cost)
        agent_span.set_attribute(C.GRAPH_ID, graph_id)
        agent_span.set_status(Status(StatusCode.OK))

        agent_ctx = set_span_in_context(agent_span)
        ctx_by_agent.setdefault(spec.agent_name, agent_ctx)
        if spec.is_root:
            root_span = agent_span
            root_ctx = agent_ctx

        # Realism: each agent makes one LLM call to the mock LLM. The reply text
        # is not used to build the graph (the demo owns its structured output),
        # but the call is real so the mock LLM is genuinely exercised.
        llm.complete(spec.llm_prompt)
        llm_span = tracer.start_span(
            spec.llm_span_name,
            context=agent_ctx,
            start_time=_nanos(base_nanos, spec.llm_start),
        )
        llm_span.set_attribute(C.SPAN_KIND, C.KIND_LLM)
        llm_span.set_attribute(C.USAGE_INPUT_TOKENS, spec.llm_tokens_in)
        llm_span.set_attribute(C.USAGE_OUTPUT_TOKENS, spec.llm_tokens_out)
        llm_span.set_status(Status(StatusCode.OK))
        llm_span.end(end_time=_nanos(base_nanos, spec.llm_end))

        for tool in spec.tools:
            tool_span = tracer.start_span(
                f"{spec.span_name.split('.')[0]}.{tool.name}",
                context=agent_ctx,
                start_time=_nanos(base_nanos, tool.start),
            )
            tool_span.set_attribute(C.SPAN_KIND, C.KIND_TOOL)
            tool_span.set_attribute(C.TOOL_NAME, tool.name)
            tool_span.set_attribute(C.TOOL_TARGET_AGENT, tool.target_agent)
            tool_span.set_status(
                Status(StatusCode.ERROR if tool.error else StatusCode.OK)
            )
            tool_span.end(end_time=_nanos(base_nanos, tool.end))

        for a2a in spec.a2a:
            # SERVER kind: this run handles an A2A task sent by the peer, so
            # the mapper points the A2A_MESSAGE edge owner -> peer.
            a2a_span = tracer.start_span(
                a2a.name,
                context=agent_ctx,
                kind=SpanKind.SERVER,
                start_time=_nanos(base_nanos, a2a.start),
            )
            a2a_span.set_attribute(C.A2A_TASK_ID, a2a.task_id)
            a2a_span.set_attribute(C.A2A_PEER_AGENT, a2a.peer_agent)
            a2a_span.set_status(Status(StatusCode.OK))
            a2a_span.end(end_time=_nanos(base_nanos, a2a.end))

        agent_span.end(end_time=_nanos(base_nanos, spec.end))

    provider.force_flush()
    provider.shutdown()
    # Order spans by start time for a stable, readable payload.
    return sorted(exporter.spans, key=lambda s: (s.start_time, s.name))


def deterministic_base_nanos() -> int:
    return _DETERMINISTIC_BASE_NANOS
