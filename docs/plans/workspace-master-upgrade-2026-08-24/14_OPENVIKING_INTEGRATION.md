# OpenViking Integration Plan

OpenViking is optional external Context Database / Knowledge Retrieval Engine.

## Boundary

Takkub remains control plane:
- scope
- trust
- user/project identity
- context budget
- provider/model policy
- final prompt/context composition

OpenViking:
- resources/docs
- knowledge index
- semantic recursive retrieval
- L0/L1/L2 loading
- retrieval traces

## Deployment

Preferred:
```text
Takkub (MIT)
  |
HTTP/MCP
  |
OpenViking sidecar/service (AGPL)
```

Do not vendor OpenViking source into Takkub.

## Config

```text
TAKKUB_OPENVIKING_ENABLED=0
TAKKUB_OPENVIKING_MODE=shadow|read|hybrid
TAKKUB_OPENVIKING_URL=http://127.0.0.1:...
TAKKUB_OPENVIKING_API_KEY=...
```

## Rollout

A. health/version adapter
B. shadow retrieval only
C. compare native Brain/resource retrieval vs OV
D. merge OV resource results into Context Builder
E. Obsidian/resource indexing
F. optional selected dual-write only if later justified

Operational memory stays Takkub Brain.

## Compatibility
Pin tested OpenViking release/capabilities.
Do not hard-code mutable `viking://` URI semantics as canonical Takkub IDs.
