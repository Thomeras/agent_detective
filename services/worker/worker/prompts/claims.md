You are auditing one step of a multi-agent system for fact fabrication.

Below is the OUTPUT produced by the agent named `<<AGENT_NAME>>`, which is
suspected of being the point where quality broke. Extract the 3 to 5 most
concrete, checkable factual claims it asserts (specific values, names, numbers,
prices, dates, entities). Prefer claims that, if fabricated, would silently
propagate to downstream agents.

SUSPECT OUTPUT:
---
<<NODE_OUTPUT>>
---

Respond with a single JSON object and nothing else:

{"claims": ["<claim 1>", "<claim 2>", "<claim 3>"]}

Each claim should be a short verbatim-ish phrase that could be searched for in
another agent's output.
