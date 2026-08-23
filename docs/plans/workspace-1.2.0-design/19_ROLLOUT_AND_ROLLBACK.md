# Rollout & Rollback

Staged exposure:
1. Explorer default-on after stable.
2. Editor beta.
3. Preview beta.
4. Design workflow beta.
5. External integrations individually enabled.

Do not reuse V2 migration flags for unrelated workspace features.
Persist only small UI state (explorer width/collapsed, optional editor tab/preview target). Do not persist unsaved buffers unless explicitly designed.

Rollback must not change project files except explicit user saves. Disabling workspace features must leave Lead/agents, external file open, Graft, Brain/Conversation, storage/migration intact.
