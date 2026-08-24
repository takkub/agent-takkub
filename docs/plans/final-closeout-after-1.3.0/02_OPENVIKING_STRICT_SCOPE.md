# OpenViking Strict Project Scope

Every indexed resource must have:
- workspace_id
- project_id
- source
- kind
- resource_id/knowledge_id
- trust
- updated_at

Default project query allows:
- exact project_id
- explicitly GLOBAL curated resources

Reject:
- other project_id
- missing/invalid project metadata unless explicitly global

Defense in depth:
1. OpenViking-side filter if capability exists
2. Takkub adapter metadata filter
3. Context Builder final scope validation

Tests:
A cannot retrieve B
B cannot retrieve A
Global can appear for both
Missing project metadata fails closed
