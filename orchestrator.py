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
LAST_REPORT_FILE = "/home/admin/regolancer-orchestrator/last_report_date.txt"
LAST_REPORT_ERROR_FILE = "/home/admin/regolancer-orchestrator/last_report_error_date.txt"
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

        stdout_target = subprocess.PIPE if REGOLANCER_LIVE_LOGS else subprocess.DEVNULL
        stderr_target = subprocess.STDOUT if REGOLANCER_LIVE_LOGS else subprocess.DEVNULL

        proc = subprocess.Popen(
            [REGOLANCER_BIN, "--config", tmp.name],
            stdout=stdout_target,
            stderr=stderr_target,
            text=REGOLANCER_LIVE_LOGS,  # só precisa se for ler
            bufsize=1 if REGOLANCER_LIVE_LOGS else 0
        )

        if REGOLANCER_LIVE_LOGS and proc.stdout is not None:
            for line in proc.stdout:
                print(f"{prefix} {line.rstrip()}")

        proc.wait()

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
# MESSAGE
# =========================

def format_rebalance_msg(csv_line):
    try:
        parts = csv_line.split(",")
        amount_msat = int(parts[3])
        amount_sat = amount_msat // 1000
        amount_fmt = f"{amount_sat:,}"
    except Exception:
        amount_fmt = "?"

    return f"☯️ ⚡ {amount_fmt} by Regolancer-Orchestrator"

def telegram_notifier_loop():
    print("[TELEGRAM] notifier started")

    while True:
        try:
            new_rebalances = read_new_rebalances()

            for line in new_rebalances:
                if SEND_REBALANCE_MSG:
                    msg = format_rebalance_msg(line)
                    send_telegram(msg)

        except Exception:
            print("[TELEGRAM] ERROR")
            traceback.print_exc()

        time.sleep(30)

# =========================
# DAILY REPORT (23:59)
# =========================

def read_last_report_date():
    if not os.path.exists(LAST_REPORT_FILE):
        return None
    try:
        with open(LAST_REPORT_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def write_last_report_date(date_str):
    try:
        with open(LAST_REPORT_FILE, "w") as f:
            f.write(date_str)
    except Exception as e:
        print(f"[ERROR] Failed to write LAST_REPORT_FILE: {e}")

def read_last_report_error_date():
    if not os.path.exists(LAST_REPORT_ERROR_FILE):
        return None
    try:
        with open(LAST_REPORT_ERROR_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def write_last_report_error_date(date_str):
    try:
        with open(LAST_REPORT_ERROR_FILE, "w") as f:
            f.write(date_str)
    except Exception as e:
        print(f"[ERROR] Failed to write LAST_REPORT_ERROR_FILE: {e}")


def clear_last_report_error_date():
    try:
        if os.path.exists(LAST_REPORT_ERROR_FILE):
            os.remove(LAST_REPORT_ERROR_FILE)
    except Exception as e:
        print(f"[ERROR] Failed to clear LAST_REPORT_ERROR_FILE: {e}")

def maybe_run_daily_report():
    if os.getenv("ENABLE_DAILY_REPORT", "TRUE").upper() != "TRUE":
        return

    now = datetime.now()
    today_str = now.date().isoformat()

    # só depois das 23:59
    if now.hour < 23 or (now.hour == 23 and now.minute < 59):
        return

    # já rodou com sucesso hoje
    if read_last_report_date() == today_str:
        return

    print("[INFO] Attempting to run daily report.py")

    try:
        subprocess.run(
            [sys.executable, REPORT_PY],
            check=True,
            capture_output=True,
            text=True
        )

        print("[INFO] daily report.py executed successfully")

        write_last_report_date(today_str)
        clear_last_report_error_date()  # limpa estado de erro

    except subprocess.CalledProcessError as e:
        print("[ERROR] report.py failed")

        stderr = (e.stderr or "").strip()
        stdout = (e.stdout or "").strip()

        # evita spam: só 1 telegram por dia de erro
        last_error_date = read_last_report_error_date()
        if last_error_date != today_str:
            msg = (
                "❌ Daily report failed\n\n"
                f"📅 Date: {today_str}\n\n"
                f"STDERR:\n{stderr[:350]}\n\n"
                f"STDOUT:\n{stdout[:350]}"
            )
            send_telegram(msg)
            write_last_report_error_date(today_str)

        # continua tentando a cada 30s

def scheduler_loop():
    print("[SCHEDULER] daily report scheduler started")

    while True:
        try:
            maybe_run_daily_report()
        except Exception:
            print("[SCHEDULER] ERROR")
            traceback.print_exc()

        time.sleep(30)  # checa a cada 30s

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    print("=== REGOLANCER ORCHESTRATOR (MULTI-WORKER MODE) ===")

    # workers
    for wid in range(1, MAX_WORKERS + 1):
        threading.Thread(
            target=worker_loop,
            args=(wid,),
            daemon=True
        ).start()

    # telegram notifier
    threading.Thread(
        target=telegram_notifier_loop,
        daemon=True
    ).start()

    # daily report scheduler
    threading.Thread(
        target=scheduler_loop,
        daemon=True
    ).start()

    # keep main alive
    while True:
        time.sleep(3600)
