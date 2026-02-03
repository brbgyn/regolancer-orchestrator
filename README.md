# ⚡ Regolancer Orchestrator

The **Regolancer-Orchestrator** wraps the `regolancer` CLI and LNDg to run automated,
continuous rebalance cycles on a Lightning node.

It pulls channel data from LNDg, selects source and target channel pairs based on
LNDg auto-rebalance targets, generates a temporary `regolancer` config for each
pair, runs the CLI, sends Telegram notifications for successful rebalances, and
produces a detailed daily rebalance report.

The orchestrator is designed to run **24/7 for years**, with special care taken
to minimize CPU usage, disk writes, thermal load, and log noise.

---

## What it does

- Reads open and active channels from the LNDg API.
- Builds source and target channel pairs using LNDg targets and live balances.
- Runs `regolancer` for each pair with a generated config.
- Enforces per-worker cycle timeouts to avoid stale decisions.
- Optionally randomizes pair order for fair scheduling under time limits.
- Tracks successes in CSV files and sends Telegram notifications.
- Produces a daily report with historical comparisons.

---

## Repository layout

- `orchestrator.py` – main daemon running rebalance workers, scheduler and notifier.
- `lndg_api.py` – LNDg channel fetch and normalization.
- `logic.py` – pairing logic and target percentage calculations.
- `logging_utils.py` – compact pair logging helper.
- `report.py` – daily and historical CSV summary writer.
- `config.template.json` – base `regolancer` config with LND connection details.
- `regolancer` – CLI binary.
- `.env.example` – fully documented environment configuration.
- `systemd/regolancer-orchestrator.service` – systemd unit template.

---

## Prerequisites

- Linux host
- Fully synced LND node
- LNDg running with API enabled
- Working `regolancer` binary
- Python **3.9+**
- Telegram bot token and chat ID

---

## Installation

```bash
git clone https://github.com/your/repo.git
cd regolancer-orchestrator

python -m venv venv
./venv/bin/pip install -r requirements.txt
chmod +x regolancer
cp .env.example .env
```

Edit `.env` and `config.template.json`, then run:

```bash
./venv/bin/python orchestrator.py
```

---

## Environment configuration (`.env`)

All runtime behavior is controlled via environment variables.
Below is a full description of **every supported option**.

---

### DRY RUN

```env
DRY_RUN=false
```

When `true`, `regolancer` is **not executed**.
Use for testing configuration and pairing logic.

---

### LNDg API

```env
LNDG_BASE_URL=http://localhost:8889
LNDG_USER=
LNDG_PASS=
```

Credentials and base URL used to fetch channel data from LNDg.

---

### Telegram

```env
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```

Used for:
- Successful rebalance notifications
- Daily report error alerts

---

### Notifications & reports

```env
SEND_REBALANCE_MSG=TRUE
ENABLE_DAILY_REPORT=TRUE
```

- `SEND_REBALANCE_MSG` – send Telegram message on each successful rebalance.
- `ENABLE_DAILY_REPORT` – run daily report automatically at 23:59.

---

### Regolancer debug options

```env
REGOLANCER_LIVE_LOGS=FALSE
LOG_TEMPLATE_CONFIG=FALSE
```

- `REGOLANCER_LIVE_LOGS`
  - Streams regolancer stdout/stderr.
  - High CPU and log noise. Debug only.

- `LOG_TEMPLATE_CONFIG`
  - Prints generated JSON configs.
  - Debug only.

---

### Rebalance amount strategy

```env
AMOUNT_INITIAL=19532
AMOUNT_INCREASE_PERCENT=200
AMOUNT_EVERY_ROUNDS=1
AMOUNT_MAX_INCREASES=2
```

Controls how rebalance amounts evolve over time.

- Start with `AMOUNT_INITIAL`
- Increase by `AMOUNT_INCREASE_PERCENT` every `AMOUNT_EVERY_ROUNDS`
- Reset after `AMOUNT_MAX_INCREASES`

---

### Worker execution

```env
RUN_FOREVER=true
SLEEP_SECONDS=5
MAX_WORKERS=2
MAX_CYCLE_SECONDS=300
```

- `RUN_FOREVER` – keep workers running indefinitely.
- `SLEEP_SECONDS` – delay between cycles.
- `MAX_WORKERS` – number of parallel workers.
- `MAX_CYCLE_SECONDS` – abort and restart cycle if exceeded.

---

### Scheduling behavior

```env
RANDOMIZE_PAIRS=TRUE
```

Randomizes pair order per cycle to avoid starvation
when cycles time out before all pairs are processed.

---

### Logging & performance

```env
LOG_OPERATIONAL=FALSE
ENABLE_FILE_LOGS=FALSE
```

- `LOG_OPERATIONAL`
  - Enables verbose per-pair logs.
  - Strongly recommended `FALSE` in production.

- `ENABLE_FILE_LOGS`
  - Reserved for future extensions.

---

## Error logging

All critical errors are written to:

```
errors.log
```

Includes:
- Unhandled exceptions
- Cycle timeouts
- Scheduler and notifier failures

---

## Systemd service

```bash
sudo cp systemd/regolancer-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now regolancer-orchestrator
journalctl -u regolancer-orchestrator -f
```

---

## Final notes

This project prioritizes:
- Correctness over speed
- Fresh channel data
- Fair scheduling
- Minimal CPU, IO and thermal footprint

Designed for long-term, always-on Lightning nodes.
