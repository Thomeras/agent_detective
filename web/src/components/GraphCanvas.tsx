// Cytoscape rendering of one execution graph.
// - node fill = quality_score gradient (gray when unknown/None)
// - edge style keyed on edge type (SPAWN / A2A_MESSAGE / TOOL_DELEGATION)
// - culprit nodes get a highlight ring; propagation-path nodes/edges are lit

import cytoscape from "cytoscape";
import type { Core, ElementDefinition, Stylesheet } from "cytoscape";
import { useEffect, useRef } from "react";

import type { GraphDetail } from "../api/types";
import { scoreColor } from "../format";
import { useTheme } from "../ui/theme";

interface GraphCanvasProps {
  graph: GraphDetail;
  culprits: Set<string>;
  pathNodes: Set<string>;
  // Consecutive path hops keyed as `${source}|${target}`.
  pathEdgeKeys: Set<string>;
  selectedRunId: string | null;
  onNodeSelect: (runId: string) => void;
}

// Canvas colours come from the same CSS custom properties as the rest of the
// app, read at build time — cytoscape cannot resolve var() itself, so a
// hardcoded palette here would keep the graph dark inside a light page.
function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) =>
    css.getPropertyValue(name).trim() || fallback;
  return {
    text: v("--ad-text", "#e6edf3"),
    outline: v("--ad-bg", "#0d1117"),
    border: v("--ad-border", "#30363d"),
    edgeLine: v("--ad-text-faint", "#484f58"),
    accent: v("--ad-accent-fg", "#58a6ff"),
    ok: v("--ad-ok-fg", "#3fb950"),
    warn: v("--ad-warn-fg", "#d29922"),
    fail: v("--ad-fail-fg", "#f85149"),
    purple: v("--ad-judged-fg", "#bc8cff"),
  };
}

function buildStylesheet(): Stylesheet[] {
  const p = palette();
  return [
  {
    selector: "node",
    style: {
      "background-color": (ele) => scoreColor(ele.data("quality_score") as number | null),
      label: "data(agent_name)",
      color: p.text,
      "font-size": "11px",
      "font-family": "ui-monospace, Menlo, Consolas, monospace",
      "text-valign": "bottom",
      "text-halign": "center",
      "text-margin-y": 6,
      "text-outline-color": p.outline,
      "text-outline-width": 2,
      width: 34,
      height: 34,
      "border-width": 2,
      "border-color": p.border,
    },
  },
  {
    selector: "node.culprit",
    style: {
      "border-width": 5,
      "border-color": p.fail,
      "overlay-color": p.fail,
      "overlay-opacity": 0.12,
      "overlay-padding": 8,
    },
  },
  {
    selector: "node.on-path",
    style: {
      "border-color": p.warn,
      "border-width": 3,
    },
  },
  {
    selector: "node.selected",
    style: {
      "border-color": p.accent,
      "border-width": 4,
    },
  },
  {
    selector: "node.failed",
    style: {
      shape: "diamond",
    },
  },
  {
    // Deterministic node (no LLM tokens): square, so the LLM/non-LLM split of
    // the pipeline is visible at a glance. LLM nodes stay round.
    selector: "node.deterministic",
    style: {
      shape: "round-rectangle",
      width: 30,
      height: 30,
    },
  },
  {
    selector: "edge",
    style: {
      width: 2,
      "line-color": p.edgeLine,
      "target-arrow-color": p.edgeLine,
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
      "arrow-scale": 1,
    },
  },
  {
    selector: 'edge[type = "SPAWN"]',
    style: {
      "line-color": p.ok,
      "target-arrow-color": p.ok,
      "line-style": "solid",
    },
  },
  {
    selector: 'edge[type = "A2A_MESSAGE"]',
    style: {
      "line-color": p.accent,
      "target-arrow-color": p.accent,
      "line-style": "dashed",
    },
  },
  {
    selector: 'edge[type = "TOOL_DELEGATION"]',
    style: {
      "line-color": p.purple,
      "target-arrow-color": p.purple,
      "line-style": "dotted",
    },
  },
  {
    selector: "edge.on-path",
    style: {
      width: 4,
      "line-color": p.warn,
      "target-arrow-color": p.warn,
      "z-index": 10,
    },
  },
  {
    // Nodes that sit inside a cycle (a ReAct / retry loop): dashed purple ring.
    selector: "node.in-loop",
    style: {
      "border-style": "dashed",
      "border-color": p.purple,
      "border-width": 3,
    },
  },
  {
    // The edge that closes the cycle (loop-back): curved, labelled "loop".
    selector: "edge.loop-edge",
    style: {
      "line-color": p.purple,
      "target-arrow-color": p.purple,
      "line-style": "dashed",
      "curve-style": "unbundled-bezier",
      "control-point-distances": [-55],
      "control-point-weights": [0.5],
      label: "loop",
      "font-size": "9px",
      color: p.purple,
      "text-outline-color": p.outline,
      "text-outline-width": 2,
      "z-index": 9,
    },
  },
  ];
}

