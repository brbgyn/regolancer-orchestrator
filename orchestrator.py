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

# True = teste | False = produção
DRY_RUN = False

REGOLANCER_BIN = "/home/admin/regolancer-orchestrator/regolancer"
TEMPLATE_FILE = "/home/admin/regolancer-orchestrator/config.template.json"
RUN_FOREVER = True
SLEEP_SECONDS = 5
MAX_WORKERS = 1

ENABLE_FILE_LOGS = False
LOG_DIR = "/home/admin/regolancer-orchestrator/logs"
os.makedirs(LOG_DIR, exist_ok=True)

SUCCESS_REBAL_FILE = "/home/admin/regolancer-orchestrator/success-rebal.csv"
SUCCESS_STATE_FILE = "/home/admin/regolancer-orchestrator/last_success_offset.txt"

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

def run_regolancer_with_live_logs(cmd, worker_id, prefix):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    logf = None
    if ENABLE_FILE_LOGS:
        log_path = f"{LOG_DIR}/W{worker_id}.log"
        logf = open(log_path, "a")
        logf.write(f"\n=== START ===\n")
        logf.flush()

    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue

        # stdout em tempo real (journalctl)
        print(f"[W{worker_id}] {prefix} | {line}")

        # log em arquivo (opcional)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    rc = proc.wait()

    if logf:
        logf.write(f"=== END exit={rc} ===\n")
        logf.close()

    return rc

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

        print(f"[W{worker_id}] === CONFIG JSON ===")
        print(json.dumps(cfg, indent=2))
        print(f"[W{worker_id}] === END CONFIG ===")

        print(f"[W{worker_id}] START regolancer")

        exit_code = run_regolancer_with_live_logs(
            [REGOLANCER_BIN, "--config", tmp.name],
            worker_id,
            prefix=f"{src['alias']} → {tgt['alias']}"
        )

        print(f"[W{worker_id}] END regolancer (exit={exit_code})")

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

async def main():
    channels = await load_channels()
    pairs = build_pairs(channels)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for i, pair in enumerate(pairs, start=1):
            executor.submit(run_pair, i, pair)


async def one_cycle():
    channels = await load_channels()
    pairs = build_pairs(channels)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for i, pair in enumerate(pairs, start=1):
            executor.submit(run_pair, i, pair)

def format_rebalance_msg(csv_line):
    return (
        "☯️  ⚡ by Regolancer-Orchestrator"
        #f"`{csv_line}`"
    )

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

def run_loop():
    print("=== REGOLANCER ORCHESTRATOR STARTED ===")

    while True:
        try:
            # 1️⃣ Executa um ciclo completo de rebalances
            asyncio.run(one_cycle())

            # 2️⃣ APÓS o ciclo: verificar novos sucessos
            new_rebalances = read_new_rebalances()

            for line in new_rebalances:
                msg = format_rebalance_msg(line)
                send_telegram(msg)
        except KeyboardInterrupt:
            print("=== STOP REQUESTED (CTRL+C) ===")
            break
        except Exception as e:
            print("=== ERROR IN ORCHESTRATOR LOOP ===")
            traceback.print_exc()

        print(f"=== CYCLE FINISHED — sleeping {SLEEP_SECONDS}s ===")
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    run_loop()
