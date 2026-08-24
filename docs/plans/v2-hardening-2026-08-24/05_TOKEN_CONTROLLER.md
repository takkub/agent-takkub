# Dynamic Token Controller

Budget considers:
- model context window
- task complexity
- source relevance
- already-read files
- previous turns
- expected output
- retry/rework history

Targets:
Small 2k-4k injected
Medium 4k-8k
Large 6k-12k initial, expand in stages

Rules:
- budget is ceiling, not quota
- never fill unused budget
- top-K retrieval + relevance threshold
- count tokens per source
- measure tokens per accepted task, including rework