// Nodes that lie on a directed cycle, and the edges that close those cycles.
// Robust to how the loop is encoded (SPAWN chain + TOOL back-edge): we look at
// structure, not edge type. Small graphs -> plain per-node BFS is fine.
// Exported so the legend can key its "retry loop" entry off the same detection.
export function detectLoops(
  nodeIds: string[],
  edges: Array<{ source: string; target: string }>,
  startAt: Map<string, number>,
): { loopNodes: Set<string>; loopEdgeKeys: Set<string> } {
  const adj = new Map<string, string[]>();
  nodeIds.forEach((id) => adj.set(id, []));
  edges.forEach((e) => {
    if (adj.has(e.source) && adj.has(e.target)) adj.get(e.source)!.push(e.target);
  });
  const reaches = (from: string, to: string): boolean => {
    const seen = new Set<string>();
    const stack = [...(adj.get(from) ?? [])];
    while (stack.length) {
      const cur = stack.pop()!;
      if (cur === to) return true;
      if (seen.has(cur)) continue;
      seen.add(cur);
      stack.push(...(adj.get(cur) ?? []));
    }
    return false;
  };
  const loopNodes = new Set<string>();
  nodeIds.forEach((id) => {
    if (reaches(id, id)) loopNodes.add(id);
  });
  const loopEdgeKeys = new Set<string>();
  edges.forEach((e) => {
    // The cycle-closing (back) edge: closes a cycle AND runs backward in time
    // (source started after target), so the forward-flow edges stay unmarked.
    const closes = loopNodes.has(e.source) && loopNodes.has(e.target) && reaches(e.target, e.source);
    const backward = (startAt.get(e.source) ?? 0) > (startAt.get(e.target) ?? 0);
    if (closes && backward) loopEdgeKeys.add(`${e.source}|${e.target}`);
  });
  return { loopNodes, loopEdgeKeys };
}

export default function GraphCanvas({
  graph,
  culprits,
  pathNodes,
  pathEdgeKeys,
  selectedRunId,
  onNodeSelect,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const onSelectRef = useRef(onNodeSelect);
  onSelectRef.current = onNodeSelect;
  const [theme] = useTheme();

  // (Re)build the cytoscape instance when the graph structure or theme changes.
  useEffect(() => {
    if (!containerRef.current) return;

    const nodeIds = new Set(graph.nodes.map((n) => n.data.id));
    const elements: ElementDefinition[] = [
      ...graph.nodes.map((n) => ({ data: { ...n.data } })),
      // Guard against edges referencing runs that are not in the node set.
      ...graph.edges
        .filter((e) => nodeIds.has(e.data.source) && nodeIds.has(e.data.target))
        .map((e) => ({ data: { ...e.data } })),
    ];

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: buildStylesheet(),
      layout: {
        name: "breadthfirst",
        directed: true,
        padding: 24,
        spacingFactor: 1.2,
      },
      wheelSensitivity: 0.2,
    });

    cy.nodes().forEach((node) => {
      if (node.data("status") === "failed") node.addClass("failed");
      // LLM call vs deterministic step: token usage is the ground truth — a
      // node whose subtree consumed no tokens did its work without a model.
      const tokens = (node.data("tokens_in") ?? 0) + (node.data("tokens_out") ?? 0);
      if (tokens === 0) node.addClass("deterministic");
    });

    // Mark cycle (loop) members and their closing edge so a ReAct / retry loop
    // is visible at a glance; the worst member (e.g. an empty render) is still
    // its own node, red by score, clickable for detail.
    const startAt = new Map<string, number>(
      graph.nodes.map((n) => [n.data.id, Date.parse(n.data.started_at ?? "") || 0]),
    );
    const { loopNodes, loopEdgeKeys } = detectLoops(
      graph.nodes.map((n) => n.data.id),
      graph.edges.map((e) => ({ source: e.data.source, target: e.data.target })),
      startAt,
    );
    cy.nodes().forEach((node) => {
      if (loopNodes.has(node.id())) node.addClass("in-loop");
    });
    cy.edges().forEach((edge) => {
      if (loopEdgeKeys.has(`${edge.source().id()}|${edge.target().id()}`)) {
        edge.addClass("loop-edge");
      }
    });

    cy.on("tap", "node", (evt) => {
      onSelectRef.current(evt.target.id() as string);
    });

    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [graph, theme]);

  // Apply culprit / path / selection highlight classes reactively.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.nodes().forEach((node) => {
        const id = node.id();
        node.toggleClass("culprit", culprits.has(id));
        node.toggleClass("on-path", pathNodes.has(id));
        node.toggleClass("selected", selectedRunId === id);
      });
      cy.edges().forEach((edge) => {
        const key = `${edge.source().id()}|${edge.target().id()}`;
        edge.toggleClass("on-path", pathEdgeKeys.has(key));
      });
    });
  }, [culprits, pathNodes, pathEdgeKeys, selectedRunId, graph]);

  return <div className="graph-canvas" ref={containerRef} />;
}
