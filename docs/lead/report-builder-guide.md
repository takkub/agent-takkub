# Report Types Guide — 3 Formats for Different Audiences

This document describes the content requirements and guidelines for each of the 3 report types built by `takkub report build --type <customer|dev|boss>`.

All reports:
- Use the same HTML template, CSS, and lightbox functionality
- Embed images as JPEG base64 data URIs (max 1440px wide, quality 80)
- Support dark/light mode automatically
- Work on all providers (claude/codex/gemini-agy/opencode/kimi/cursor)
- Work on Windows ConPTY and macOS

---

## 1. Customer Report (User Manual)

**Audience:** End users, customers, non-technical stakeholders
**Purpose:** Explain how to use a feature or system in simple, step-by-step language

### What to include
- Feature overview and purpose
- Step-by-step instructions with numbered lists
- Screenshots showing the user-facing UI (not admin/backend)
- Tips and common questions
- Examples of expected outcomes

### Strictly forbidden in customer reports
- **Code or technical details:** commit hashes, file paths (`.ts`, `.py`, `/api/`), endpoints, error codes
- **Bug/fix terminology:** "bug", "migration", "qa-gate", "qa-test"
- **Provider names:** "claude", "codex", "gemini", "anthropic", "kimi", "cursor", "opencode"
- **Internal role names:** "backend", "devops", "lead", "reviewer"
- **Financial/internal data:** salary, cost, internal yalue figures
- **Error responses:** 403, 500, error messages with codes

### Format
- Conversational, friendly tone
- Thai language as default (or local language)
- Focus on "what" and "how", not "why"
- Role badges for different user types (e.g., "ลูกค้า", "แอดมิน")

### Lint
The `takkub report build --type customer` command will warn before publish if any forbidden words are found.

### Example sections
```
<header class="top">
  <h1>Feature Name</h1>
  <p>What users can do with this feature in simple terms.</p>
</header>

<section class="sys">
  <div>
    <h2>How to use</h2>
    <div class="role member">
      <h3>User (ลูกค้า)</h3>
      <ol>
        <li>Click the feature menu</li>
        <li>Follow the prompts</li>
      </ol>
    </div>
  </div>
  <div class="shots">
    <figure><button><img src="{{img:screenshot-name}}" alt="..."></button></figure>
  </div>
</section>
```

---

## 2. Dev Report (Technical Report)

**Audience:** Other developers, QA, technical reviewers
**Purpose:** Document what changed, why, how it was tested, and any risks

### What to include
- Summary of changes (files modified, lines affected)
- Root cause analysis for bugs
- Test results with numbers (X/Y tests passed)
- Screenshots of the UI changes
- API responses and error codes (if relevant)
- Database changes or migrations
- Performance impact (if any)
- Deployment steps or migration notes
- Risks and what wasn't tested

### Acceptable technical detail
- File paths (`src/foo/bar.ts:42`)
- Commit hashes and branch names
- Error codes and stack traces
- API endpoint names
- Database migration scripts
- Test stats and coverage numbers

### Forbidden in dev reports
- Internal business decisions or strategy discussions
- Financial figures or costs
- Role/team details (name the impact, not the person)

### Format
- Technical but clear language
- Code blocks with `<pre class="ev">` for examples
- Status badges for test results (✓ pass, ✗ fail, ⊘ skip)
- Tables for comparing before/after

### Example sections
```
<section class="sys">
  <div>
    <h2>What changed</h2>
    <p>Modified files:</p>
    <ul>
      <li><code>src/foo/bar.ts:42</code> — added validation</li>
      <li><code>tests/foo.spec.ts</code> — added test case</li>
    </ul>
    <p>Commits:</p>
    <pre class="ev">abc1234 fix: add validation
def5678 test: add coverage</pre>
  </div>
</section>

<div class="overview">
  <div class="kpis">
    <div class="kpi"><b>30/33</b><span>tests passed</span></div>
    <div class="kpi"><b>0</b><span>regressions found</span></div>
  </div>
</div>
```

---

## 3. Boss Report (Executive Summary)

