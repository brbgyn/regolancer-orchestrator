# Regolancer Orchestrator

This repository wraps the `regolancer` CLI and LNDg to run automated, continuous
rebalance cycles on a Lightning node. It pulls channel data from LNDg, selects
source and target channels based on LNDg auto-rebalance targets, generates a
temporary `regolancer` config for each pair, runs the CLI, and optionally
notifies Telegram about successful rebalances.

## What it does
- Reads open and active channels from the LNDg API.
- Builds source and target channel pairs using LNDg targets and live balances.
- Runs `regolancer` for each pair with a generated config.
- Tracks successes in a CSV file and sends Telegram notifications.
- Optionally writes a daily report with total rebalances vs orchestrator share.

## Repository layout
- `orchestrator.py` main loop that runs rebalance cycles and Telegram alerts.
- `lndg_api.py` LNDg channel fetch and normalization.
- `logic.py` pairing logic and target percentages.
- `logging_utils.py` compact pair logging helper.
- `report.py` daily CSV summary writer.
- `config.template.json` base `regolancer` config with LND connection details.
- `regolancer` CLI binary (replace with your own build if needed).
- `requirements.txt` Python runtime deps.
- `.env.example` sample environment file for LNDg and Telegram.
- `systemd/regolancer-orchestrator.service` systemd unit template.

## How pairing works
Each cycle loads LNDg channels and calculates an effective local balance
including pending outbound HTLCs. Pairs are built as follows:
- Source channels are NOT auto-rebalanced in LNDg and have local_pct greater
  than `ar_out_target`.
- Target channels ARE auto-rebalanced in LNDg and have local_pct lower than
  `100 - ar_in_target`.
- For each source/target pair, `pfrom` is `100 - ar_out_target` and `pto` is
  `100 - ar_in_target`.

This means sources are channels you want to drain (above their outbound target),
and targets are channels you want to fill (below their inbound target).

## Prerequisites
- A Linux host (paths and binary defaults assume Linux).
- A synced LND node with macaroon and TLS cert available.
- LNDg running with API access enabled.
- A working `regolancer` CLI binary.
- Python 3.9+.
- Telegram bot token and chat ID (mandatory with current code).

## Installation
1. Clone the repo and move into it.
2. Create a virtual environment and install deps:
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
5. Create an environment file from the example (see Configuration).
6. Run the orchestrator:
   ```
   ./venv/bin/python orchestrator.py
   ```

## Configuration

### Environment variables
`orchestrator.py` requires these to start:
- `TELEGRAM_TOKEN` Telegram bot token.
- `TELEGRAM_CHAT_ID` Telegram chat ID to receive messages.
- `LNDG_USER` LNDg API username.
- `LNDG_PASS` LNDg API password.

`report.py` also uses:
- `LNDG_BASE_URL` Base URL of the LNDg API, for example `http://localhost:8889`.

Note: `lndg_api.py` currently hardcodes the base URL to `http://localhost:8889`.
If your LNDg API is elsewhere, edit `LNDG_BASE_URL` in `lndg_api.py` as well.

### Environment file (`.env`)
This repo ships with `.env.example`. Copy it to `.env` and fill in values:
```
cp .env.example .env
```
You can load it in your shell before running:
```
set -a
source .env
set +a
```
If you use the systemd unit below, it reads `.env` automatically via
`EnvironmentFile`.

### Paths and constants in `orchestrator.py`
These are defaulted to `/home/admin/regolancer-orchestrator` and should be
updated for your deployment:
- `REGOLANCER_BIN` path to the CLI.
- `TEMPLATE_FILE` path to `config.template.json`.
- `LOG_DIR` directory for per-worker logs.
- `SUCCESS_REBAL_FILE` CSV written by `regolancer` (must match `stat` in config).
- `SUCCESS_STATE_FILE` pointer for Telegram notification offsets.

Operational settings:
- `DRY_RUN` if `True`, prints configs and skips execution.
- `SLEEP_SECONDS` delay between cycles.
- `MAX_WORKERS` worker threads, for parallel pairs.
- `ENABLE_FILE_LOGS` write stdout to `logs/W{worker_id}.log`.

### `config.template.json`
This file is the base config passed to `regolancer`, and is mutated per pair.
Keep the following fields aligned with `orchestrator.py`:
- `stat` must point to the same `SUCCESS_REBAL_FILE`.
- `from` and `to` are overwritten each run.
- `pfrom` and `pto` are injected per pair.

### Excluding channels
To exclude channel IDs from consideration, add them to `EXCLUSION_LIST` in
`lndg_api.py`.

## Usage
Run a continuous orchestration loop:
```
./venv/bin/python orchestrator.py
```

Expected output:
- Pair selection logs like `SRC alias (LOCAL x%) -> TGT alias (LOCAL y%)`.
- Live `regolancer` stdout for each worker.
- Cycle start/finish markers with sleep timers.

### Dry-run mode
Set `DRY_RUN = True` in `orchestrator.py` to validate pairing and config
generation without running `regolancer`.

### Daily report
`report.py` produces a daily CSV showing the share of node rebalances produced
by the orchestrator. It only writes once per day.
```
./venv/bin/python report.py
```

Outputs:
- `daily-report.csv` with date, total node rebalances, orchestrator count,
  and percentage.

### Systemd service (template)
The template in `systemd/regolancer-orchestrator.service` assumes the repo lives
at `/home/admin/regolancer-orchestrator` and runs under the `admin` user. Copy
it to `/etc/systemd/system/`, edit paths/user as needed, then enable it:
```
sudo cp systemd/regolancer-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now regolancer-orchestrator
```
Logs will be available via:
```
journalctl -u regolancer-orchestrator -f
```

## Operational notes
- LNDg API authentication uses HTTP Basic Auth with `LNDG_USER` and `LNDG_PASS`.
- Only open and active channels are considered.
- Effective local balance includes pending outbound HTLCs.
- Telegram notifications are sent for new lines appended to the `stat` CSV.
- This project assumes `regolancer` writes a CSV line per successful rebalance
  to the `stat` file configured in `config.template.json`.

## Troubleshooting
- If you see `Missing required env var`, export the listed variables.
- If LNDg API calls fail, verify credentials and base URL.
- If `regolancer` cannot connect, validate macaroon and TLS paths in
  `config.template.json`.
