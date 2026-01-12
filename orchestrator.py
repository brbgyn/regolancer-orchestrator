import requests
import os
import asyncio
import time
import traceback
import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from lndg_api import load_channels
from logic import build_pairs
from logging_utils import log_pair

# =========================
# CONFIG
# =========================

DRY_RUN = False

REGOLANCER_BIN = "/home/admin/regolancer-orchestrator/regolancer"
TEMPLATE_FILE = "/home/admin/regolancer-orchestrator/config.template.json"
REPORT_PY = "/home/admin/regolancer-orchestrator/report.py"

RUN_FOREVER = True
SLEEP_SECONDS = 5
MAX_WORKERS = 1

ENABLE_FILE_LOGS = False
LOG_DIR = "/home/admin/regolancer-orchestrator/logs"
os.makedirs(LOG_DIR, exist_ok=True)

SUCCESS_REBAL_FILE = "/home/admin/regolancer-orchestrator/success-rebal.csv"
SUCCESS_STATE_FILE = "/home/admin/regolancer-orchestrator/last_success_offset.txt"

# estado do report diário
LAST_DAILY_REPORT_FILE = "/home/admin/regolancer-orchestrator/last_daily_report_date.txt"

# =========================
# ENV
# =========================

def require_env(name):
    if not os.getenv(name):
        raise RuntimeError(f"Missing required env var: {name}")

require_env("TELEGRAM_TOKEN")
require_env("TELEGRAM_CHAT_ID")
require_env("LNDG_USER")
require_env("LNDG_PASS")

# =========================
# TELEGRAM
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

def send_telegram(msg):
    try:
        requests.post(
            TELEGRAM_API_URL,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "disable_web_page_preview": True
            },
            timeout=5
        )
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")

# =========================
# REGOLANCER EXECUTION
# =========================

def run_regolancer_with_live_logs(cmd, worker_id, prefix):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print(f"[W{worker_id}] {prefix} | {line}")

    return proc.wait()

def run_pair(worker_id, pair):
    with open(TEMPLATE_FILE) as f:
        cfg = json.load(f)

    src = pair["source"]
    tgt = pair["target"]

    cfg["from"] = [src["pubkey"]]
    cfg["to"]   = [tgt["pubkey"]]
    cfg["pfrom"] = pair["pfrom"]
    cfg["pto"]   = pair["pto"]

    log_pair(worker_id, src, tgt)

    with tempfile.NamedTemporaryFile("w", suffix=".json") as tmp:
        json.dump(cfg, tmp, indent=2)
        tmp.flush()

        if DRY_RUN:
            print(f"[W{worker_id}] DRY-RUN → regolancer NÃO executado")
            return

        print(f"[W{worker_id}] START regolancer")

        exit_code = run_regolancer_with_live_logs(
            [REGOLANCER_BIN, "--config", tmp.name],
            worker_id,
            prefix=f"{src['alias']} → {tgt['alias']}"
        )

        print(f"[W{worker_id}] END regolancer (exit={exit_code})")

# =========================
# SUCCESS REBAL READER
# =========================

def read_new_rebalances():
    last_offset = 0

    if os.path.exists(SUCCESS_STATE_FILE):
        with open(SUCCESS_STATE_FILE, "r") as f:
            try:
                last_offset = int(f.read().strip())
            except ValueError:
                last_offset = 0

    if not os.path.exists(SUCCESS_REBAL_FILE):
        return []

    new_lines = []

    with open(SUCCESS_REBAL_FILE, "r") as f:
        f.seek(last_offset)
        for line in f:
            line = line.strip()
            if line:
                new_lines.append(line)

        new_offset = f.tell()

    with open(SUCCESS_STATE_FILE, "w") as f:
        f.write(str(new_offset))

    return new_lines

# =========================
# DAILY REPORT (23:59)
# =========================

def maybe_run_daily_report():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # só às 23:59
    if not (now.hour == 23 and now.minute == 59):
        return

    last_run = None
    if os.path.exists(LAST_DAILY_REPORT_FILE):
        with open(LAST_DAILY_REPORT_FILE, "r") as f:
            last_run = f.read().strip()

    if last_run == today_str:
        return  # já rodou hoje

    print("[INFO] Running daily report.py")

    try:
        subprocess.run(
            ["python3", REPORT_PY],
            check=True
        )

        with open(LAST_DAILY_REPORT_FILE, "w") as f:
            f.write(today_str)

    except Exception as e:
        print(f"[ERROR] Failed to run report.py: {e}")

# =========================
# MESSAGE
# =========================

def format_rebalance_msg(csv_line):
    return "☯️  ⚡ by Regolancer-Orchestrator"

# =========================
# MAIN LOOP
# =========================

async def one_cycle():
    channels = await load_channels()
    pairs = build_pairs(channels)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for i, pair in enumerate(pairs, start=1):
            executor.submit(run_pair, i, pair)

def run_loop():
    print("=== REGOLANCER ORCHESTRATOR STARTED ===")

    while True:
        try:
            # executa rebalances
            asyncio.run(one_cycle())

            # envia mensagens de sucesso
            new_rebalances = read_new_rebalances()
            for line in new_rebalances:
                msg = format_rebalance_msg(line)
                send_telegram(msg)

            # ⏰ relatório diário
            maybe_run_daily_report()

        except KeyboardInterrupt:
            print("=== STOP REQUESTED (CTRL+C) ===")
            break
        except Exception:
            print("=== ERROR IN ORCHESTRATOR LOOP ===")
            traceback.print_exc()

        print(f"=== CYCLE FINISHED — sleeping {SLEEP_SECONDS}s ===")
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    run_loop()
