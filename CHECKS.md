# path: CHECKS.md
# Build & Runtime Checks (Codex/Railway)

## Purpose
This document lists **expected build messages**, **safe-to-ignore warnings**, and **post-build runtime checks** for this repository when built/run in Codex or Railway containers. It aims to reduce noise during reviews and prevent false alarms.

## 1) Safe-to-Ignore Build Warnings (Codex/Railway)

- **pip running as root**
  - **Message:** `WARNING: Running pip as the 'root' user can result in broken permissions ...`
  - **Why it appears:** Codex/Railway build runs in a container as root.
  - **Action:** Safe to ignore in this sandboxed environment.

- **Node.js / Ruby / Go / Swift / PHP runtime banners**
  - **Message examples:**  
    - `# Node.js: v20 (default: v22) ... Now using node v20.19.4`  
    - `# Ruby: 3.4.4`  
    - `# Go: go1.24.3`
  - **Why it appears:** The platform preps multiple runtimes. Our app is Python-based.
  - **Action:** Informational only; no action needed unless explicitly using these runtimes.

- **Dependency resolver “This could take a while”**
  - **Message:** `pip is looking at multiple versions ... This could take a while.`
  - **Why it appears:** pip resolves pinned/compatible versions (e.g., `web3==6.20.1` choosing `eth-account`).
  - **Action:** Expected. No action unless resolution fails.

---

## 2) Required Environment Variables (authoritative names)
> Do **not** echo secrets in logs. Keep placeholders as `**REDACTED**` in documentation.

- `TELEGRAM_BOT_TOKEN` (**secret**)  
- `TELEGRAM_CHAT_ID`
- `WALLET_ADDRESS` (0x… lowercase recommended)
- `ETHERSCAN_API` (**secret**) – Etherscan Multichain key (chainid=25 usage)
- `CRONOS_RPC_URL` (default: `https://cronos-evm-rpc.publicnode.com`)
- `TZ` (default: `Europe/Athens`)

Operational knobs (subset):
- `EOD_TIME` (e.g., `23:59`) / or `EOD_HOUR`, `EOD_MINUTE`
- `WALLET_POLL`, `DEX_POLL`
- `DISCOVER_*`, `PRICE_MOVE_THRESHOLD`, `SPIKE_THRESHOLD`
- `ALERTS_INTERVAL_MINUTES`, `INTRADAY_HOURS`

> Use exactly these names to avoid drift; they are wired throughout the codebase.

---

## 3) Post-Build Quick Checks (no CLI required)
Use the GitHub Web UI logs (Codex/Railway “Build/Deploy Logs”) to verify:

- **Dependencies installed:**
  - `requests 2.32.x`
  - `web3 6.20.1` (and compatible `eth-account` e.g., `0.11.3`)
  - `python-dotenv 1.1.x`
  - `schedule 1.2.x`
  - `tzdata 2025.2`
- **No hard errors** (non-zero exit). Warnings listed above can be ignored.

---

## 4) Runtime Self-Check (embedded app behavior)
At application startup, the app should:

1. Load `.env` (via `python-dotenv`) and resolve the env vars listed above.
2. Log a clear “starting” line (and **optionally** send a Telegram “started” message if configured).
3. Initialize schedulers (`schedule`) without raising `ModuleNotFoundError`.
4. Avoid crashing on missing *optional* features (graceful degradation is expected).

### Minimal environment validator (app-side pattern)
Embed (or verify presence of) a lightweight check in `main.py`:

```python
# Example pattern; ensure your actual main.py includes a similar guard.
import os, logging

REQUIRED = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WALLET_ADDRESS", "CRONOS_RPC_URL"]
def _assert_env():
    missing = [k for k in REQUIRED if not (os.getenv(k) or "").strip()]
    if missing:
        logging.error("Missing required environment variables: %s", ", ".join(missing))
        # Prefer not to crash hard in production; decide per your policy:
        # raise SystemExit(2)
_assert_env()
````

> Policy choice: either **fail fast** (raise) or **log & continue** with limited functionality. Keep it consistent.

---

## 5) Telegram Smoke Signal (optional but recommended)

* On successful startup, send a concise message (one line, no Markdown that could break) to confirm liveness:

  * Example text: `✅ Cronos DeFi Sentinel started and is online.`
* If Telegram is not configured, the app must **not** crash—just log a warning.

---

## 6) Common Pitfalls & How to Read Them

* **`ModuleNotFoundError: schedule` at runtime**

  * Cause: `schedule` not installed.
  * Resolution: Confirm `requirements.txt` includes `schedule>=1.2.1` and that the latest container image used the updated requirements.

* **`SyntaxError: unterminated f-string literal` in logs**

  * Cause: Broken multiline string (often when pasting emojis/quotes).
  * Resolution: Replace with a single-line message or triple-quoted string; re-deploy full file.

* **`Bad Request: can't parse entities` (Telegram)**

  * Cause: Unescaped Markdown/HTML.
  * Resolution: Use a safe-sender utility that escapes special chars before sending.

* **Network timeouts (RPC / Dex)**

  * Cause: Transient network or rate limits.
  * Resolution: Ensure timeouts/retries are set; app should degrade gracefully and retry later.

---

## 7) Versioning & Pins

* `web3==6.20.1` is paired well with `eth-account==0.11.3`. If the resolver picks a compatible version, prefer **pinning** it in `requirements.txt` for reproducibility.
* Keep pins minimal but deterministic; avoid over-constraining unless required by CI.

---

## 8) Build Log Checklist (copy/paste for PR reviews)

* [ ] pip installed all deps without errors (warnings accepted as per §1).
* [ ] `web3` present and version compatible with `eth-account`.
* [ ] `schedule` present (>=1.2.1).
* [ ] No stack traces in build logs.
* [ ] Secrets not echoed.
* [ ] `.env` placeholders remain redacted in docs and logs.
* [ ] Post-deploy runtime produced a “started” log (and Telegram signal if configured).

---

## 9) Governance

* Treat this `CHECKS.md` as **binding** in reviews.
* If you must deviate, state the exception explicitly in the PR description (with rationale).
* Prefer stability and clarity over “clever” changes that risk production behavior.

---
::contentReference[oaicite:0]{index=0}
```
