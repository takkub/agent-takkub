# App-wide Resource Governor

Manage:
provider panes, Editor/Preview WebEngine, Knowledge Studio,
OpenViking, indexers, Git workers, retrieval, QA/subagents.

Under pressure:
- delay indexing
- reduce retrieval concurrency
- pause invisible preview refresh
- avoid optional WebView creation
- cap workers
- suspend optional indexing

Priority:
active work > save/edit > terminal delivery > active preview > retrieval > indexing.

Never kill active agent work just to hit RAM target.
