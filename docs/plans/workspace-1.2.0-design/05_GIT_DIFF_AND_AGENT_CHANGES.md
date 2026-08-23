# Git Changes & Agent Awareness

Explorer section:
```text
CHANGES 4
M src/app/page.tsx
M src/components/Header.tsx
A src/components/Hero.tsx
D src/legacy.tsx
```

Click -> Monaco diff view.
Use one clearly labeled baseline policy in V1.

Agent attribution may be shown only when cockpit events provide reliable provenance.
Never guess role attribution from Git alone.

Ask Agent payload:
- project id,
- relative path,
- selected line range/text (bounded),
- user request,
- current dirty state.

Do not inject whole file when selection/reference is enough. Use Graft for structural follow-up.
