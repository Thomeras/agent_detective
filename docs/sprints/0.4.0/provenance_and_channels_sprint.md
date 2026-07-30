# Sprint P0-P7 — skóre umí doložit, čím bylo změřeno

**Hotovo.** Tenhle soubor je záznam provedeného sprintu, ne zadání. Vznikl
z forenzní kontroly runu `7fa6f73d-f071-514b-93cc-dc5d662df5ce` — ze stejného
runu jako FIX-9A/9B/10, ale z jiné otázky: ne „je to číslo správně", nýbrž
„umí to číslo doložit, čím bylo změřeno".

Odpověď byla ne. Skóre 0.4 od `gpt-4o-mini` bylo v databázi nerozlišitelné od
0.4 od frontier modelu, protože `judge_model` žil jen v configu a v odchozím
HTTP requestu. `/calibration`, `/agents/leaderboard` i version-diff tak
porovnávaly napříč nesouměřitelnými měřeními a neměly jak to poznat.

## Co se změnilo

### P0/P7 — každé skóre pojmenuje svůj přístroj

Migrace **0014**: `runs.{judge_model, score_weights}`,
`blame_reports.{judge_model, cost_coverage}`, `tier1_verdicts.judge_model`
plus index na `(judge_prompt_hash, judge_model)`. Prompt fingerprint už
persistovaný byl (0009) — model k němu chyběl, a `routers/calibration.py` to
měl přiznané v docstringu jako známou limitaci. Limitace i docstring jsou pryč.

`Settings.describe_judge()` říká při startu workeru, čím se měří:

```
judge: model=mock base_url=http://mock-llm:8080/v1 kind=MOCK (verdicts are canned)
judge: model=openai/gpt-4o-mini base_url=https://openrouter.ai/api/v1 kind=real model
```

Dřív to šlo zjistit jen přes `docker exec env`.

Dál: `POST /v1/traces` na API portu proxuje na ingest (uživatel řekne „API na
8000", ingest poslouchá na 8001, a osmitisícovka o něm neměla ani zmínku —
POST tiše selhal). `JUDGE_MAX_SPEND_USD` s akumulací spendu na analýzu; při
vyčerpání se přestane volat a uzly padnou na neoskórované, ne na spadlou
analýzu. Neznámá cena zůstává `null`, nikdy `$0`.

### P1 — přenormalizace váhy přestala být tichá

`composite_score` vrací `CompositeScore(score, unscored_reason,
effective_weights)`. Matematika je beze změny — mění se to, že blend teď řekne,
čím vážil:

```
components {schema: null, judge: 0.70, heuristics: 0.75}
-> 0.7136, effective_weights {judge: 0.727, heuristics: 0.273}
```

Chybějící kanál dřív tiše předal svou váhu těm zbývajícím a uživatel četl
„weighted mean over three independent components". Váhy jdou do
`runs.score_weights`, do CLI (`1 of 3 channels (effective weight: …)`) i do UI.

V enginu chybějící kanál nově **snižuje** confidence
(`single_channel_penalty`), místo aby zvyšoval autoritu toho zbývajícího.
`cut_point` si typ ponechal — každý slabší report type v `REPORT_TYPE_CAP`
tvrdí něco jiného (že nikdo neselhal, že příčina je vně, že selhal
verifikátor) a přepnout na něj by tvrdilo, co důkazy neříkají. Místo toho má
strop `SINGLE_CHANNEL_CUT_POINT_CAP = 0.7`: pojmenovat jeden origin na slovo
jednoho přístroje nesmí dosáhnout jistoty, kterou mají dva shodné kanály.

### P2 — `output_contracts` přestala být mrtvá tabulka

Měla nula řádků po `docker compose up`, žádný write path (jen čtenáře), žádné
CLI a jeden řádek v `architecture.md`. Přibyly `GET/POST /contracts`,
`GET /contracts/suggest`, `detective contracts {list,register,suggest}`
a obrazovka **Contracts** ve web UI.

`suggest` odvodí schéma z uložených payloadů daného agenta, konzervativně:
klíč je `required`, jen když je ve **všech** použitelných vzorcích (min. 5,
tvrdý floor 3), typy jsou pozorované, žádný enum/format/rozsah se nevymýšlí.
Když se vzorky neshodnou, vrátí odmítnutí s důvodem — **permisivní schéma je
horší než žádné**, vyrobilo by kanál s vahou 0.35 z ničeho.

To je zároveň důvod, proč P1 kousalo: bez kontraktů je `schema` null na každém
uzlu každé čisté instalace, takže judge neskákal na 72,7 % ve výjimce, ale
v defaultu.

### P3 — deterministická fakta jdou judgeovi jako text

