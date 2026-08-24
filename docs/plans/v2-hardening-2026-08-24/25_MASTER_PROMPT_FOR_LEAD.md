# MASTER PROMPT — Takkub v2 Hardening after 1.4.1

Current baseline is already 1.4.1.

Before coding:
1. inspect current HEAD/version/issues
2. inspect context_gate, Context Builder, Settings, OpenViking runtime work
3. mark each plan item DONE/PARTIAL/MISSING/OBSOLETE
4. implement only gaps

Do NOT rebuild:
- strict OpenViking scope
- current context/token gate
- current Knowledge & Design Settings
- #377 URI correlation fix

Priority:
A classifier v2
B adaptive escalation
C dynamic token controller
D explainable trace
E resource governor
F centralized fail-open/circuit breakers
G simple Automatic UX
H retrieval prompt-injection defense
I managed local OpenViking if missing
J benchmark/chaos/soak

Rules:
- current regex/text-length classifier becomes Stage 1 only
- no LLM call just to classify every task
- risk domains never SMALL
- small fast path stays cheap
- no optional retrieval unless justified
- budget is ceiling, not target
- optional service failure degrades
- Brain=operational memory
- Conversation=session
- Graft=code structure
- Obsidian=curated knowledge
- OpenViking=optional retrieval
- Context Builder=final policy
- no local LLM dependency
- do not mix #362

Final report:
HEAD/version
delta matrix
classifier accuracy
adaptive tests
token benchmark
RAM/CPU benchmark
chaos/fail-open evidence
GUI evidence
security tests
OpenViking lifecycle
CI
remaining risks
#362 untouched
