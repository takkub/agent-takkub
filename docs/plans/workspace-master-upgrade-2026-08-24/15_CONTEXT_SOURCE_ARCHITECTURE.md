# Context Source Architecture

Recommended abstraction:

```text
core/context_sources/
  brain_source.py
  conversation_source.py
  openviking_source.py
  resource_source.py
```

Graft may remain tool-driven instead of being forced into generic retrieval.

Context Builder responsibilities:
- task query
- project
- role/shard
- trust
- scope
- provider/model
- token budget
- dedup
- provenance
- source priority

Suggested output trace:
```text
Context:
- Brain: 4 records / 950 tokens
- Conversation: 1 summary / 600 tokens
- OpenViking: 3 resources / 1800 tokens
- Graft: 2 structural snippets / 700 tokens
Total: 4050 / budget 6000
```

This trace should be available to doctor/debug UI.
