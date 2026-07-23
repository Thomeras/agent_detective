// Cytoscape rendering of one execution graph.
// - node fill = quality_score gradient (gray when unknown/None)
// - edge style keyed on edge type (SPAWN / A2A_MESSAGE / TOOL_DELEGATION)
// - culprit nodes get a highlight ring; propagation-path nodes/edges are lit

import cytoscape from "cytoscape";
import type { Core, ElementDefinition, Stylesheet } from "cytoscape";
import { useEffect, useRef } from "react";

import type { GraphDetail } from "../api/types";
import { scoreColor } from "../format";

interface GraphCanvasProps {
  graph: GraphDetail;
  culprits: Set<string>;
  pathNodes: Set<string>;
  // Consecutive path hops keyed as `${source}|${target}`.
  pathEdgeKeys: Set<string>;
  selectedRunId: string | null;
  onNodeSelect: (runId: string) => void;
}

const stylesheet: Stylesheet[] = [
  {
    selector: "node",
    style: {
      "background-color": (ele) => scoreColor(ele.data("quality_score") as number | null),
      label: "data(agent_name)",
      color: "#e6edf3",
      "font-size": "11px",
      "font-family": "ui-monospace, Menlo, Consolas, monospace",
      "text-valign": "bottom",
      "text-halign": "center",
      "text-margin-y": 6,
      "text-outline-color": "#0d1117",
      "text-outline-width": 2,
      width: 34,
      height: 34,
      "border-width": 2,
      "border-color": "#30363d",
    },
  },
  {
    selector: "node.culprit",
    style: {
      "border-width": 5,
      "border-color": "#f85149",
      "overlay-color": "#f85149",
      "overlay-opacity": 0.12,
      "overlay-padding": 8,
    },
  },
  {
    selector: "node.on-path",
    style: {
      "border-color": "#d29922",
      "border-width": 3,
    },
  },
  {
    selector: "node.selected",
    style: {
      "border-color": "#58a6ff",
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
      "line-color": "#484f58",
      "target-arrow-color": "#484f58",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
      "arrow-scale": 1,
    },
  },
  {
    selector: 'edge[type = "SPAWN"]',
    style: {
      "line-color": "#3fb950",
      "target-arrow-color": "#3fb950",
      "line-style": "solid",
    },
  },
  {
    selector: 'edge[type = "A2A_MESSAGE"]',
    style: {
      "line-color": "#58a6ff",
      "target-arrow-color": "#58a6ff",
      "line-style": "dashed",
    },
  },
  {
    selector: 'edge[type = "TOOL_DELEGATION"]',
    style: {
      "line-color": "#bc8cff",
      "target-arrow-color": "#bc8cff",
      "line-style": "dotted",
    },
  },
  {
    selector: "edge.on-path",
    style: {
      width: 4,
      "line-color": "#d29922",
      "target-arrow-color": "#d29922",
      "z-index": 10,
    },
  },
  {
    // Nodes that sit inside a cycle (a ReAct / retry loop): dashed purple ring.
    selector: "node.in-loop",
    style: {
      "border-style": "dashed",
      "border-color": "#bc8cff",
      "border-width": 3,
    },
  },
  {
    // The edge that closes the cycle (loop-back): curved, labelled "loop".
    selector: "edge.loop-edge",
    style: {
      "line-color": "#bc8cff",
      "target-arrow-color": "#bc8cff",
      "line-style": "dashed",
      "curve-style": "unbundled-bezier",
      "control-point-distances": [-55],
      "control-point-weights": [0.5],
      label: "loop",
      "font-size": "9px",
      color: "#bc8cff",
      "text-outline-color": "#0d1117",
      "text-outline-width": 2,
      "z-index": 9,
    },
  },
];

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

  // (Re)build the cytoscape instance when the graph structure changes.
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
      style: stylesheet,
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
  }, [graph]);

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
