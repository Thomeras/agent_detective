# Sprinty pro release 0.4.0

Rozpad `docs/release-0.4.0.md` na samostatná zadání pro `/sprint-driven-dev` —
jeden soubor na jeden problém. **Feature 2 (nové UI, `web/`) tu není: je hotová.**

**FIX-9A / FIX-9B / FIX-10 z release checklistu nepocházejí.** Vznikly z živého
runu `7fa6f73d-f071-514b-93cc-dc5d662df5ce` (napojení rozsáhlého research
systému): judge oskóroval osm sběračů jako vadné za to, že zdroj neměl data, a
ta deflovaná čísla ohnula i headline verdikt. Detaily v samotných souborech.

**P0-P7 taky ne** — viz `provenance_and_channels_sprint.md`. Ze stejného runu,
ale z jiné otázky: ne „je to číslo správně", nýbrž „umí to číslo doložit, čím
bylo změřeno". Odpověď byla ne, a celý jeden ze tří kanálů byl navíc defaultně
nedostupný. Hotové, včetně šesti mezer nalezených při vlastní kontrole.

Každý soubor je hotový prompt: vlož ho celý jako zadání sprintu. Uvnitř má cíl,
současný stav s odkazy `soubor:řádek`, zadání, acceptance, invarianty, co je mimo
scope a co patří až do wrap-upu.

## Přehled

| Sprint | Soubor | Oblast | Závisí na |
|---|---|---|---|
| FIX-1A | `1a_ingest_deferred_delegation_fix_sprint_prompt.md` | ingest + otel_mapper | — |
| FIX-5 | `5_late_spans_after_finalization_fix_sprint_prompt.md` | ingest (+ migrace) | FIX-1A |
| FIX-6B | `6b_quiescence_visibility_fix_sprint_prompt.md` | ingest + CLI doctor | FIX-5 |
| FIX-3A | `3a_sdk_graph_identity_fix_sprint_prompt.md` | detective_sdk | — |
| FIX-1B | `1b_sdk_external_parent_span_fix_sprint_prompt.md` | detective_sdk | FIX-3A |
| FIX-3B | `3b_sdk_root_correlation_attr_fix_sprint_prompt.md` | detective_sdk | FIX-3A |
| FEAT-7A | `7a_sdk_event_driven_steps_fix_sprint_prompt.md` | detective_sdk | FIX-3A/1B/3B (stejný soubor) |
| FEAT-7B | `7b_langgraph_adapter_fix_sprint_prompt.md` | detective_sdk (nový modul) | FEAT-7A |
| FEAT-7C | `7c_langchain_cost_capture_fix_sprint_prompt.md` | detective_sdk (nový modul) | FEAT-7A, FEAT-7B |
| FIX-4 | `4_node_not_analyzed_state_fix_sprint_prompt.md` | API + CLI render | — |
| FIX-6A | `6a_deterministic_only_mode_fix_sprint_prompt.md` | CLI doctor/render | FIX-4 |
| FIX-9A | `9a_zero_result_set_not_a_defect_fix_sprint_prompt.md` | worker scoring + blame_engine | koordinovat s FIX-4 |
| FIX-9B | `9b_judge_role_collector_and_structured_plan_fix_sprint_prompt.md` | roles + judge prompt | FIX-9A |
| FIX-10 | `10_findings_export_score_provenance_fix_sprint_prompt.md` | web (export findings) | FIX-9A |
| FIX-DOCS | `8_docs_pipelines_and_scoring_fix_sprint_prompt.md` | dokumentace | všechno výše |
| P0-P7 | `provenance_and_channels_sprint.md` (záznam, ne zadání) | napříč: migrace 0014/0015, engine, worker, API, CLI, web | FIX-9A |

## Čtyři nezávislé stopy (můžou běžet paralelně, uvnitř sériově)

1. **Ingest:** FIX-1A → FIX-5 → FIX-6B
   Sdílejí `services/ingest/ingest/{main,repository,finalizer,config}.py`.
2. **SDK:** FIX-3A → FIX-1B → FIX-3B → FEAT-7A → FEAT-7B → FEAT-7C
   Prvních pět sahá na `packages/detective_sdk/detective_sdk/tracing.py`, hlavně
   na `Run.__init__` a `build_payload`.
3. **Report/API:** FIX-4 → FIX-6A
   Sdílejí `packages/detective_cli/detective_cli/render.py`.
4. **Skórování:** FIX-9A → FIX-9B → FIX-10
   Prvních dva sahají na `services/worker/worker/scoring.py`.

Napříč stopami kolize nejsou — kromě `doctor.py`, kterého se dotýká FIX-6B
(stopa 1) i FIX-6A (stopa 3). Pusť je za sebou, ne současně.

**FIX-9A × FIX-4 se potkávají na výčtu `unscored_reason`:** 9A do něj přidává
hodnotu, FIX-4 ho renderuje. Pusť 9A první, nebo hodnotu do FIX-4 rovnou dopiš.

**FIX-DOCS až úplně nakonec** a mimo sprint režim: dokumentuje výslednou podobu
kódu, ne plán.

## Co tady schválně není

- **Feature 2 — nové UI (`web/`).** Hotové. Sprinty se ho nedotýkají; FIX-4 jen
  ověří, že nový stav doteče do API (`unscored_reason` se v UI renderuje jako
  řetězec).
- **Sekce 2-5 release checklistu** (verzování, příprava, release, po releasu).
  To je release proces, ne vývojový sprint — zůstává v `docs/release-0.4.0.md`.
  Verze balíčků se rozhodnou podle toho, co ze sprintů reálně doputuje dovnitř.
