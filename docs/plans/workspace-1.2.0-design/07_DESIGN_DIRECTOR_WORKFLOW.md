# Design Director Workflow

Problem: generic AI-looking UI from unconstrained generation.

Designer/design-director:
- understand product/user,
- inspect current UI,
- retrieve references,
- define visual direction,
- typography/spacing/density/hierarchy,
- publish design artifact/spec,
- does not own production code by default.

Design reviewer/critic:
- inspect screenshot/live preview,
- compare to direction,
- accessibility/usability/consistency,
- detect generic AI clichés,
- approve/revise findings.

Flow:
`User -> Lead -> Designer -> references/design system -> artifact -> Preview -> Approve/Revise -> Frontend -> Live Preview -> Reviewer -> QA`

Default anti-AI rules: avoid unjustified gradients, glass, giant hero text, every section as card, rounded-2xl everywhere, random glow/shadow, icon-in-circle repetition, purple AI theme, invented KPIs.
Require hierarchy, deliberate type, consistent spacing, component reuse, reference rationale, responsive intent, visual review.
