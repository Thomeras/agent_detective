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
];

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