**Audience:** Product owners, managers, non-technical decision makers
**Purpose:** Show progress, impact, risks, and decisions needed at a glance

### What to include
- High-level summary (what was accomplished)
- Status by feature/area (done, testing, deployed)
- Key metrics (bugs found, performance gain %)
- Impact on users (how many users affected, when)
- Risks and decisions needed
- Next steps

### Strictly avoid
- Deep technical details (`src/foo.ts`, commit hashes, error codes)
- Internal team structure details
- Detailed test results or deployment steps

### Acceptable summary-level detail
- Number of bugs (not individual bug names)
- Release/version names
- Deployment date and which environment
- "X% improvement" or "Y users affected"

### Format
- Executive bullet points
- KPI tiles showing metrics
- Status colors (🟢 done, 🟡 testing, 🔴 blocked)
- Simple tables comparing before/after at a high level

### Example sections
```
<div class="overview">
  <div class="kpis">
    <div class="kpi"><b>5 of 7</b><span>features complete</span></div>
    <div class="kpi"><b>1,000+ users</b><span>can now access the feature</span></div>
  </div>
  <table>
    <tr><td>Feature A</td><td><span class="status ok">Done</span></td></tr>
    <tr><td>Feature B</td><td><span class="status skip">Testing</span></td></tr>
  </table>
</div>

<section class="sys text-only">
  <div>
    <h2>Risks and decisions</h2>
    <ul>
      <li><b>Risk:</b> Migration may take 2 hours during peak time</li>
      <li><b>Decision needed:</b> Should we delay until Thursday?</li>
    </ul>
  </div>
</section>
```

---

## Building Reports

### Command
```bash
takkub report build --type customer|dev|boss \
  --content ./report-content \
  [--out report.html] \
  [--title "My Report Title"]
```

### Content directory structure
```
report-content/
  content.html          # Main content (body sections)
  images.txt            # Image mappings: name|path
  template.html         # (optional) custom template
```

### Images
- Supported formats: PNG, JPG, GIF, WebP
- Automatically converted to JPEG quality 80
- Max width: 1440px (auto-resized if larger)
- Embedded as base64 data URIs (no external requests)
- Duplicates detected by MD5 hash (warning if same image used twice with different names)

### Mobile checking (optional)
If playwright is installed, reports are automatically checked for:
- Viewport: 360px (small phone), 390px (modern phone), 768px (tablet)
- No horizontal scrolling
- Tables don't overflow viewport width
- Touch targets are adequate

Skipped if playwright unavailable (not an error).

---

## Content Blocks and Special Tags

### Conditional sections
Use HTML comments to hide content for specific report types:

```html
<!--CUSTOMER-ONLY-->
This section only appears in customer reports
<!--/CUSTOMER-ONLY-->

<!--DEV-ONLY-->
This section only appears in dev reports
<!--/DEV-ONLY-->

<!--BOSS-ONLY-->
This section only appears in boss reports
<!--/BOSS-ONLY-->
```

### Image placeholders
```html
<figure>
  <img src="{{img:my-screenshot-name}}" alt="...">
</figure>
```

Maps to `images.txt`:
```
my-screenshot-name|/path/to/screenshot.png
```

### Styled elements
```html
<!-- Status badge -->
<span class="status ok">Done</span>
<span class="status bad">Failed</span>
<span class="status skip">Skipped</span>

<!-- KPI tiles (metrics) -->
<div class="kpis">
  <div class="kpi"><b>99.9%</b><span>uptime</span></div>
  <div class="kpi"><b>5ms</b><span>response time</span></div>
</div>

<!-- Code/response blocks -->
<pre class="ev">GET /api/user/123
{
  "name": "John",
  "status": "active"
}</pre>

<!-- Role badges -->
<div class="role admin"><h3>Admin</h3>...</div>
<div class="role member"><h3>User</h3>...</div>
```

---

## Publishing

After building, publish with:
```bash
takkub report publish report.html \
  --name "my-report.html" \
  --expires 30d \
  --label "Feature launch report"
```

The published link is stable even if you rebuild and republish with the same `--name`.
