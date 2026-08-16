# Current Baseline Audit

## Baseline

```text
repo: takkub/agent-takkub
branch: main
commit: 0aee262a2b2648b248822e3bb587a49001b14166
version: 1.0.68
```

แผน Brain รอบก่อนอิง snapshot เก่ากว่า baseline นี้ 48 commits

## Current architecture facts ที่ Brain ต้องเคารพ

### 1. assign มี 2 execution modes แล้ว

```text
takkub assign --mode pane
takkub assign --mode subagent
```

`subagent`:
- ไม่มี AgentPane/PTY
- สร้าง task capsule
- ใช้ native child ของ provider เดียวกับ parent
- ปิดงานด้วย `takkub subagent-done`
- ใช้ Task Ledger / wait / inbox / worktree-finalize completion sinks เดิม

ดังนั้น Brain ห้ามออกแบบเฉพาะ pane lifecycle

### 2. Task context ไม่ควรผูกกับ spawn

Pane สามารถมีชีวิตอยู่แล้วถูก assign งานใหม่ได้

ดังนั้น:

```text
spawn-time Brain only = ผิด
```

ต้องมี:

```text
assignment-time Brain Context
```

### 3. มี file-based task handoff อยู่แล้ว

ระบบเดิมมี:

```text
_task_handoff_pointer()
TASK_HANDOFF_THRESHOLD
last_assigned_task_file
```

มันมีไว้แก้ long-paste/delivery ไม่ใช่ semantic resume

เพื่อไม่ให้ชื่อชนกัน Brain ใช้:

```text
ContinuationRecord
```

แทน `HandoffRecord`

### 4. มี DigestFacts แล้ว

`Orchestrator.done()` สร้าง structured facts จาก:
- branch
- commits ahead
- uncommitted
- merge conflicts
- files touched
- issue ref
- report path
- headline

ข้อมูลเหล่านี้วัดโดย cockpit ไม่ใช่ parse จาก agent prose

Brain outcome capture ต้อง reuse object นี้

### 5. Lead Inbox มี adaptive digest/wait/revalidation

Lead notification มี batching/delay/revalidation logic

ดังนั้น:

```text
Lead notice != authoritative completion storage
```

Brain capture ต้องเกิดที่ completion lifecycle boundary ไม่ใช่เมื่อ digest flush

### 6. Token diet เป็น product constraint จริง

ระบบเพิ่งลด role-file staged context ลงอย่างมาก และ root CLAUDE.md ถูกลดให้เหลือเฉพาะ shared rules

Brain context จึงต้อง:
- bounded
- relevant
- pull-on-demand
- ไม่ preload history ทั้งก้อน

### 7. ProviderSpec สำคัญขึ้น

ปัจจุบัน provider capabilities มีผลถึง delivery เช่น:
- supports_agent_file_read
- ready markers
- provider override
- boot diagnostics

Brain ต้องผ่าน existing assignment/delivery composition path
ห้ามสร้าง provider-specific delivery path ใหม่

### 8. Auto-resume มีอยู่แล้ว

Usage-limit park/wake:
- task เดิม
- pane เดิม/continuation ของ execution
- ไม่ใช่ completion

Brain ห้ามสร้าง continuation record ทุก park/wake cycle
