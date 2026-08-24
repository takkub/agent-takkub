# Task Complexity Classifier v2

Current heuristic is Stage 1 only.

Stage 2 should use:
- estimated impacted files
- modules/services crossed
- Graft/project topology
- API/schema/migration signals
- security/payment/auth/infra risk
- history/knowledge need
- UI/design dependency
- multi-agent need
- production/rollback impact

Score suggestion:
files 0-3
modules 0-3
API/schema 0-3
risk 0-4
history 0-2
design 0-2
multi-agent 0-2
prod/rollback 0-3

0-4 small, 5-9 medium, 10+ large.

Hard override:
auth/security/payment/migration/destructive/prod/infra => never SMALL.

Output:
TaskComplexity(size, score, confidence, reasons, risk_flags, estimated_files, estimated_modules)
