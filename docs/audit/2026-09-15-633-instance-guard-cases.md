# Threat Model & Guard Cases: #633 (Instance & Cross-Cockpit Guard)

เอกสารวิเคราะห์ threat model, trust boundary, รูปแบบคำสั่ง, รูปแบบ payload/tool-call, และตาราง test cases ที่ต้อง Deny และ Allow ครบทั้งชุดสำหรับปัญหา **#633** (guard บังคับป้องกัน prod / cockpit instance อื่น ครอบคลุม default-deny ทุก role รวม Lead) ตามกฎ #609 ก่อนลงมือแก้ไขโค้ด

---

## 1. บทนำและข้อเท็จจริงของปัญหา (Incident Post-Mortem)

### 1.1 เหตุการณ์จริง (2026-09-15 11:39)
- **สถานการณ์:** มี cockpit รันอยู่ 2 instance บนเครื่องเดียวกัน:
  - **Dev instance:** ทำงานอยู่ใน checkout repo (`config.DATA_HOME == config.REPO_ROOT`)
  - **Prod instance:** ทำงานในโหมด installed build (`DATA_HOME == ~/.agent-takkub`) ถือ PID 26256
- **จุดที่ผิดพลาด:**
  1. Pane ใน cockpit dev รันคำสั่ง `python -m agent_takkub report build` ด้วย system Python ที่มีแพ็กเกจ `agent-takkub` 1.0.60 ติดตั้งอยู่
  2. System Python บูตเข้าโหมด installed ทำให้ `_resolve_data_home()` คืนค่า `~/.agent-takkub` (Prod DATA_HOME) แทนที่จะเป็น dev repo root
  3. Single-instance lock auto-kill ใน `app.py` ตรวจพบว่า lock file ของ prod (`agent-takkub-cockpit-cc791295977a.lock`) มี PID 26256 ถืออยู่ จึงส่งคำสั่ง auto-kill ไปฆ่า Prod cockpit PID 26256 ดับทันที
  4. Lead ใน dev instance เห็น process ปลอมค้างอยู่ จึงสั่ง kill ต่อไปโดยไม่รู้ว่ากำลังถือ lock ของ prod ทำให้ prod ดับถาวร
  5. Process ปลอมที่เพิ่งบูตเขียนไฟล์เปล่า `projects.json` และ `role-providers.json` ทับลง root ของ Prod DATA_HOME (#634) ส่งผลให้ข้อมูลคอนฟิกของ Prod หายทั้งหมด
- **ช่องโหว่เดิมใน Guard:**
  - #632 แก้เฉพาะฝั่งแอป (live-owner check ก่อน auto-kill/write_port) และใน `pane_guard` บล็อก `python -m agent_takkub` เฉพาะ specialist roles แต่ `lead` ยังคง exempt ผ่าน `_UNGUARDED_ROLES`
  - Guard ไม่มีชั้นตรวจจับ "Protected DATA_HOME" ทำให้คำสั่ง shell (`rm`, `mv`, `>`, `Set-Content`, `python -c`) และเครื่องมือ `Edit`/`Write` สามารถเขียน/แก้ไขไฟล์ใน `~/.agent-takkub` ได้โดยไม่มีสิ่งใดขวางกั้น
  - Guard ไม่มีชั้นตรวจจับการ `taskkill`/`Stop-Process`/`kill` ที่เจาะจง PID ของ cockpit instance อื่น
  - Guard ปัจจุบันมีนโยบาย fail-open ซึ่งหากตัววิเคราะห์ error คำสั่งอันตรายจะหลุดรอดไปทำงานจริง

---

## 2. Trust Boundaries & Scope of Enforcement

| Boundary | นิยาม / แหล่งที่มา | การบังคับใช้ (Enforcement) |
|---|---|---|
| **Own Cockpit Instance** | Instance ที่ pane สังกัด (ระบุจาก `TAKKUB_PORT_FILE`, `TAKKUB_STORAGE_ROOT`, `TAKKUB_ARTIFACTS_DIR`, หรือ port ที่เชื่อมต่อตอบ `instance-identity`) | อนุญาตให้ทำงาน อ่าน/เขียนไฟล์ และจัดการ child process ภายใน scope ของ instance ตัวเองได้ตามปกติ |
| **Protected DATA_HOME** | ทุก DATA_HOME ของ cockpit บนเครื่องที่ **ไม่ใช่** instance ของตนเอง (ค้นพบจาก installed default `~/.agent-takkub`, `config.REPO_ROOT`, `AGENT_TAKKUB_HOME`, lock files ใน temp, หรือ port files อื่น) | **Deny การเขียน/ย้าย/ลบ ทุกรูปแบบ** (อ่านได้อย่างเดียว) บังคับใช้กับ **ทุก role รวม Lead** |
| **Cross-Instance Process Kill** | คำสั่ง kill process (`taskkill`, `Stop-Process`, `kill`, `pkill`, `os.kill`, `psutil`) ที่เล็งเป้าหมายไปยัง PID ของ cockpit instance อื่น หรือ child process ของ instance อื่น | **Deny โดยเด็ดขาด** (ต้องตรวจสอบ PID จริงกับ instance table ไม่ใช่เดาจากชื่อ) ยกเว้นเป็น child process ของตนเอง |
| **Cockpit App Booting** | การรัน `python -m agent_takkub` (ไม่มี submodule), `agent-takkub` launcher, หรือ `npm i -g agent-takkub` | **Deny สำหรับทุก role รวม Lead** เว้นแต่มี `AGENT_TAKKUB_HOME` ชี้ไปยัง temp directory ในคำสั่งเดียวกัน |
| **Fail-Closed Policy** | กรณีตัววิเคราะห์ในหมวด instance guard เกิด Exception | **Fail-Closed ทันที** (`exit_code: 2`, deny) ห้าม fail-open |
| **User Confirmation** | ข้อยกเว้นกรณี user สั่งตรง | ต้องใช้กลไกที่ Lead ปลอมไม่ได้ (เช่น short-lived UI token) ปัจจุบันยังไม่มี UI hook ดังนั้นให้ **Default-Deny** ไว้ก่อน |

---

## 3. Threat Vectors & Attack Surfaces

### 3.1 Vector 1: App Booting & Single-Instance Lock Hijack
- คำสั่ง:
  - `python -m agent_takkub ...` (ไม่มี submodule)
  - `pythonw -m agent_takkub ...`
  - `py -m agent_takkub ...`
  - `agent-takkub` / `agent-takkub.exe` / `agent-takkub.cmd`
  - `npm i -g agent-takkub` / `npm install -g agent-takkub`
- Threat: บูต GUI cockpit ซ้อน ซึ่งอาจโหลดค่า config ของ prod หรือ dev แล้วเข้าสู่ลูป auto-kill ชิง single-instance lock และเขียนทับ port file / state

### 3.2 Vector 2: Direct Tool Calls (`Edit` / `Write`)
- เครื่องมือ: Claude PreToolUse หรือ Engine tool calls สำหรับ `Edit` และ `Write`
- Target path: ชี้ไปยังไฟล์ภายใน Protected DATA_HOME เช่น `C:\Users\monch\.agent-takkub\projects.json` หรือ `~/.agent-takkub/role-providers.json`
- Threat: เขียนทับหรือลบ configuration ของ prod cockpit โดยตรงผ่าน AI file-editing capabilities

### 3.3 Vector 3: Shell File Operations (Bash / CMD / PowerShell)
- คำสั่งลบ: `rm`, `del`, `erase`, `rmdir`, `rd`, `unlink`, `Remove-Item`, `ri`
- คำสั่งย้าย: `mv`, `move`, `Move-Item`, `mi`
- คำสั่งคัดลอก/เขียนทับ: `cp`, `copy`, `Copy-Item`, `cpi` (ปลายทางอยู่ใน Protected DATA_HOME)
- Redirection: `>`, `>>`, `1>`, `2>`, `*>` ชี้ไปยังไฟล์ใน Protected DATA_HOME
- PowerShell setters: `Set-Content`, `sc`, `Add-Content`, `ac`, `Out-File`
- Git command: `git -C <protected_data_home> ...` ที่ทำการเปลี่ยนแปลงสถานะ
- Python inline: `python -c "open('...', 'w').write(...)"`, `Path('...').write_text(...)`, `os.remove(...)`, `shutil.rmtree(...)`
- Threat: ทำลายไฟล์ฐานข้อมูลโปรเจค, token, session logs ของ instance อื่น

### 3.4 Vector 4: Cross-Instance Process Termination
- คำสั่ง: `taskkill /pid <PID>`, `taskkill /F /PID <PID>`, `Stop-Process -Id <PID>`, `kill <PID>`, `kill -9 <PID>`, `pkill <PID>`, `python -c "os.kill(<PID>, ...)"`, `python -c "psutil.Process(<PID>).kill()"`
- Threat: สั่งปิด/ฆ่า process ของ prod cockpit หรือ background workers/panes ของ instance อื่น ทำให้ระบบ prod ล่ม

### 3.5 Vector 5: Path Obfuscation & Normalization Bypasses
- รูปแบบที่ต้อง Normalize:
  - Quotes: `"..."`, `'...'`, escaped quotes `\"...\"`
  - Relative paths: `.\...`, `..\...`, `./...` (เทียบกับ `cwd`)
  - Environment variables: `~`, `$HOME`, `%USERPROFILE%`, `$env:USERPROFILE`, `%APPDATA%`, `$env:APPDATA`
  - Windows junction & symlink: `Path.resolve()` เพื่อหา canonical path ที่แท้จริง
  - Shell wrappers: `cmd /c "..."`, `pwsh -c "..."`, `powershell -c "..."`, `bash -c "..."`, `sh -c "..."`, `rtk ...`
  - Chaining: `;`, `&&`, `||`, `|`, `&`, newlines

---

## 4. ตารางเคสที่ต้อง DENY (Must Deny Cases)

ทุกเคสด้านล่างนี้เมื่อถูกตรวจสอบโดย `pane_guard.classify` หรือ `cli.cmd_guard` **ต้องส่งผล `allowed=False` (หรือ `exit_code: 2`)** ครอบคลุมทุก role รวมถึง `lead`

| ID | หมวดหมู่ | Role | คำสั่ง / Tool Payload | เหตุผลที่ต้อง Deny (Threat Vector) |
|---|---|---|---|---|
| **D01** | App Boot | `lead` | `python -m agent_takkub report build` | ห้ามบูต app agent_takkub โดยไม่มี temp AGENT_TAKKUB_HOME |
| **D02** | App Boot | `backend` | `python -m agent_takkub` | Specialist รัน bare module ไม่ได้ เสี่ยงบูต app ซ้อน |
| **D03** | App Boot | `lead` | `pythonw -m agent_takkub` | รัน pythonw bare module จะบูต GUI app |
| **D04** | App Boot | `lead` | `py -m agent_takkub` | รัน py bare module จะบูต app |
| **D05** | App Boot | `lead` | `agent-takkub` | เรียก app launcher ตรงๆ |
| **D06** | App Boot | `gemini` | `agent-takkub.exe` | เรียก executable app launcher |
| **D07** | App Boot | `lead` | `npm i -g agent-takkub` | ติดตั้ง/อัปเดต binary ซึ่งเสี่ยง auto-restart app |
| **D08** | App Boot | `backend` | `npm install -g agent-takkub` | ติดตั้ง global package ของ agent-takkub |
| **D09** | Tool Edit | `lead` | Tool: `Edit`, path: `~/.agent-takkub/projects.json` | Direct edit ไฟล์ใน Protected DATA_HOME |
| **D10** | Tool Write | `backend` | Tool: `Write`, path: `C:\Users\monch\.agent-takkub\role-providers.json` | Direct write ไฟล์ใน Protected DATA_HOME |
| **D11** | File Delete | `lead` | `rm ~/.agent-takkub/projects.json` | ลบไฟล์ใน Protected DATA_HOME ด้วย rm |
| **D12** | File Delete | `lead` | `del %USERPROFILE%\.agent-takkub\projects.json` | ลบไฟล์ใน Protected DATA_HOME ด้วย del |
| **D13** | File Delete | `lead` | `Remove-Item $env:USERPROFILE\.agent-takkub\projects.json` | ลบไฟล์ด้วย PowerShell Remove-Item |
| **D14** | File Write | `lead` | `echo "" > ~/.agent-takkub/projects.json` | เขียนทับไฟล์ด้วย shell redirection `>` |
| **D15** | File Write | `backend` | `echo "{}" >> ~/.agent-takkub/projects.json` | เพิ่มข้อมูลด้วย redirection `>>` |
| **D16** | File Write | `lead` | `Set-Content -Path ~/.agent-takkub/projects.json -Value "{}"` | เขียนไฟล์ด้วย PowerShell Set-Content |
| **D17** | File Write | `lead` | `Out-File -FilePath ~/.agent-takkub/projects.json` | เขียนไฟล์ด้วย PowerShell Out-File |
| **D18** | File Move | `lead` | `mv ~/.agent-takkub/projects.json ./backup.json` | ย้ายไฟล์ออกจาก Protected DATA_HOME |
| **D19** | File Move | `lead` | `Move-Item ./temp.json -Destination ~/.agent-takkub/` | ย้ายไฟล์เข้าไปใน Protected DATA_HOME |
| **D20** | File Copy | `backend` | `cp ./temp.json ~/.agent-takkub/projects.json` | ก๊อปปี้ไฟล์ทับ Protected DATA_HOME |
| **D21** | File Copy | `lead` | `Copy-Item ./temp.json -Destination ~/.agent-takkub/` | ก๊อปปี้ไฟล์ด้วย Copy-Item |
| **D22** | Python Write | `lead` | `python -c "open(r'C:\Users\monch\.agent-takkub\projects.json', 'w').write('{}')"` | Python inline เปิดไฟล์โหมด 'w' ใน Protected DATA_HOME |
| **D23** | Python Write | `lead` | `python -c "from pathlib import Path; Path('~/.agent-takkub/projects.json').write_text('{}')"` | Python pathlib write_text ใน Protected DATA_HOME |
| **D24** | Python Delete | `lead` | `python -c "import os; os.remove('~/.agent-takkub/projects.json')"` | Python os.remove ใน Protected DATA_HOME |
| **D25** | Git Mutate | `lead` | `git -C ~/.agent-takkub clean -fd` | รันคำสั่ง git ที่แก้ไขไฟล์ใน Protected DATA_HOME |
| **D26** | Kill Process | `lead` | `taskkill /pid 26256` (26256 = Prod cockpit PID) | ฆ่า process ของ cockpit instance อื่นด้วย taskkill |
| **D27** | Kill Process | `backend` | `taskkill /F /PID 26256` | Force kill PID ของ cockpit instance อื่น |
| **D28** | Kill Process | `lead` | `Stop-Process -Id 26256` | ฆ่า PID ของ instance อื่นด้วย Stop-Process |
| **D29** | Kill Process | `lead` | `kill 26256` | ฆ่า PID ของ instance อื่นด้วย kill |
| **D30** | Kill Process | `lead` | `pkill 26256` | ฆ่า PID ของ instance อื่นด้วย pkill |
| **D31** | Kill Process | `lead` | `python -c "import os; os.kill(26256, 9)"` | ฆ่า PID ของ instance อื่นด้วย Python os.kill |
| **D32** | Kill Process | `lead` | `python -c "import psutil; psutil.Process(26256).kill()"` | ฆ่า PID ของ instance อื่นด้วย Python psutil |
| **D33** | Kill Process | `lead` | `taskkill /pid 26257` (26257 = child ของ prod PID 26256) | ฆ่า child process ของ cockpit instance อื่น |
| **D34** | Wrapper Bypass | `lead` | `cmd /c "del ~/.agent-takkub\projects.json"` | ซ่อนคำสั่งใน cmd /c wrapper |
| **D35** | Wrapper Bypass | `lead` | `pwsh -c "Remove-Item ~/.agent-takkub/projects.json"` | ซ่อนคำสั่งใน pwsh -c wrapper |
| **D36** | Chaining Bypass | `lead` | `echo ok && rm -rf ~/.agent-takkub` | ซ่อนคำสั่งอันตรายใน command chain `&&` |
| **D37** | Relative Path | `lead` | `rm ../../.agent-takkub/projects.json` | ซ่อน path ด้วย relative path traversal |
| **D38** | Image Kill | `lead` | `taskkill /im pythonw.exe` (เมื่อ prod กำลังรัน) | ฆ่าด้วยชื่อ image name ที่กระทบ process ของ instance อื่น |

---

## 5. ตารางเคสที่ต้อง ALLOW (Must Allow Cases)

เคสด้านล่างนี้เป็นการใช้งานที่ถูกต้องและจำเป็น ต้องได้รับอนุญาตให้ทำงานได้ตามปกติ (`allowed=True` หรือ `exit_code: 0`):

| ID | หมวดหมู่ | Role | คำสั่ง / Tool Payload | เหตุผลที่ต้อง Allow (Legitimate Operation) |
|---|---|---|---|---|
| **A01** | Read File | `lead` | `cat ~/.agent-takkub/projects.json` | อ่านไฟล์ใน Protected DATA_HOME ได้ (ไม่เปลี่ยนแปลงข้อมูล) |
| **A02** | Read File | `lead` | `Get-Content ~/.agent-takkub/projects.json` | อ่านไฟล์ด้วย PowerShell Get-Content |
| **A03** | Read File | `lead` | `python -c "print(open('~/.agent-takkub/projects.json').read())"` | Python inline เปิดอ่านไฟล์โหมด read-only |
| **A04** | List Dir | `backend` | `ls ~/.agent-takkub` หรือ `dir ~/.agent-takkub` | แสดงรายการไฟล์ใน Protected DATA_HOME |
| **A05** | App Boot (Temp) | `lead` | `AGENT_TAKKUB_HOME=/tmp/test python -m agent_takkub report build` | อนุญาตเมื่อมี AGENT_TAKKUB_HOME ชี้ไปที่ temp directory |
| **A06** | App Boot (Temp) | `lead` | `$env:AGENT_TAKKUB_HOME='C:\Temp\test'; python -m agent_takkub` | อนุญาตเมื่อระบุ temp AGENT_TAKKUB_HOME ใน PowerShell |
| **A07** | Submodule | `lead` | `python -m agent_takkub.cli report build` | อนุญาต submodule ที่ไม่ใช่ bare `agent_takkub` |
| **A08** | Submodule | `backend` | `python -m agent_takkub.headless` | อนุญาต submodule อื่นๆ |
| **A09** | CLI Binary | `lead` | `takkub report build --type customer` | เรียกใช้งาน CLI binary ตรงๆ ตามนโยบาย |
| **A10** | CLI Binary | `gemini` | `takkub done 'finished'` | Specialist เรียก `takkub done` |
| **A11** | Tool Edit (Own) | `lead` | Tool: `Edit` ใน workspace/worktree ของตนเอง | แก้ไขไฟล์ source ภายใน project ตามขอบเขตงาน |
| **A12** | File Write (Own) | `backend` | `echo "test" > ./test_output.txt` | เขียนไฟล์ใน workspace ตัวเอง |
| **A13** | Kill Own Child | `backend` | `taskkill /pid 54321` (54321 = child dev server ของ pane ตัวเอง) | จัดการ child process ที่ pane ตัวเองเป็นคนสร้าง |
| **A14** | Kill Own Child | `lead` | `Stop-Process -Id 54321` (54321 = test runner ของตนเอง) | ปิด process ที่ตนเอง spawn ขึ้นมา |
| **A15** | Test Runner | `backend` | `python -m pytest tests/` | รันเทสตามปกติ |

---

## 6. แนวทางการออกแบบ Implementation ใน `pane_guard.py` และ `cli.py`

### 6.1 โมดูลตรวจจับ Instance Identity (`instance_guard.py` หรือส่วนหนึ่งของ `pane_guard.py`)
1. **`get_current_instance_info()`:**
   - ดึง `TAKKUB_PORT_FILE` จาก environment
   - ดึง `TAKKUB_STORAGE_ROOT` และ `TAKKUB_ARTIFACTS_DIR`
   - เชื่อมต่อ port ของตนเองเพื่อขอ `{"cmd": "instance-identity"}` (ได้ pid, port, canonical `data_home`)
   - Fallback ไปยัง `config.DATA_HOME.resolve()`
2. **`get_protected_data_homes()`:**
   - รวม list ของ candidate DATA_HOME:
     - Installed default: `(Path.home() / ".agent-takkub").resolve()`
     - Repo root: `config.REPO_ROOT.resolve()`
     - `AGENT_TAKKUB_HOME` (ถ้ามี)
     - ค้นหาจาก lock files `agent-takkub-cockpit-*.lock` ใน temp directory
     - ค้นหาจาก port files ที่มีอยู่
   - กรองเอาเฉพาะ path ที่ **ไม่เท่ากับ** `own_data_home`
   - คืนค่าเป็น `frozenset[Path]`
3. **`get_foreign_cockpit_pids()`:**
   - รวบรวม PID จาก lock files `agent-takkub-cockpit-*.lock` ของ instance อื่น
   - รวบรวม PID จาก port files ของ instance อื่นผ่าน `check_cockpit_port_alive` -> `instance-identity`
   - ค้นหา child processes ของ foreign PID เหล่านี้ (โดยใช้ `psutil` ถ้าติดตั้งอยู่ หรือคำสั่ง process inspection)
   - คืนค่าเป็น `set[int]`

### 6.2 การตรวจสอบใน `pane_guard.classify`
- ตรวจสอบก่อน `if not name or name in _UNGUARDED_ROLES:` (เพื่อให้ครอบคลุม `lead`)
- กฎที่ 1: `app_boot` — ตรวจจับ `python -m agent_takkub` (bare), `agent-takkub` launcher, `npm i -g agent-takkub` เว้นแต่มี temp `AGENT_TAKKUB_HOME`
- กฎที่ 2: `cross_instance_kill` — สกัด PID จาก `taskkill`, `Stop-Process`, `kill`, `pkill`, `os.kill`, `psutil` แล้วเทียบกับ `foreign_cockpit_pids`
- กฎที่ 3: `protected_data_home_write` — สกัด destination path จากคำสั่ง file mutation หรือ redirection แล้วตรวจสอบว่าตกอยู่ใต้ Protected DATA_HOME หรือไม่

### 6.3 การตรวจสอบใน `cli.cmd_guard`
- สำหรับ `Edit` และ `Write`:
  - ตรวจสอบ `file_path` เทียบกับ `protected_data_homes` สำหรับ **ทุก role** (ไม่ยกเว้น specialist หรือ lead)
  - ถ้าตกอยู่ใน Protected DATA_HOME ให้ปฏิเสธทันที (`exit_code: 2`)
- นโยบาย Fail-Closed สำหรับหมวด Instance Guard:
  - ใน `cmd_guard` ให้ครอบ try-except เฉพาะส่วนของ instance guard เพื่อคืนค่า `exit_code: 2` ทันทีหากเกิด error แทนที่จะปล่อยหลุดไปที่ outer fail-open

---
*เอกสารนี้ถูกจัดเตรียมไว้สำหรับการตรวจสอบและอ้างอิงตามข้อกำหนด #633 ก่อนเริ่มเขียนโค้ด*
