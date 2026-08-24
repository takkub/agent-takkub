# v1-like vs Automatic vs Deep Benchmark

Fixed workload suite:

Small: label rename, spacing, one-line bug
Medium: 3-file refactor, API+UI change, cross-module bug
Large: new feature, schema/API, design+frontend workflow

Run:
A gate disabled / legacy-like
B Automatic
C Deep

Compare:
context tokens
total tokens
elapsed time
tool calls
first-pass correctness
retries
QA outcome

Primary metric: tokens per accepted task.
