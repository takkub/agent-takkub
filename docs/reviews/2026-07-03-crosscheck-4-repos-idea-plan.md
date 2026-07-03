# Cross-check: 4 repos idea plan for agent-takkub

## 1. Worktree + self-correction: token/effort จริงไหม

- **Worktree isolation ไม่ใช่ token~0 จริงใน effort รวม**: token ต่อ pane อาจไม่เพิ่มมาก แต่ engineering cost สูง เพราะปัจจุบัน fan-out คือ `role#N` ใน project namespace เดียว, cwd/session/transcript/token-meter/AGENTS.md/memory อิง cwd หลายจุด (`spawn_engine`, `config`, `token_meter`, `chatlog_scanner`). ถ้าเปลี่ยนเป็น worktree ต้องทำ mapping `main repo -> worktree cwd -> project paths` ให้ทุก provider เหมือนกัน
- **Hidden cost ใหญ่สุดคือ merge-back UX**: ต้องมี diff preview, conflict detection, dirty-tree check, untracked file handling, generated/ignored artifact policy, และ “Lead-propose user confirm” จริง ไม่ใช่แค่ `git worktree add` แล้วปล่อยให้ Lead merge เอง
- **Non-git / partial-git projects เป็น blocker**: agent-takkub รองรับ project paths ทั่วไป; worktree ใช้ไม่ได้กับ zip/tarball/non-git, subdir ที่ไม่ใช่ repo root, mono-repo หลาย path, หรือ repo ที่มี local dirty state หนาแน่น ต้อง fallback เป็น cwd เดิม
- **ผู้ใช้ไม่เห็นงานใน main tree เป็น UX debt**: pane title/click paths/token meter จะชี้ worktree; user/Lead อาจเปิดไฟล์หลักแล้วไม่เห็นงาน ต้องมี status chip/label “isolated worktree” + command/link เปิด diff กลับ main
- **Self-correction memory ถูกกว่ากว่า worktree แต่ไม่ฟรี**: มี `role_memory` อยู่แล้วและมี cap/dedupe 16KB/120 entries + inline tail 200 lines จึงต่อยอดได้ง่าย แต่ auto-capture “user correction -> Lead project rule” ต้องมี classifier, dedupe, scope, expiry, และ review/approve surface ไม่งั้น memory บวมและบันทึกคำบ่น/กรณีเฉพาะเป็น rule ถาวร
- **ระวัง rule ขัดกันข้าม role**: role memory ปัจจุบันเป็น per-role; Lead-level correction จะ override ทุก role ได้ง่าย ต้องเก็บ provenance เช่น date, source snippet, project, role, confidence และมี prune/disable

## 2. สิ่งที่ Lead ตัดออก ถูกไหม / ควรเก็บอะไร

- **ตัด Electron rewrite ถูก**: PyQt/ConPTY/session lifecycle ตอนนี้มี bug guards เยอะ การ rewrite จะเสีย regression budget มาก
- **ตัด agent-adapter 23 ถูก**: product positioning เป็น Claude-first + codex/gemini cross-check; adapter layer จะเพิ่ม auth/provider semantics และ failure modes มากกว่า ROI
- **ตัด SQLite/CDC ถูกในตอนนี้**: state สำคัญยังเป็น JSON/runtime files และ Qt event loop; DB จะเพิ่ม migration/locking/backup โดยยังไม่แก้ routing หลัก
- **ตัด skill-tree 200K ถูก**: ไม่เข้ากับ lean pane/token policy; แต่ควรเก็บแค่ idea “planner picks tiny relevant capabilities lazily” ไม่ใช่ corpus
- **feedback routing ควรขึ้น Tier2 ก่อน worktree หรืออย่างน้อยคู่กับ self-correct**: โค้ดมี `pipeline`, `auto_chain`, `verify`, `done_notice` อยู่แล้ว การ route CI/test fail/conflict กลับ role เดิมเป็น extension ของ completion/verification loop และ ROI ชัดกว่า worktree merge-back ในระยะสั้น
- **AgentSkillOS DAG strategy ใช้ได้แบบเล็กกับ `routing_planner`/pipeline templates**: ไม่ควรทำ generic DAG engine แต่ควรเพิ่ม typed route graph เล็ก ๆ: implement -> verify -> failure classify -> reassign same role/qa/devops/reviewer. ใช้ rule table + tests แบบ `routing_planner.py` จะเข้ากับ codebase กว่า LLM-retrieval DAG
- **visual canvas/cross-tool export ตัดถูก**: ไม่ใช่ปัญหา token/quality หลักของ cockpit ตอนนี้

## 3. ลำดับ Tier/ROI ที่เสนอ

- **Tier1: ui-ux-pro-max scoped skill เหมาะ**: `pane_tools_policy.py` มี per-role plugin allowlist อยู่แล้ว และ plugin installer ก็แยก pane-loaded/hook-heavy ได้ เหมาะกับ designer/critic/frontend เฉพาะงาน design
- **Tier2 ควรเป็น self-correction + feedback routing ก่อน worktree**: self-correction ต่อจาก `role_memory` ได้เร็ว แต่ต้องเริ่มแบบ “suggested memory patch” ให้ Lead/user เห็น ไม่ใช่ auto-write rule ถาวรเงียบ ๆ; feedback routing ต่อจาก verify/pipeline มี blast radius จำกัดและแก้ loop จริง
- **Worktree isolation ควรเป็น Tier2.5/Tier3 pilot เฉพาะ Multi-mode fan-out ที่ repo clean และ git root เดียว**: ทำ opt-in, feature flag, cap แค่ 2-4 shards, no auto-merge; output เป็น patch/diff proposal ให้ Lead รวมเองก่อน
- **ลำดับที่ผมแนะนำ**: (1) UI skill scoped policy, (2) feedback routing MVP for verify/CI fail -> role reassign, (3) self-correction suggested-rule flow + curation, (4) worktree pilot with merge preview, (5) broader worktree automation

## 4. Blind spots

- **Security/trust boundary ของ memory**: user correction อาจมี secrets, private URLs, หรืออารมณ์ชั่วคราว; ต้อง redact และให้เห็นก่อน persist
- **Prompt precedence drift**: Project `CLAUDE.md`, Lead context, teammate role prompt, role memory, AGENTS.md ของ codex/gemini อาจขัดกัน ต้องมี precedence rule ชัดก่อนเพิ่ม memory layer
- **Windows worktree path length / file watcher / dev server ports**: multiple worktrees ทำให้ node_modules/install/cache/dev server ports ซ้ำและหนัก disk; ถ้าไม่กำหนด dependency strategy จะช้าและเปลืองกว่า token ที่ประหยัด
- **Provider parity**: Claude, Codex, Gemini branches spawn ต่างกัน (`AGENTS.md`, sandbox, model/provider fallback). Worktree/memory/routing ต้อง test ทั้ง 3 ไม่ใช่แค่ Claude
- **Observability**: ถ้าเพิ่ม auto-route/auto-fix ต้องมี event log/status surface ว่าใครถูก route เพราะอะไร มิฉะนั้น Lead debug ยาก
- **Evaluation metric ยังไม่ชัด**: ควรวัด wall-clock, correction count, rework rate, merge conflicts, pane crash/respawn, token per completed task ก่อนประกาศว่า feature “คุ้ม”

