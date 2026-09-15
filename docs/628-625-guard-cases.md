# Threat Model & Guard Cases: #628 & #625 (Lead Direct-Edit Guard)

เอกสารวิเคราะห์ threat boundary, รูปแบบคำสั่ง, รูปแบบโค้ด/diff, และตาราง test cases ที่ต้อง Deny และ Allow ครบทั้งชุดสำหรับปัญหา **#628** และ **#625** ก่อนลงมือแก้ไขโค้ด `pane_guard.py`

---

## 1. บทนำและปัญหาที่พบ

### 1.1 ปัญหา #628: Identifier Word Boundary Bypasses in `sensitive_deep_patterns`
- **ต้นเหตุ:** ฟังก์ชัน `evaluate_lead_direct_edit` ใน `src/agent_takkub/pane_guard.py` ใช้ regex patterns เช่น `\b(?:auth|oauth|jwt|login|signup|password|permission|privilege|otp|2fa|mfa|rate[\s_-]?limit)\b`, `\b(?:tokens?|api[_-]?keys?|secrets?)\b`
- ใน Python regex (`re`), อักขระ word character (`\w`) คือ `[a-zA-Z0-9_]` (รวม underscore `_` เข้าไปด้วย)
- เมื่อมี identifier ในรูปแบบ **snake_case** เช่น `check_auth_token`, `refresh_token`, `api_auth`, `is_admin_user`, `bypass_auth`, `skip_auth_check`:
  - ตัวอักษรข้างเคียง `auth` หรือ `token` คือ `_` ซึ่งเป็น word character ทำให้ `\b` (word boundary) ไม่ตรงเงื่อนไข
  - ส่งผลให้ pattern ทั้งหมดไม่ match และ Lead สามารถ direct Edit/Write โค้ด sensitive เหล่านี้ได้โดยไม่ถูก guard ดักจับ
- สำหรับ **camelCase / PascalCase** เมื่อถูก lowercase ก่อน regex (เช่น `checkAuthToken` -> `checkauthtoken`, `refreshToken` -> `refreshtoken`, `apiAuth` -> `apiauth`):
  - ตัวอักษรถูกรวมเป็นคำเดี่ยว ไม่มี boundary ทำให้หลุดการตรวจสอบเช่นเดียวกัน
- สำหรับ **dot-notation / kebab-case / SCREAMING_SNAKE_CASE**:
  - `req.auth_token` หรือ `config.jwt_secret` หลุดเนื่องจาก underscore
  - `AUTH_TOKEN`, `API_KEY` หลุดเมื่อ lowercased แล้วติด underscore
- นอกจากนี้ pattern บางตัวเช่น `(?:bypass|skip|disable|remove)\s*(?:verif|valid|signature|sanitiz|auth|check)` ใช้ `\s*` ทำให้ bypass ในรูปแบบ snake_case (`bypass_auth`, `bypass_signature`, `skip_auth_check`, `disable_signature_validation`) หลุดทั้งหมด

### 1.2 ปัญหา #625: Scratchpad Files Outside Project Root Incorrectly Denied
- **ต้นเหตุ:** ลำดับการตรวจสอบใน `evaluate_lead_direct_edit` ปัจจุบัน:
  1. `_is_note_exempt` (`.md`/`.txt`)
  2. `structural_deep_patterns` (`package.json`, lockfiles, `migrations`, CI workflows, Dockerfile)
  3. `sensitive_deep_patterns` (auth, tokens, crypto, payments) ตรวจทั้ง path และ diff content
  4. `_is_direct_edit_exempt` (runtime directory, scratchpad ใน `%TEMP%` หรือนอก project root)
