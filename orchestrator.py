import requests
import os
import asyncio
import time
import traceback
import json
import subprocess
import tempfile
import threading
import sys
sys.stdout.reconfigure(line_buffering=True)
from datetime import datetime
from dotenv import load_dotenv
from lndg_api import load_channels
from logic import build_pairs
from logging_utils import log_pair

load_dotenv()

# =========================
# ENV HELPERS
# =========================

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")

# =========================
# CONFIG (ENV)
# =========================

RUN_FOREVER       = env_bool("RUN_FOREVER", True)
SLEEP_SECONDS     = int(os.getenv("SLEEP_SECONDS", "5"))
MAX_WORKERS       = int(os.getenv("MAX_WORKERS", "1"))
ENABLE_FILE_LOGS  = env_bool("ENABLE_FILE_LOGS", False)

DRY_RUN               = env_bool("DRY_RUN", True)
REGOLANCER_LIVE_LOGS  = env_bool("REGOLANCER_LIVE_LOGS", True)
LOG_TEMPLATE_CONFIG   = env_bool("LOG_TEMPLATE_CONFIG", False)
SEND_REBALANCE_MSG    = env_bool("SEND_REBALANCE_MSG", True)

REGOLANCER_BIN   = "/home/admin/regolancer-orchestrator/regolancer"
TEMPLATE_FILE    = "/home/admin/regolancer-orchestrator/config.template.json"
REPORT_PY        = "/home/admin/regolancer-orchestrator/report.py"
AMOUNT_STATE_FILE = "/home/admin/regolancer-orchestrator/amount_state.json"

SUCCESS_REBAL_FILE = "/home/admin/regolancer-orchestrator/success-rebal.csv"
SUCCESS_STATE_FILE = "/home/admin/regolancer-orchestrator/last_success_offset.txt"

# =========================
# TELEGRAM
# =========================

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
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
# AMOUNT STATE
# =========================

_amount_lock = threading.Lock()

def advance_cycle_and_get_amount():
    with _amount_lock:
        initial = int(os.getenv("AMOUNT_INITIAL", "10000"))
        percent = float(os.getenv("AMOUNT_INCREASE_PERCENT", "50"))
        every   = int(os.getenv("AMOUNT_EVERY_ROUNDS", "5"))
        max_inc = int(os.getenv("AMOUNT_MAX_INCREASES", "8"))

        state = {"cycle": 0, "increase": 0}

        if os.path.exists(AMOUNT_STATE_FILE):
            try:
                with open(AMOUNT_STATE_FILE) as f:
                    state = json.load(f)
            except Exception:
                pass

        state["cycle"] += 1

        if state["cycle"] % every == 0:
            state["increase"] += 1
            if state["increase"] > max_inc:
                state["increase"] = 0

        amount = initial
        for _ in range(state["increase"]):
            amount = int(amount * (1 + percent / 100))

        with open(AMOUNT_STATE_FILE, "w") as f:
            json.dump(state, f)

        return amount, state

# =========================
# REGOLANCER
# =========================

def run_regolancer(worker_id, pair, amount, pair_id):
    with open(TEMPLATE_FILE) as f:
        cfg = json.load(f)

    src = pair["source"]
    tgt = pair["target"]

    cfg["from"]   = [src["pubkey"]]
    cfg["to"]     = [tgt["pubkey"]]
    cfg["pfrom"]  = pair["pfrom"]
    cfg["pto"]    = pair["pto"]
    cfg["amount"] = amount

    prefix = (
        f"[W{worker_id}] "
        f"[PAIR {pair_id}] "
        f"[AMOUNT {amount:,}] "
        f"[pfrom={pair['pfrom']}%] "
        f"[pto={pair['pto']}%]"
    )

    log_pair(worker_id, src, tgt, amount, prefix=prefix)

    with tempfile.NamedTemporaryFile("w", suffix=".json") as tmp:
        json.dump(cfg, tmp, indent=2)
        tmp.flush()

        if DRY_RUN:
            print(
                f"[W{worker_id}] [PAIR {pair_id}] DRY-RUN → "
                f"{src['alias']} → {tgt['alias']}"
            )
            return

        #print(
        #    f"{prefix} START regolancer "
        #    f"{src['alias']} → {tgt['alias']}"
        #)

        proc = subprocess.Popen(
            [REGOLANCER_BIN, "--config", tmp.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in proc.stdout:
            if REGOLANCER_LIVE_LOGS:
                print(f"{prefix} {line.rstrip()}")

        exit_code = proc.wait()

# =========================
# WORKER LOOP
# =========================

def worker_loop(worker_id):
    print(f"[W{worker_id}] Worker started")

    while RUN_FOREVER:
        try:
            amount, state = advance_cycle_and_get_amount()

            channels = asyncio.run(load_channels())
            pairs = build_pairs(channels)

            pair_counter = 0  # 🔁 RESET A CADA CICLO

            if pairs:
                for pair in pairs:
                    pair_counter += 1
                    run_regolancer(worker_id, pair, amount, pair_counter)

            print(
                f"[W{worker_id}] === CYCLE FINISHED "
                f"(cycle={state['cycle']} amount={amount}) — sleeping {SLEEP_SECONDS}s ==="
            )

        except Exception:
            print(f"[W{worker_id}] ERROR")
            traceback.print_exc()

        time.sleep(SLEEP_SECONDS)

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    print("=== REGOLANCER ORCHESTRATOR (MULTI-WORKER MODE) ===")

    for wid in range(1, MAX_WORKERS + 1):
        threading.Thread(
            target=worker_loop,
            args=(wid,),
            daemon=True
        ).start()

    while True:
        time.sleep(3600)
