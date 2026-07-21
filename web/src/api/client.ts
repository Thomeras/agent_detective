// Typed fetch layer for services/api. Endpoints and shapes match section 6.3
// of the build spec and services/api/api/routers/*.

import type {
  AnalyzeResponse,
  GraphDetail,
  GraphListResponse,
  IncidentDetail,
  IncidentListResponse,
  IncidentStatus,
  LeaderboardResponse,
  RunPayloads,
} from "./types";

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = API_BASE_URL.replace(/\/$/, "");
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
  } catch (err) {
    throw new ApiError(0, `Network error contacting API: ${String(err)}`);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // response body was not JSON; keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  listGraphs(limit = 50, offset = 0): Promise<GraphListResponse> {
    return request(`/graphs?limit=${limit}&offset=${offset}`);
  },
  getGraph(graphId: string): Promise<GraphDetail> {
    return request(`/graphs/${graphId}`);
  },
  getRunPayloads(graphId: string, runId: string): Promise<RunPayloads> {
    return request(`/graphs/${graphId}/payloads/${runId}`);
  },
  analyzeGraph(graphId: string): Promise<AnalyzeResponse> {
    return request(`/graphs/${graphId}/analyze`, { method: "POST" });
  },
  listIncidents(limit = 50, offset = 0): Promise<IncidentListResponse> {
    return request(`/incidents?limit=${limit}&offset=${offset}`);
  },
  getIncident(incidentId: number): Promise<IncidentDetail> {
    return request(`/incidents/${incidentId}`);
  },
  patchIncident(incidentId: number, status: IncidentStatus): Promise<IncidentDetail> {
    return request(`/incidents/${incidentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
  },
  leaderboard(): Promise<LeaderboardResponse> {
    return request(`/agents/leaderboard`);
  },
};