- ใน #587 F3 มีการจัดลำดับให้ deep check ตรวจก่อน `_is_direct_edit_exempt` เพื่อป้องกันกรณี `package.json` หรือ CI config ที่อยู่ระดับ repo แต่อยู่นอก configured sub-roots (multi-root project)
- ทว่าใน #611 มีการเพิ่มการตรวจ **diff content** เข้าไปใน `sensitive_deep_patterns` ทำให้ไฟล์ชั่วคราว/scratchpad เช่น `<scratchpad ใน %TEMP%>/probe.sh` ที่มีเนื้อหา `echo check auth token flow` โดน deny ด้วย `lead_direct_edit:deep_category` ทันที ทั้งๆ ที่ไม่ใช่ซอร์สโค้ดของโปรเจค
- **ข้อกำหนดที่ถูกต้อง:**
  - **Structural deep** (lockfile, manifest, CI workflows, migration, schema) ต้อง **Deny ทุกที่** (ไม่ว่าจะอยู่นอก root หรือไม่ ตาม #587 F3)
  - **Exempt check** (`_is_direct_edit_exempt`) ต้องทำงานเพื่อยกเว้น scratchpad/temp/memory และ runtime directory
  - **Sensitive-keyword check** (auth, security, tokens, crypto, payments ทั้ง path และ diff content) ต้องตรวจเฉพาะไฟล์ที่ **ไม่ใช่ exempt** (กล่าวคือ อยู่ใต้ project root / BLOCKED_DIRS)

---

## 2. Threat Model & Trust Boundaries

| Boundary | ผู้กระทำ / เป้าหมาย | กฎการควบคุม (Policy) |
|---|---|---|
| **Lead Direct-Edit Policy** | Lead agent (role `lead`) ทำการ Edit / Write | อนุญาตเฉพาะงาน `scope=tiny` ที่เป็น non-source หรือ tiny fix ทั่วไป (≤15 บรรทัด/ครั้ง, สะสม ≤2 ไฟล์/30 บรรทัด) **และต้องไม่อยู่ในหมวด deep category** |
| **Deep Category: Structural** | ไฟล์ manifest, lockfile, schema, migration, CI, Dockerfile | ห้าม Lead แก้ไขโดยเด็ดขาด **ทุกกรณีและทุกที่** (รวมถึงไฟล์นอก project root) ต้อง delegate ผ่าน `takkub assign` |
| **Deep Category: Sensitive Keywords** | ฟังก์ชัน/ตัวแปร/โมดูลเกี่ยวกับ auth, security, crypto, token, secret, payment, bypass | ห้าม Lead แตะต้องในซอร์สโค้ดของโปรเจค ไม่ว่าจะเขียนในรูปแบบ snake_case, camelCase, PascalCase, kebab-case, dot-notation หรือ screaming snake |
| **Scratchpad / Runtime Exemption** | สคริปต์ทดสอบชั่วคราว, task spec, probe scripts ใน `%TEMP%`, scratchpad, cockpit runtime | อนุญาตให้ Lead เขียน/อ่านได้อย่างอิสระเพื่อตรวจสอบระบบ เนื้อหา diff ในไฟล์เหล่านี้ต้องไม่ติด sensitive-keyword deny |
| **Test Path Carve-Out (#611)** | ไฟล์เทสที่มีโฟลเดอร์เทสชัดเจน (`tests/`, `__tests__/`, `e2e/`) | ตัว path เองไม่ถูกบล็อกด้วย sensitive keyword (เช่น `tests/test_auth.py`) แต่ **diff content ยังคงถูกตรวจสอบ** เพื่อป้องกันการ hardcode secret หรือ bypass ใน test fixture |

---

## 3. ตารางเคสที่ต้อง DENY (Must Deny Cases)

ทุกเคสด้านล่างนี้เมื่อเรียก `evaluate_lead_direct_edit` ในบริบทโปรเจค (หรือ `project=None` เมื่อไฟล์อยู่ใน project/cwd) **ต้องคืนค่า `allowed=False`** พร้อม `rule="lead_direct_edit:deep_category"`

| ID | ประเภท / เคส | Target Path | Content / Diff Text | เหตุผลที่ต้อง Deny (Threat Vector) |
|---|---|---|---|---|
| **D01** | Snake_case function | `src/agent_takkub/probe_tmp.py` | `def check_auth_token(): pass` | มี `auth` และ `token` ใน snake_case |
| **D02** | Snake_case variable | `src/agent_takkub/probe_tmp.py` | `refresh_token = get_token()` | มี `token` ใน snake_case |
| **D03** | Snake_case function | `src/agent_takkub/probe_tmp.py` | `def api_auth(): pass` | มี `auth` ใน snake_case |
| **D04** | Snake_case variable | `src/agent_takkub/probe_tmp.py` | `is_admin_user = True` | มี `is_admin` ใน snake_case |
| **D05** | Snake_case bypass | `src/agent_takkub/probe_tmp.py` | `def bypass_auth(): pass` | Bypass logic ใน snake_case |
| **D06** | Snake_case bypass | `src/agent_takkub/probe_tmp.py` | `def skip_auth_check(): pass` | Skip check ใน snake_case |
| **D07** | Snake_case bypass | `src/agent_takkub/probe_tmp.py` | `def disable_signature_validation(): pass` | Disable verification ใน snake_case |
| **D08** | Snake_case bypass | `src/agent_takkub/probe_tmp.py` | `def bypass_signature(): pass` | Bypass signature ใน snake_case |
| **D09** | Snake_case secret/key | `src/agent_takkub/probe_tmp.py` | `api_key = read_key()` | มี `api_key` ใน snake_case |
| **D10** | Snake_case secret/key | `src/agent_takkub/probe_tmp.py` | `get_secret_key()` | มี `secret` ใน snake_case |
| **D11** | Snake_case jwt | `src/agent_takkub/probe_tmp.py` | `jwt_token = encode()` | มี `jwt` และ `token` ใน snake_case |
| **D12** | Snake_case password | `src/agent_takkub/probe_tmp.py` | `user_password = hash_pass()` | มี `password` ใน snake_case |
| **D13** | Snake_case permission | `src/agent_takkub/probe_tmp.py` | `user_permission = check()` | มี `permission` ใน snake_case |
| **D14** | Snake_case 2fa/otp | `src/agent_takkub/probe_tmp.py` | `check_2fa(code)` | มี `2fa` ใน snake_case |
| **D15** | Snake_case otp | `src/agent_takkub/probe_tmp.py` | `verify_otp(code)` | มี `otp` ใน snake_case |
| **D16** | Snake_case mfa | `src/agent_takkub/probe_tmp.py` | `user_mfa(user)` | มี `mfa` ใน snake_case |
| **D17** | Snake_case rate limit | `src/agent_takkub/probe_tmp.py` | `rate_limit = 100` | มี `rate_limit` ใน snake_case |
| **D18** | Snake_case path | `src/agent_takkub/check_auth_token.py` | `x = 1\n` (bland diff) | Path มี `auth` และ `token` ใน snake_case |
| **D19** | Snake_case path | `src/agent_takkub/refresh_token.py` | `x = 1\n` (bland diff) | Path มี `token` ใน snake_case |
| **D20** | Snake_case path | `src/agent_takkub/api_auth.py` | `x = 1\n` (bland diff) | Path มี `auth` ใน snake_case |
| **D21** | Snake_case path | `src/agent_takkub/is_admin_user.py` | `x = 1\n` (bland diff) | Path มี `is_admin` ใน snake_case |
| **D22** | CamelCase function | `src/agent_takkub/probe_tmp.ts` | `function checkAuthToken() {}` | มี `Auth` และ `Token` ใน camelCase |
| **D23** | CamelCase variable | `src/agent_takkub/probe_tmp.ts` | `const refreshToken = token;` | มี `Token` ใน camelCase |
| **D24** | CamelCase function | `src/agent_takkub/probe_tmp.ts` | `function apiAuth() {}` | มี `Auth` ใน camelCase |
| **D25** | CamelCase variable | `src/agent_takkub/probe_tmp.ts` | `const isAdminUser = true;` | มี `isAdmin` ใน camelCase |
| **D26** | CamelCase bypass | `src/agent_takkub/probe_tmp.ts` | `function bypassAuth() {}` | Bypass auth ใน camelCase |
| **D27** | CamelCase bypass | `src/agent_takkub/probe_tmp.ts` | `function skipAuthCheck() {}` | Skip check ใน camelCase |
| **D28** | CamelCase bypass | `src/agent_takkub/probe_tmp.ts` | `disableSignatureValidation()` | Disable signature ใน camelCase |
| **D29** | CamelCase secret/key | `src/agent_takkub/probe_tmp.ts` | `const checkApiKey = key;` | มี `ApiKey` ใน camelCase |
| **D30** | CamelCase secret/key | `src/agent_takkub/probe_tmp.ts` | `getSecretKey()` | มี `Secret` ใน camelCase |
| **D31** | CamelCase jwt | `src/agent_takkub/probe_tmp.ts` | `const jwtToken = sign();` | มี `jwtToken` / `JWTToken` |
| **D32** | CamelCase password | `src/agent_takkub/probe_tmp.ts` | `const userPassword = val;` | มี `Password` ใน camelCase |
| **D33** | CamelCase permission | `src/agent_takkub/probe_tmp.ts` | `const userPermission = p;` | มี `Permission` ใน camelCase |
| **D34** | CamelCase 2fa | `src/agent_takkub/probe_tmp.ts` | `check2FA(code)` | มี `2fa` ใน camelCase |
| **D35** | CamelCase otp | `src/agent_takkub/probe_tmp.ts` | `verifyOtp(code)` / `verifyOTP` | มี `Otp` ใน camelCase |
| **D36** | CamelCase mfa | `src/agent_takkub/probe_tmp.ts` | `userMfa(user)` / `userMFA` | มี `Mfa` ใน camelCase |
| **D37** | CamelCase rate limit | `src/agent_takkub/probe_tmp.ts` | `const rateLimit = 50;` | มี `rateLimit` ใน camelCase |
| **D38** | CamelCase path | `src/agent_takkub/checkAuthToken.ts` | `x = 1\n` (bland diff) | Path มี `AuthToken` ใน camelCase |
| **D39** | CamelCase path | `src/agent_takkub/refreshToken.ts` | `x = 1\n` (bland diff) | Path มี `Token` ใน camelCase |
| **D40** | CamelCase path | `src/agent_takkub/apiAuth.ts` | `x = 1\n` (bland diff) | Path มี `Auth` ใน camelCase |
| **D41** | CamelCase path | `src/agent_takkub/isAdminUser.ts` | `x = 1\n` (bland diff) | Path มี `isAdmin` ใน camelCase |
| **D42** | Screaming Snake Case | `src/agent_takkub/probe_tmp.py` | `AUTH_TOKEN = "secret"` | SCREAMING_SNAKE_CASE auth/token |
| **D43** | Screaming Snake Case | `src/agent_takkub/probe_tmp.py` | `API_KEY = "key123"` | SCREAMING_SNAKE_CASE api_key |
| **D44** | Screaming Snake Case | `src/agent_takkub/probe_tmp.py` | `JWT_SECRET = "jwt"` | SCREAMING_SNAKE_CASE secret |
| **D45** | Kebab-case | `src/agent_takkub/probe_tmp.ts` | `url = "/api/check-auth-token"` | Kebab-case auth-token |
| **D46** | Dot-notation property | `src/agent_takkub/probe_tmp.ts` | `const userToken = req.auth_token;` | Property เข้าถึง `auth_token` |
| **D47** | Dot-notation property | `src/agent_takkub/probe_tmp.ts` | `const k = config.apiKey;` | Property เข้าถึง `apiKey` |
| **D48** | Hardcoded test secret (#611) | `src/security-e2e/fixtures.ts` | `const apiKey = "hardcoded";` | Sensitive content hiding in test fixture |
| **D49** | Structural outside root (#587 F3) | `<scratchpad>/package.json` | `{"name": "test"}` | Structural deep (package.json) นอก root |
| **D50** | Structural outside root (#587 F3) | `<scratchpad>/.github/workflows/ci.yml` | `name: CI` | Structural deep (CI workflow) นอก root |

---

## 4. ตารางเคสที่ต้อง ALLOW (Must Allow Cases)

ทุกเคสด้านล่างนี้เมื่อเรียก `evaluate_lead_direct_edit` **ต้องคืนค่า `allowed=True`**

| ID | ประเภท / เคส | Target Path | Content / Diff Text | เหตุผลที่ต้อง Allow |
|---|---|---|---|---|
| **A01** | **#625 Scratchpad in %TEMP%** | `<scratchpad in %TEMP%>/probe.sh` | `echo check auth token flow` | สคริปต์ชั่วคราวนอก project root ต้องไม่ถูก content deep บล็อก |
| **A02** | **#625 Scratchpad in %TEMP%** | `<scratchpad in %TEMP%>/auth_test.sh` | `curl -H "Authorization: Bearer token" http://localhost` | สคริปต์ probe/test ใน temp นอก project root |
| **A03** | **#625 Cockpit runtime note** | `<config.RUNTIME_DIR>/lead_edits/state.json` | `{"auth": "verified"}` | State/ไฟล์ชั่วคราวของ cockpit runtime |
| **A04** | **#625 Memory / scratch outside root** | `~/.claude-work/scratch/task.sh` | `python -c "import auth"` | สคริปต์ scratchpad นอก project root |
| **A05** | Word boundary: `author` | `src/agent_takkub/meta.py` | `author_name = "Jane Doe"` | `author` ไม่ใช่ `auth` (ผู้เขียน/git author) |
| **A06** | Word boundary: `author` | `src/agent_takkub/git.py` | `git_author = get_author()` | `author` ไม่ใช่ `auth` |
| **A07** | Word boundary: `tokenize` | `src/agent_takkub/parser.py` | `tokens = tokenizer.tokenize(text)` | `tokenize`/`tokenizer` ไม่ใช่ auth token |
| **A08** | Word boundary: `keyboard` | `src/agent_takkub/ui.py` | `on_key_press(keyboard_event)` | `keyboard`/`key_press` ไม่ใช่ `api_key` |
| **A09** | Word boundary: dict keys | `src/agent_takkub/util.py` | `for key in mapping.keys():` | bare `key`/`keys` ของ dictionary ไม่ใช่ `api_key` |
| **A10** | Test path with bland diff (#611) | `apps/api/src/security-e2e/test-harness.ts` | `x = 1\n` -> `x = 2\n` | โฟลเดอร์ e2e/test ยกเว้น path leg เมื่อเนื้อหาไม่ sensitive |
| **A11** | Test path with bland diff (#611) | `src/payments.test.ts` | `x = 1\n` -> `x = 2\n` | Test suffix ยกเว้น path leg เมื่อเนื้อหาไม่ sensitive |
| **A12** | Test path with bland diff (#611) | `tests/test_auth.py` | `x = 1\n` -> `x = 2\n` | Test path ยกเว้น path leg เมื่อเนื้อหาไม่ sensitive |
| **A13** | Plain non-sensitive tiny edit | `src/agent_takkub/math.py` | `def add(a, b): return a + b` | ซอร์สโค้ดปกติที่ไม่อยู่ในหมวด deep และไม่เกิน cap |
| **A14** | Note exemption (#587 A2) | `docs/auth_guide.md` | `# Authentication Guide\nTokens...` | ไฟล์ `.md`/`.txt` ได้รับการยกเว้นตาม `#587 A2` |
| **A15** | Preset exemption (#587 A3) | `src/agent_takkub/auth.py` | `def auth(): pass` | โปรเจคที่ตั้ง `lead_may_implement=True` (เช่น agent-takkub) |

---

## 5. กลยุทธ์การแก้ไขโค้ด (Implementation Strategy)

### 5.1 แก้ไข #625: ปรับลำดับการตรวจสอบใน `evaluate_lead_direct_edit`
1. ตรวจ `_is_note_exempt` -> Allow
2. ตรวจ `lead_may_implement(project)` -> Allow
3. ตรวจ `structural_deep_patterns` (lockfile, manifest, migration, CI, schema, dockerfile) -> **Deny ทุกที่** (คงกฎ #587 F3)
4. ตรวจ `_is_direct_edit_exempt(file_path, cwd, project)` -> **Allow ทันที** (หากอยู่นอก project root / scratchpad / runtime)
5. ตรวจ `sensitive_deep_patterns` (ทั้ง path และ diff content) -> **ตรวจเฉพาะไฟล์ใต้ project root / BLOCKED_DIRS**
6. ตรวจ scope tiny, line count, cumulative caps ตามลำดับ

### 5.2 แก้ไข #628: Normalization & Regex Enhancements
1. สร้างฟังก์ชัน `_normalize_identifier_boundaries(text: str) -> str`:
   - แยก camelCase/PascalCase: อักษรพิมพ์เล็กตามด้วยพิมพ์ใหญ่ (`re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)`), พิมพ์ใหญ่กลุ่มตามด้วยพิมพ์ใหญ่+พิมพ์เล็ก (`re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)`)
   - แปลง underscore `_` เป็นช่องว่าง ` `
   - แปลงเป็น lowercase
2. ปรับ regex ใน `sensitive_deep_patterns`:
   - Pattern 2 (bypass/skip): รองรับ delimiter `[\s\-_]*` แทน `\s*` เพื่อจับ `bypass_auth`, `skip_auth_check`, `disable_signature_validation`, `bypass_signature`
   - Pattern 5 (tokens/api_keys): รองรับ `api[\s\-_]?keys?`
   - Pattern 8 (verify_signature/is_admin): รองรับ `verify[\s\-_]*signature|is[\s\-_]*admin|\bbypass\b|return\s+true\b`
3. ใช้ normalized text ในการตรวจสอบ `sensitive_deep_patterns` ทั้งสำหรับ `norm_file` (path) และ `diff_text` (content)
