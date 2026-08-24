# Git Changes / Diff Final Spec

## Rename-aware model

```python
FileChange(
  path="new/path.ts",
  old_path="old/path.ts",
  status="R",
  repo_root="..."
)
```

## Diff rules
M: HEAD:path -> current:path
A: empty -> current:path
D: HEAD:path -> empty
R: HEAD:old_path -> current:new_path

## Multi-root
Resolve each configured root to git top-level.
Deduplicate roots belonging to same repo.
Run one debounced GitChangesService per distinct repo.

UI:
```text
CHANGES
  web (2)
  api (4)
```

If one repo only, retain compact view.

## Ignore
Prefer Git APIs/commands over handwritten approximation.
