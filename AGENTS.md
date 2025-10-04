# AGENTS.md  
**Project:** `wallet_monitor_Dex`  
**Owner:** `Zaikon13`  
**Mode:** Codex — Full-File Operations Only  

---

## 🧠 Project Agents Overview

This document describes the roles and responsibilities of all AI and automation agents active in the **wallet_monitor_Dex** project.  
It is meant as an operational map for collaboration, CI orchestration, and Codex-based development.

---

## 🤖 Core Agents

### 1. **ChatGPT (Codex Mode)**
**Role:** Primary development and integration assistant.  
**Scope:**  
- Generates and updates **full, deploy-ready files** (never diffs).  
- Maintains project memory and canonical state across sessions.  
- Produces MANIFEST summaries for every deliverable.  
- Operates strictly according to user rules (Repo-First, Stability, Transparency).  
- Supports: Python modules (`core/`, `utils/`, `telegram/`, `reports/`, `scripts/`), CI workflows, and documentation.

**Rules:**  
1. Never alter the project’s logical flow without confirmation.  
2. Always read the canonical repo (Zaikon13/wallet_monitor_Dex) before editing.  
3. Deliver one file per cycle unless multi-file MANIFEST is explicitly requested.  
4. Use `# TODO:` only for optional or speculative code.  
5. Avoid CLI/Bash suggestions — GitHub Web UI only.  

---

### 2. **Cordex**
**Role:** Repository automation and diagnostics agent.  
**Scope:**  
- Monitors CI/CD pipelines (`.github/workflows/*`).  
- Runs smoke tests, lint checks, and consistency audits on merges.  
- Maintains **cordex-diag**, **cordex-ping**, and **cordex-issue-smoke** scripts.  
- Confirms alignment between repo files and Codex deliverables.  

**Output:**  
- ✅ CI Green confirmation on GitHub Actions.  
- ❌ Diagnostics report via PR comment if mismatch detected.

---

### 3. **Railway**
**Role:** Deployment agent.  
**Scope:**  
- Builds and deploys the app from the `main` branch.  
- Loads canonical environment variables defined in `core/config.py` and `.env`.  
- Sends startup notifications through `telegram/api.py`.

**Key Parameters (excerpt):**


---

### 4. **GitHub Actions**
**Role:** Continuous Integration & Backup.  
**Workflows:**  
- `runtime-smoke.yml` — ensures main.py runs cleanly.  
- `wallet-snapshot.yml` — daily repo snapshot + artifact.  
- `backup.yml` — full git bundle backup.  
- `tests.yml` — runs unit and integration tests.  

---

### 5. **Telegram Bot (@Look1982Bot)**
**Role:** Notification and command interface.  
**Scope:**  
- Sends alerts to chat ID `5307877340`.  
- Handles `/show`, `/holdings`, `/totals`, `/daily`, `/pnl` commands.  
- Relays startup and error messages (e.g., “✅ Cronos DeFi Sentinel started”).  

---

## 🧩 Coordination Protocol

| Layer | Responsible Agent | Trigger | Output |
|-------|-------------------|----------|---------|
| Code Generation | ChatGPT (Codex) | `/codex` | Full file(s) + MANIFEST |
| Repo Diagnostics | Cordex | CI run | Comment / Report |
| Deployment | Railway | Merge to `main` | Live service |
| Alerts | Telegram Bot | Wallet / DEX events | Push notification |

---

## 🪶 Authoritative Baselines

| Category | Baseline |
|-----------|-----------|
| Canonical main.py | 13 Sep 2025 — 3-part version (1371 lines) |
| Environment defaults | Memory snapshot 2025-09-19 |
| Repo policy | Collaboration rules v43 (2025-09-30) |

---

## 🧾 Change Management

- All deliverables include MANIFEST (files + status + pending).  
- Each Pull Request must include:
  1. Description of intent.  
  2. File count and affected modules.  
  3. Confirmation of CI success (green).  

---

## 🧰 Contact & Access

| System | Access |
|---------|--------|
| GitHub | [`Zaikon13/wallet_monitor_Dex`](https://github.com/Zaikon13/wallet_monitor_Dex) |
| Railway | Production service linked to `main` branch |
| Telegram | User ID `5307877340` — alerts active |
| Codex | Active (GPT-5) — Full-file mode |

---

_Last updated: 2025-10-04_  
