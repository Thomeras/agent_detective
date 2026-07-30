// Screen 4: output contracts — the deterministic schema channel.
//
// With no contract registered, the schema component is null on every node of
// every run, the judge's weight silently renormalizes from 0.40 to 0.727, and
// a single-channel score presents as a three-channel one. The table had no
// write path at all, so the only way to fill it was hand-written SQL. This
// screen is the thirty-second version: pick an agent, read what its own stored
// payloads support, register it.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { ContractSuggestion, LeaderboardAgent } from "../api/types";
import { EmptyState, ErrorState, Loading } from "../components/ui";
import {
  Badge,
  Field,
  Page,
  RecordFields,
  RecordList,
  RecordRow,
  SearchInput,
  Select,
  StatTile,
  Toolbar,
} from "../ui/primitives";
import { useAsync } from "../hooks/useAsync";

function agentNames(agents: LeaderboardAgent[]): string[] {
  const seen = new Set<string>();
  for (const a of agents) if (a.agent_name) seen.add(a.agent_name);
  return [...seen].sort();
}

// The evidence line under a proposal: how much of it the samples actually
// support. Shown whether or not a contract came back — a refusal earns its
// numbers just as much as an acceptance does.
function SampleEvidence({ suggestion }: { suggestion: ContractSuggestion }) {
  const s = suggestion.samples;
  return (
    <div className="stat-row">
      <StatTile label="Runs examined" value={s.runs_examined} />
      <StatTile label="With output" value={s.runs_with_output} />
      <StatTile
        label="Usable samples"
        value={s.usable_samples}
        tone={s.usable_samples >= suggestion.min_samples ? "ok" : "warn"}
        hint={`${suggestion.min_samples} needed`}
      />
      <StatTile label="Failed runs" value={s.failed_runs} />
    </div>
  );
}

