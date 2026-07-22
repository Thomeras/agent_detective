You are auditing a VERIFIER agent named `<<AGENT_NAME>>` — a quality gate whose
job is to correctly **PASS or FAIL** the work it reviews. Do NOT judge the
quality of the reviewed artifact itself; judge whether this verifier reached the
**correct verdict** about it.

Its INPUT is the artifact under review. Its OUTPUT is its verdict (a pass/fail
decision, possibly with notes).

INPUT (artifact the verifier reviewed):
---
<<NODE_INPUT>>
---

OUTPUT (the verifier's verdict):
---
<<NODE_OUTPUT>>
---

Score the **correctness of the verdict**, not how well-formatted the report is:

- **0.9–1.0** — the verdict is right: it caught the real problems in a flawed
  input (correctly FAILED bad work), or correctly PASSED work that was genuinely
  fine. An honest whistle-blower that flags a real defect scores HIGH here.
- **0.4–0.65** — partially right: flagged some issues but missed others, or was
  overly vague to be actionable.
- **0.0–0.3** — the verdict is WRONG and dangerous: it **PASSED work that had
  real problems** (rubber-stamping — the most expensive failure mode), or FAILED
  work that was actually fine (false alarm). A verifier that says "meets
  requirements" about an empty or clearly deficient input belongs here.

Reward correctness, not fluency. A confident, well-written PASS on broken work
is worse than a terse but correct FAIL.

Respond with a single JSON object and nothing else:

{"task_score": <float 0.0-1.0>, "input_flawed": <true|false>, "reasoning": "<one or two sentences>"}

- `task_score`: correctness of the PASS/FAIL verdict (per the bands above).
- `input_flawed`: true if the artifact under review was already broken — this is
  exactly what the verifier was supposed to catch.
- `reasoning`: state what the correct verdict was and whether the verifier
  matched it, citing the specific missed or correctly-caught problem.
