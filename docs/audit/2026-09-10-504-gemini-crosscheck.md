# 504 Design Cross-Check (Gemini)

- **Date:** 2026-09-10
- **Scope:** #504 2.1.0 storage cutover — Cross-check review on migration recovery/undo failure modes.
- **Inputs:** `docs/audit/2026-09-10-504-acceptance-review.md`, `runtime/exports/2026-09-10/agent-takkub/504-round3-faults.jsonl`, `src/agent_takkub/core/migration/promote_v1.py` (`_two_phase_move`, `_restore_removed_source`, `_undo_moved_entries`, `_copy_only_transaction`, `ArchiveV1LegacyStep.rollback`/`_undo_restored_names`).

---

## (A) สาเหตุเชิงโครงสร้างที่ทำให้ undo/recovery path ทำข้อมูลหายมาแล้ว 4 รอบ

1. **Coupling Copy กับ Deletion ใน Transaction เดียวกัน (In-band Move):**
   `_two_phase_move` พยายามทำ "copy-verify แล้วลบ source ทันที" ภายใน execution loop เดียวกัน เมื่อ Phase B (ลบ source) เกิดข้อผิดพลาดหรือ crash กลางทาง ระบบต้องพยายาม "กู้ source คืนจาก dest" (`_restore_removed_source`) ซึ่งเป็นการย้อนทางที่เปราะบาง และเมื่อ `_restore_removed_source` เจอปัญหา I/O กลับกลืน error ด้วย `except OSError: pass` ทำให้ source หายไปจริงแต่ transaction คิดว่าคืนสภาพแล้ว

2. **ขาด Durable Write-Ahead Ledger (WAL) ที่ Commit ก่อน Mutate:**
   Manifest ถูกบันทึกแบบ inline ระหว่างลบ หรือบันทึกหลังลบเสร็จ (`write_json_atomic` ท้ายฟังก์ชัน) หาก process ถูก interrupt หรือการเขียน manifest ล้มเหลว (`manifest_write_promote`, `manifest_write_archive_crash`) สถานะ ownership ของไฟล์จะหลุดจากการรับรู้ทันที และเมื่อรันซ้ำ retry จะไปกวาดไฟล์ที่เหลือจนตีความ ownership ผิด (เช่น `merged_live_home_after_retry` ดึง live home เข้าเป็น promoted แล้วสั่งลบตอน rollback)

3. **Preimage Storage เป็นแบบ Single-slot Mutable Backup:**
   `BackupManager` เก็บ preimage แบบ key เดียว (`latest_backup`) ต่อ step/name เมื่อทำ multi-generation restore (`middle_generation_same_name`, `third_generation_same_name`) การ restore generation ถัดไปจะบันทึกทับ preimage ของ generation ก่อนหน้า ทำให้เมื่อ generation ท้ายๆ ล้มเหลว ตัว rollback ไม่สามารถย้อนกลับไปยัง pre-command state ดั้งเดิมได้ แต่กลับไปดึงไฟล์ generation ระหว่างทางมาแทน

---

## (B) โครงสร้าง Transaction ใหม่เพื่อป้องกัน Defect Class นี้โดยสิ้นเชิง

1. **Never-Delete During Migration (แยก Mutation ออกเป็น 2 Pass เด็ดขาด):**
   - **Pass 1 (Migration / Staging):** ทำเฉพาะ copy-verified เท่านั้น โดย "ห้ามแตะต้องหรือลบ source ใดๆ ทั้งสิ้น" ไม่ว่าจะกรณีใด
   - **Pass 2 (Deferred Garbage Collection / Archival):** ทำงานเฉพาะหลังจากที่ทั้ง Ladder, Schema, และ Target Integrity ผ่านการ Validate ครบ 100% แล้วเท่านั้น หากติดขัดใน Pass 1 ให้หยุดและทิ้ง target ชั่วคราวได้ทันทีโดยไม่ต้องมี recovery logic ซับซ้อน เพราะ source ตัวจริงยังสมบูรณ์ 100% เสมอ
2. **Immutable Write-Ahead Ledger (WAL) ก่อน Mutate:**
   - คำนวณ file list, sha256 checksums, และ destination entry state ทั้งหมดล่วงหน้า
   - บันทึก WAL ลง disk พร้อม flush/fsync เป็นสถานะ `PENDING` ก่อนเริ่ม copy ไฟล์แรก หากเกิด crash ระหว่างทาง ตัว bootloader จะเห็น WAL ชัดเจนว่าการย้ายยังไม่สมบูรณ์
