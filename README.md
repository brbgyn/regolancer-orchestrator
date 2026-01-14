# ⚡ Regolancer Orchestrator

The **Regolancer-Orchestrator** wraps the `regolancer` CLI and LNDg to run automated,
continuous rebalance cycles on a Lightning node.

It pulls channel data from LNDg, selects source and target channels based on
LNDg auto-rebalance targets, generates a temporary `regolancer` config for each
pair, runs the CLI, sends Telegram notifications for successful rebalances, and
produces a detailed daily rebalance report.

---

## What it does

- Reads open and active channels from the LNDg API.
- Builds source and target channel pairs using LNDg targets and live balances.
- Runs `regolancer` for each pair with a generated config.
- Tracks successes in CSV files and sends Telegram notifications.
- Writes a daily report comparing:
  - Total LNDg rebalances
  - Total Regolancer-Orchestrator rebalances
  - Relative share percentages
  - Monthly and 12‑month history

---

## Repository layout

- `orchestrator.py` – main loop that runs rebalance cycles and Telegram alerts.
- `lndg_api.py` – LNDg channel fetch and normalization.
- `logic.py` – pairing logic and target percentages.
- `logging_utils.py` – compact pair logging helper.
- `report.py` – daily and historical CSV summary writer.
- `config.template.json` – base `regolancer` config with LND connection details.
- `regolancer` – CLI binary (replace with your own build if needed).
- `requirements.txt` – Python runtime dependencies.
- `.env.example` – sample environment file for LNDg and Telegram.
- `systemd/regolancer-orchestrator.service` – systemd unit template.

---

## How pairing works

Each cycle loads LNDg channels and calculates an effective local balance
including pending outbound HTLCs.

Pairs are built as follows:

- **Source channels**
  - Auto‑rebalance OFF in LNDg
  - `local_pct` greater than `ar_out_target`

- **Target channels**
  - Auto‑rebalance ON in LNDg
  - `local_pct` lower than `100 - ar_in_target`

For each source/target pair:
- `pfrom = 100 - ar_out_target`
- `pto = 100 - ar_in_target`

This means:
- Sources are channels you want to drain.
- Targets are channels you want to fill.

---

## Prerequisites

- Linux host.
- A fully synced LND node with macaroon and TLS cert available.
- LNDg running with API access enabled.
- A working `regolancer` CLI binary.
- Python **3.9+**
- Telegram bot token and chat ID.

---

## Installation

1. Clone the repository and enter it.
2. Create a virtual environment and install dependencies:

```
python -m venv venv
./venv/bin/pip install -r requirements.txt
```

3. Ensure the `regolancer` binary is executable:

```
chmod +x ./regolancer
```

4. Update `config.template.json` with your LND connection details:
   - `macaroon_dir`
   - `macaroon_filename`
   - `tlscert`
   - `connect`
   - Adjust amounts and timeouts as needed.

5. Create your environment file:

```
cp .env.example .env
```

6. Run the orchestrator:

```
./venv/bin/python orchestrator.py
```

---

## Configuration

### Environment variables (required)

Used by `orchestrator.py`:

- `TELEGRAM_TOKEN` – Telegram bot token.
- `TELEGRAM_CHAT_ID` – Telegram chat ID.
- `LNDG_USER` – LNDg API username.
- `LNDG_PASS` – LNDg API password.

Used by `report.py`:

- `LNDG_BASE_URL` – Base URL of the LNDg API (example: `http://localhost:8889`).

> Note: `lndg_api.py` defaults to `http://localhost:8889`.

---

### Environment file (`.env`)

Copy the example:

```
cp .env.example .env
```

Load manually if needed:

```
set -a
source .env
set +a
```

Systemd loads it automatically via `EnvironmentFile`.

---

### Optional environment flags

```
DRY_RUN=FALSE
ENABLE_DAILY_REPORT=TRUE
```

- `DRY_RUN=TRUE` – do not execute `regolancer`, only print channel pairs and configs.
- `ENABLE_DAILY_REPORT=FALSE` – disable automatic daily report execution at 23:59.

---

## Paths and constants (`orchestrator.py`)

Defaults assume `/home/admin/regolancer-orchestrator`:

- `REGOLANCER_BIN`
- `TEMPLATE_FILE`
- `LOG_DIR`
- `SUCCESS_REBAL_FILE`
- `SUCCESS_STATE_FILE`

Operational settings:
- `DRY_RUN`
- `SLEEP_SECONDS`
- `MAX_WORKERS`
- `ENABLE_FILE_LOGS`

---

## config.template.json

Base config passed to `regolancer`, mutated per pair.

Important fields:
- `stat` must match `SUCCESS_REBAL_FILE`
- `from` / `to` overwritten each run
- `pfrom` / `pto` injected per pair

---

## Excluding channels

Add channel IDs to `EXCLUSION_LIST` in `lndg_api.py`.

---

## Usage

Run continuous orchestration:

```
./venv/bin/python orchestrator.py
```

Expected output:
- Pair selection logs
- Live `regolancer` stdout
- Cycle start/finish markers

---

## Telegram notifications

Each successful rebalance sends:

```
☯️ ⚡ 125,000 by Regolancer-Orchestrator
```

---

## Daily report

`report.py` generates a daily CSV and Telegram summary.

- Uses LNDg as authoritative source
- Backfills up to 12 months once
- Subsequent runs are incremental

Outputs:
- `daily-report.csv`
- Telegram summary (if enabled)

---

## Example daily Telegram summary

```
📊 regolancer-orchestrator

⚡ Total Rebals Hoje: X,XXX,XXX
☯️ LNDg: X,XXX,XXX (30%)
☯️ Regolancer-Orchestrator: X,XXX,XXX (70%)

📊 Mês Atual

⚡ Total Rebals: X,XXX,XXX
☯️ LNDg: X,XXX,XXX (20%)
☯️ Regolancer-Orchestrator: X,XXX,XXX (80%)

📊 Histórico 12m

☯️ Feb: Total 0 (LNDg 0.00% / Rego 0.00%)
☯️ Mar: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Apr: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ May: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Jun: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Jul: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Aug: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Sep: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Oct: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Nov: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Dec: Total X,XXX,XXX (LNDg 100.00% / Rego 0.00%)
☯️ Jan: Total X,XXX,XXX (LNDg 35% / Rego 65%)
```

---

## Systemd service

Template in `systemd/regolancer-orchestrator.service`.

Install:

```
sudo cp systemd/regolancer-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now regolancer-orchestrator
```

Logs:

```
journalctl -u regolancer-orchestrator -f
```

---

## Troubleshooting

- `Missing required env var` → check `.env`.
- LNDg API errors → verify credentials and base URL.
- `regolancer` connection errors → validate macaroon and TLS paths.

---