You are auditing one step of a multi-agent system for fact fabrication.

Below is the OUTPUT produced by the agent named `<<AGENT_NAME>>`, which is
suspected of being the point where quality broke. Extract the 3 to 5 most
concrete, checkable factual claims it asserts (specific values, names, numbers,
prices, dates, entities). Prefer claims that, if fabricated, would silently
propagate to downstream agents.

Extract claims about the DELIVERABLE the pipeline is producing — the items,
prices, dates, names, quantities that the end user will receive. NEVER extract
process/QA meta-statements (rule counts, style-check percentages, block or
paragraph references, "passed 37 rules" and similar): those describe the
pipeline talking about itself, not the content, and are useless as propagation
evidence.

A claim must carry a CONCRETE value (a number, price, date, quantity, or a
specific named entity). Do NOT extract outline headings or promises of content
("detailed price breakdown in Kč", "delivery conditions section") — a heading
propagating downstream proves nothing about the content existing, and matching
it produces evidence that contradicts a missing-content verdict. If the output
contains only structure and promises with no concrete values, return an empty
list.

SUSPECT OUTPUT:
---
<<NODE_OUTPUT>>
---

Respond with a single JSON object and nothing else:

{"claims": ["<claim 1>", "<claim 2>", "<claim 3>"]}

Each claim should be a short verbatim-ish phrase that could be searched for in
another agent's output.