3. **Command-Level Preimage Snapshot สำหรับ Multi-generation Restore:**
   - ในการรัน `restore-v1` หลาย generation พร้อมกัน ต้อง snapshot สถานะก่อนเริ่มคำสั่ง (command entry state) ไว้เป็น snapshot รวมครั้งเดียว ห้ามใช้ per-step/per-file backup ที่ถูก overwrite เมื่อเจอไฟล์ชื่อซ้ำข้าม generation
4. **Zero Tolerance on Swallowed Errors (Fail-Closed):**
   - ห้ามมี `except (OSError, Exception): pass` ใน path การย้ายหรือการบันทึก ledger หากขั้นตอนใดล้มเหลว ต้อง abort ทันที

### Invariants ที่ต้องมีเทสคุม (Automated Property/Contract Tests):
- **Invariant 1 (Non-decreasing copy count):** ในทุกๆ tick ของการทำงาน (รวมถึงการ simulate crash/kill ณ บรรทัดใดๆ) จำนวนสำเนาที่ถูกต้องของทุกไฟล์ข้อมูลต้อง >= 1 เสมอ (ห้ามเหลือ 0 โดยเด็ดขาด)
- **Invariant 2 (Idempotent replay from arbitrary crash):** การรันคำสั่งซ้ำหลัง crash กลางทาง ต้องตรวจพบ WAL และคืนสภาพได้โดยไม่สูญเสียหรือดึงไฟล์แปลกปลอม (เช่น live provider homes) เข้ามาปนในกรรมสิทธิ์
- **Invariant 3 (Entry-state preservation on multi-step abort):** หาก multi-generation restore ล้มเหลวที่ generation N ผลลัพธ์สุดท้ายที่ปลายทางต้อง rollback กลับไปเท่ากับ entry state ก่อนเริ่มคำสั่งเสมอ

---

## (C) Verdict และประมาณการขนาดงาน

- **Verdict:** **Refactor ก่อน 2.1.0 (Patch รอบ 4 ไม่พอ — ห้าม Patch แบบแก้เฉพาะจุดอีก)**
  - การ patch รอบที่ 1–3 เป็นการแก้แบบ reactive ที่ปลายเหตุ (เพิ่ม callback, flag, exception handler ย่อยๆ) ซึ่งยิ่งทำให้ control flow ของ `_two_phase_move` และ `rollback` ซับซ้อนขึ้นเรื่อยๆ จนเกิด corner cases ใหม่ตามมาทุกรอบ (ดังที่เห็นใน round 3 faults 17 รายการ)
  - ความเสี่ยงของการปล่อย 2.1.0 โดยไม่ refactor คือความเสี่ยงข้อมูลผู้ใช้สูญหายถาวร (Data Loss) ซึ่งละเมิด core principle ของ takkub
- **ประมาณขนาดงาน (Scope Estimate):**
  - **ไฟล์ที่ต้องปรับแก้/Refactor:** 3–4 ไฟล์
    1. `src/agent_takkub/core/migration/promote_v1.py` (ตัด `_two_phase_move` / `_restore_removed_source` ทิ้ง แทนที่ด้วย WAL-based copy-only + separate cleanup pass)
    2. `src/agent_takkub/core/migration/engine.py` (เพิ่ม transactional barrier & deferred cleanup stage หลัง validation ผ่าน)
    3. `src/agent_takkub/core/migration/backup.py` / `journal.py` (รองรับ command-level immutable preimage snapshot)
    4. `src/agent_takkub/cli.py` / `auto_migrate_boot.py` (เชื่อมต่อ lifecycle ใหม่ของ restore-v1)
  - **จำนวนฟังก์ชันที่ต้องเขียนใหม่/ปรับโครงสร้าง:** ~6–8 ฟังก์ชัน (เช่น ยุบ `_two_phase_move` เป็น staging copy, ลบ `_restore_removed_source` ออกทั้งหมด, ปรับปรุง `_undo_restored_names` ให้ใช้ command snapshot, และแยก `_prune_legacy_sources` เป็น independent validation-gated pass)
