# Project Review: oh-my-agent (OMA)
**Date:** 2026-05-21  
**Reviewer:** Gemini Reviewer (takkub-agent)  
**Target Path:** `oh-my-agent-temp`  

---

### TL;DR
**Recommendation: Adopt / Vendor-in (Selective)**  
oh-my-agent is a professional-grade, highly sophisticated multi-agent orchestration framework. It features a robust "portable SSOT" architecture, rigorous security controls, and innovative "skill scaling" logic. Its documentation drift detection and skill boundary auditing are "killer features" that set it apart. It is a high-value project that should be leveraged by agent-takkub, either as a backend or a source of architectural inspiration.

---

### 1. What is this?
**oh-my-agent (OMA)** is a portable multi-agent harness designed to coordinate specialized AI agents (Frontend, Backend, Architecture, QA, etc.) across multiple AI IDEs (Claude Code, Cursor, Gemini CLI, etc.).

- **Tech Stack:** TypeScript, Bun (runtime), Commander (CLI), Docusaurus (Web Docs).
- **Core Concept:** A "Single Source of Truth" (SSOT) in `.agents/` that defines skills, workflows, and rules. This structure is projected/installed into the native configurations of various AI tools, ensuring consistent behavior across different environments.
- **Key Purpose:** To solve the problem of "monolithic agent confusion" by splitting tasks into specialized domains with gated contexts.

### 2. Code Quality
**Rating: Excellent (Senior/Lead Engineer Level)**

- **Architecture:** Uses a clean, slice-based architecture in the CLI (`commands/<name>/`). Enforcement of boundaries is automated via CI scripts (`check-boundaries.mjs`).
- **Technical Rigor:** Implements advanced concepts like **TF-IDF for skill boundary auditing** (detecting overlapping agent responsibilities) and **Deterministic Document Reference Resolution**.
- **Execution Protocol:** Uses an "SSL-lite" (Scheduling-Structural-Logical) format for skills, which is backed by recent AI agent research (Scaling Laws of Skills).
- **Maintenance:** Highly active. Uses Conventional Commits, Biome for linting, and has a comprehensive suite of smoke and unit tests using the Bun test runner.

### 3. Security/Red Flags
**Rating: Proactive & Robust**

- **Secrets Management:** Extensive rules and code-level checks to prevent hardcoded secrets. CLI logic explicitly skips "secret-bearing files" during documentation syncing.
- **Vault System:** Includes a `vault` command for managing sensitive configurations.
- **Security Workflows:** Features `oma-deepsec`, an agent-powered vulnerability scanner with PR-gate integration.
- **Dependency Audit:** No suspicious dependencies found; uses well-known, lightweight packages.

### 4. Maturity
- **Active Maintenance:** Extremely high. Commits are frequent (minutes/hours ago).
- **Community:** Support for 12 languages in documentation and a versioning history (v8.4.0) suggesting significant real-world usage and iteration.
- **Professionalism:** The project includes ADRs (Architecture Decision Records), a technical specification (`AGENTS_SPEC.md`), and a clear roadmap.

### 5. Fit with agent-takkub
- **Overlap:** There is significant conceptual overlap in the "multi-agent" space. However, OMA is much more "portable" and "IDE-agnostic" than most competitors.
- **Ideas to Adopt/Steal:**
    - **`oma docs verify/sync`:** This is a stand-out feature. It uses deterministic AST parsing to find broken references in documentation and proposes syncs based on code changes without blindly using LLMs for everything.
    - **Skill Boundary Audit:** The TF-IDF logic to prevent agent confusion is a brilliant way to maintain a large library of agents.
    - **Session Quota Caps:** The token/budget management is essential for enterprise-grade agent usage.

### 6. Risk if adopt
- **Complexity:** OMA is feature-rich and carries significant architectural weight. Integrating it fully into `agent-takkub` might introduce more complexity than needed if only a few features are desired.
- **Fast Pace:** The high frequency of updates means that any local modifications (vendoring-in) will require constant maintenance to stay in sync.
- **Dependency on Bun/UV:** It assumes a specific toolchain (Bun, UV, Serena) which might not align with all user environments.

---

### Final Recommendation
**Adopt as a backend / Selective Vendoring.**

1. **Integration:** `agent-takkub` should ideally support OMA as a "First-Class Agent Provider".
2. **Feature Extraction:** If full integration is too heavy, the `oma docs` logic and the `TF-IDF skill audit` logic should be vendored into `agent-takkub` as core utilities.
3. **Observation:** Keep a close watch on their "Scaling Laws of Skills" implementation, as they are ahead of the curve in automated agent management.
