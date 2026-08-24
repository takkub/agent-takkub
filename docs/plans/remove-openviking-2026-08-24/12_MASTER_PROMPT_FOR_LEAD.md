# MASTER PROMPT — REMOVE OPENVIKING

Target `takkub/agent-takkub`.
Baseline observed: v1.5.0, HEAD `250982b1d52816d9eb7d8041bf1fafba978a46d8`.

FIRST inspect CURRENT main/version/issues and grep:
`openviking`, `TAKKUB_OPENVIKING`, `OPENVIKING_API_KEY`, `OpenVikingSource`, `merge_openviking`, `ov managed`.
Build dependency map. Mark every pack item DONE/PARTIAL/MISSING/OBSOLETE.

Decision: OpenViking is no longer part of Takkub. REMOVE it, do not merely disable it.

Preserve:
Conversation, Brain, Graft, Obsidian/local resources, relevant files, Context Builder, context/token gate, Design Tools, Remote.

Remove:
managed runtime package, HTTP adapter/source, OpenViking context paths, Settings/Wizard, boot lifecycle, CLI, doctor, secrets/config/env/state, OpenViking-only tests/current docs.

Critical:
If generic local Resource/index/trace logic is in OpenViking-named code, extract/rename it before deletion.

Migration:
do not silently delete old v1.5.0 runtime data; never kill external process; provide safe explicit cleanup if practical.

Verify:
no OpenViking import/network/process on boot or assign; no UI/CLI product surface; core knowledge stack works; full CI; Windows runtime; depgraph; before/after startup+RAM; #362 untouched.

Final report exact deleted/changed files, preserved architecture, cleanup behavior, performance delta, CI, risks, and confirmation OpenViking is gone from active product.