Symptom („judge trestá sběrače za prázdný výnos") zavřel už FIX-9A gatem
v `scoring.py`. Otevřená zůstávala konstruktivní půlka: judge hádal to, co
vedlejší kanál věděl jistě. Prompt má nově blok `DETERMINISTIC FACTS` —
přepsaný parametr kontraktu, neviditelný artefakt, každý vystřelený check —
a k tomu fakta o sourozencích:

> `3 of 7 other node(s) in this run also produced a well-formed output carrying no records.`

Sám o sobě vypadá sběrač, co nic nenašel, nedbale. Vedle čtyř sourozenců, co
taky nic nenašli, byl suchý zdroj.

### P4 — jde říct „tenhle uzel je deterministický"

Role se odvozovala výhradně ze jména (`PLANNER_PREFIXES`), takže `plan_node`
bez jediného LLM callu dostal PLANNER rubriku. Nově span atribut
`agent_detective.node_kind` (migrace **0015**) skrz celý řetěz SDK → mapper →
ingest → worker; `deterministic` a `tool` přeskočí judge úplně.

LangGraph auto-detekce je **opt-in** (`detect_deterministic=False`): „nevystřelil
LangChain callback" poctivě neznamená „neproběhl model call" — kdo volá provider
SDK přímo, dostal by uzel označený jako deterministický a psal by prózu.
Explicitní `node_kinds={"plan_node": "deterministic"}` je vždycky nadřazený.

### P5 — confidence zná tvar grafu

`classify_topology` počítala `depth`, `scc_count` i `articulation_points` už
dřív, ale konzumovalo se to na jednom místě a jen pro `disconnected`. Nově je
archetyp `chain` a **penalta škáluje s délkou**:

```
depth  2 -> x1.000   (žádný vnitřní uzel, není co zamlčet)
depth  3 -> x0.950
depth  6 -> x0.900
depth 18 -> x0.800   (saturace, chain_full_penalty_depth)
```

Náběh je `sqrt`, protože rozlišovací schopnost padá nejrychleji na začátku:
skok z 1 kandidáta na 4 stojí víc jistoty než ze 13 na 16. Plochá penalta by
3-krokový pipeline trestala stejně jako 18-krokový řetěz, což je plošná
rekalibrace každého reportu v produktu, ne měření.

Remízy: shoda šesti uzlů na 0.4 se dřív rozsekla topologickým pořadím a
pojmenovala prvního. Tie-break byl už dřív stabilizovaný (chronologický klíč,
ne pořadí spanů), ale pořád se **rozsekl**. Nově se hlásí jako kandidátská sada
v `Evidence.hypotheses` s explicitním `unresolved` zbytkem.

Coarse judge (hrubost, kvůli které remízy vůbec vznikají) je řešený z druhé
strany: rubrika v `judge.md` žádá dvě desetinná místa a zakazuje spadnout na
kulaté kotvy 0.40/0.70/0.80.

### P6 — agregáty nesou pokrytí

`Evidence.cost_coverage` = `{"priced": n, "total": m}`. CLI vypíše
`$0.0732 (6/28 runs priced, lower bound)`. V UI přestal `IncidentInbox` sčítat
`downstream_cost_usd ?? 0` — neoceněný incident není zadarmo, je neznámý.

Stejná lež žila na Leaderboardu a GraphListu, kde je horší: `total_cost_usd` je
`SUM(cost_usd)` a SQL SUM ignoruje NULLy, takže neinstrumentovaný run
nepřispěje ničím a součet se tváří jako úplný. Přibyl `priced_run_count`
(korelovaný poddotaz na grafech, `COUNT(cost_usd)` na leaderboardu).

## Šest mezer nalezených při vlastní kontrole

Po dokončení P1-P7 vyšla kontrola proti původnímu zadání se šesti nálezy — pět
nedodělků a jedna odchylka. Všech šest zavřeno:

1. **P2 nemělo UI vůbec**, jen API a CLI. Chyba v koordinaci: agentovi na
   kontraktech bylo zakázáno sahat na `web/`, aby nekolidoval s agentem na UI,
   a ten kus se pak nepředal nikomu. → obrazovka `Contracts.tsx`.
2. **`?? 0` žilo dál** na Leaderboardu a GraphListu. → `priced_run_count`.
3. **Vyčerpaný rozpočet byl nerozlišitelný od nedostupného judge** (obojí
   `insufficient_components`). → `judge_budget_exhausted`.
4. **P1 se neudělalo dle zadání** — `cut_point` nebyl podmíněný diverzitou
   kanálů, jen penalizovaný. → `SINGLE_CHANNEL_CUT_POINT_CAP`.
5. **P3 znalo fakta jen o sobě**, ne o sourozencích. → `peer_facts`.
6. **Hrubost judge se neřešila vůbec.** → dvě desetinná místa v rubrice.

## Co zůstalo vědomě otevřené

- **LangGraph auto-detekce `node_kind` je opt-in**, ne default. Viz P4 výše —
  není to nedodělek, je to hranice toho, co jde tvrdit poctivě.
- **Rekalibrace confidence je reálná.** Chain penalta se dotkne každého
  pipeline grafu hloubky 3+. U krátkých je mírná (×0.95), ale prahy a alerty
  postavené na starých číslech se posunou.
- **Judge granularita je řešená promptem, ne strukturálně.** Model může
  instrukci ignorovat; pokud remízy přetrvají, další krok je vynutit rozlišení
  ve validaci odpovědi, ne v textu.

## Testy

D2 (contract). 20 nových testů: 6 engine (chain náběh, saturace, hranice bez
vnitřního uzlu, diversity cap, penalta za neúplné kanály), 7 worker
(přenormalizace, deklarovaný `node_kind`, model u judged složky, rozpočet),
7 inference kontraktů (odmítnutí pod floor, `required` napříč všemi vzorky,
neshoda typů, nečitelný payload jako nepoužitelný vzorek).

Šest testů zamykajících starý tvar opraveno — dva na `composite_score` jako
dvojici, dva na tvar API odpovědi, dva na confidence hodnoty před chain
penaltou (0.12 → 0.114, 0.88 → 0.8178) a strážce tabulky poznámek.

Celkem **1389 prošlo, 0 spadlo**; `tsc --noEmit` čistý.