export default function Contracts() {
  const [agent, setAgent] = useState<string>("");
  const [q, setQ] = useState("");
  const [suggestion, setSuggestion] = useState<ContractSuggestion | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const contracts = useAsync(() => api.listContracts(), []);
  const agents = useAsync(() => api.leaderboard(), []);

  const rows = useMemo(() => {
    const all = contracts.data?.contracts ?? [];
    const needle = q.trim().toLowerCase();
    if (!needle) return all;
    return all.filter((c) => (c.agent_name ?? "").toLowerCase().includes(needle));
  }, [contracts.data, q]);

  const names = useMemo(() => agentNames(agents.data?.agents ?? []), [agents.data]);
  const covered = useMemo(
    () => new Set((contracts.data?.contracts ?? []).map((c) => c.agent_name ?? "")),
    [contracts.data],
  );
  const uncovered = names.filter((n) => !covered.has(n));

  async function onSuggest() {
    if (!agent) return;
    setBusy(true);
    setNotice(null);
    setFailure(null);
    setSuggestion(null);
    try {
      setSuggestion(await api.suggestContract(agent));
    } catch (err) {
      setFailure(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  }

  // Registers the proposal exactly as it was shown — no field the reader did
  // not see goes to the server.
  async function onRegister() {
    if (!suggestion?.contract) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.registerContract(suggestion.contract);
      setNotice(`Contract registered for ${suggestion.contract.agent_name}.`);
      setSuggestion(null);
      contracts.reload();
    } catch (err) {
      setFailure(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page
      title="Output contracts"
      subtitle="The deterministic schema channel. An agent with no contract is scored on the judge and heuristics alone — the missing channel hands its weight to the ones left, and the blend stops being three-channel."
      actions={
        <button className="btn" onClick={contracts.reload} disabled={contracts.loading}>
          Refresh
        </button>
      }
    >
      {contracts.loading && <Loading label="Loading contracts" />}
      {contracts.error && <ErrorState message={contracts.error} onRetry={contracts.reload} />}

      {!contracts.loading && !contracts.error && (
        <>
          <div className="stat-row">
            <StatTile label="Registered" value={rows.length} />
            <StatTile
              label="Agents without one"
              value={uncovered.length}
              tone={uncovered.length > 0 ? "warn" : "ok"}
              hint={uncovered.length > 0 ? "scored without the schema channel" : undefined}
            />
          </div>

          <Toolbar>
            <Select<string>
              value={agent}
              onChange={setAgent}
              options={[
                { value: "", label: "Select an agent…" },
                ...names.map((n) => ({
                  value: n,
                  label: covered.has(n) ? `${n} (has a contract)` : n,
                })),
              ]}
              title="Agent to derive a contract for"
            />
            <button className="btn" onClick={onSuggest} disabled={!agent || busy}>
              {busy ? "Reading payloads…" : "Derive from stored runs"}
            </button>
            <div className="toolbar-end">
              <SearchInput value={q} onChange={setQ} placeholder="Search registered agents…" />
            </div>
          </Toolbar>

          {notice && <div className="reason-block muted small">{notice}</div>}
          {failure && <ErrorState message={failure} onRetry={onSuggest} />}

          {suggestion && (
            <>
              <SampleEvidence suggestion={suggestion} />

              {suggestion.contract ? (
                <RecordRow tone="ok">
                  <div className="rec-top">
                    <span className="rec-title mono">{suggestion.agent_name}</span>
                    <span className="rec-end">
                      <Badge tone="ok">
                        {suggestion.keys.filter((k) => k.included).length} key(s) proposed
                      </Badge>
                    </span>
                  </div>
                  <RecordFields>
                    <Field label="Required in every sample">
                      {suggestion.keys
                        .filter((k) => k.required && k.included)
                        .map((k) => `${k.key}: ${k.type}`)
                        .join(", ") || "none"}
                    </Field>
                    <Field label="Seen but left out">
                      {suggestion.keys
                        .filter((k) => !k.included)
                        .map((k) => `${k.key} (${k.note ?? k.observed_types.join("|")})`)
                        .join(", ") || "none"}
                    </Field>
                  </RecordFields>
                  <pre className="mono small">
                    {JSON.stringify(suggestion.contract.json_schema, null, 2)}
                  </pre>
                  <div className="rec-top">
                    <button className="btn primary" onClick={onRegister} disabled={busy}>
                      Register this contract
                    </button>
                    <span className="muted small">
                      Derived from {suggestion.samples.usable_samples} usable payload(s); a key is
                      required only when every one of them carried it.
                    </span>
                  </div>
                </RecordRow>
              ) : (
                // A refusal is a result, not an error: a schema that passes
                // everything would manufacture a 0.35-weight channel out of
                // payloads that never agreed on anything.
                <EmptyState
                  title="The stored payloads do not support a contract"
                  hint={suggestion.reason ?? "Not enough agreement between samples."}
                />
              )}
            </>
          )}

          {rows.length === 0 ? (
            <EmptyState
              title="No contracts registered"
              hint="Every node is scored without the schema channel until one is."
            />
          ) : (
            <RecordList>
              {rows.map((c) => (
                <RecordRow key={c.id ?? `${c.agent_name}-${c.agent_version_pattern}`} dense>
                  <div className="rec-top">
                    <span className="rec-title mono">{c.agent_name ?? "(unknown)"}</span>
                    <span className="rec-end">
                      <span className="rec-time mono">{c.agent_version_pattern ?? "*"}</span>
                    </span>
                  </div>
                  <RecordFields>
                    <Field label="Required">
                      {((c.json_schema?.required as string[] | undefined) ?? []).join(", ") ||
                        "none"}
                    </Field>
                    <Field label="Properties">
                      {Object.keys(
                        (c.json_schema?.properties as Record<string, unknown> | undefined) ?? {},
                      ).join(", ") || "none"}
                    </Field>
                  </RecordFields>
                </RecordRow>
              ))}
            </RecordList>
          )}
        </>
      )}
    </Page>
  );
}
