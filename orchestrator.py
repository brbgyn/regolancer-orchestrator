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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from lndg_api import load_channels
from logic import build_pairs
from logging_utils import log_pair

CURRENT_CYCLE_AMOUNT = None

# =========================
# ENV HELPERS
# =========================

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")

DRY_RUN = env_bool("DRY_RUN", default=True)
REGOLANCER_LIVE_LOGS = env_bool("REGOLANCER_LIVE_LOGS", default=True)
LOG_TEMPLATE_CONFIG = env_bool("LOG_TEMPLATE_CONFIG", default=False)
SEND_REBALANCE_MSG = env_bool("SEND_REBALANCE_MSG", default=True)

# =========================
# CONFIG
# =========================

load_dotenv()

REGOLANCER_BIN = "/home/admin/regolancer-orchestrator/regolancer"
TEMPLATE_FILE = "/home/admin/regolancer-orchestrator/config.template.json"
REPORT_PY = "/home/admin/regolancer-orchestrator/report.py"
AMOUNT_STATE_FILE = "/home/admin/regolancer-orchestrator/amount_state.json"

RUN_FOREVER = True
SLEEP_SECONDS = 5
MAX_WORKERS = 1

ENABLE_FILE_LOGS = False
REGOLANCER_LIVE_LOGS = env_bool("REGOLANCER_LIVE_LOGS", default=True)
LOG_DIR = "/home/admin/regolancer-orchestrator/logs"
os.makedirs(LOG_DIR, exist_ok=True)

SUCCESS_REBAL_FILE = "/home/admin/regolancer-orchestrator/success-rebal.csv"
SUCCESS_STATE_FILE = "/home/admin/regolancer-orchestrator/last_success_offset.txt"

_last_report_attempt_date = None

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

def advance_cycle_and_get_amount():
    initial = int(os.getenv("AMOUNT_INITIAL", "10000"))
    percent = float(os.getenv("AMOUNT_INCREASE_PERCENT", "50"))
    every = int(os.getenv("AMOUNT_EVERY_ROUNDS", "5"))
    max_inc = int(os.getenv("AMOUNT_MAX_INCREASES", "8"))

    state = {
        "cycle": 0,
        "increase": 0
    }

    if os.path.exists(AMOUNT_STATE_FILE):
        try:
            with open(AMOUNT_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass

    # avança UM ciclo
    state["cycle"] += 1

    # a cada N ciclos, aumenta
    if state["cycle"] % every == 0:
        state["increase"] += 1

        if state["increase"] > max_inc:
            state["increase"] = 0

    # calcula amount
    amount = initial
    for _ in range(state["increase"]):
        amount = int(amount * (1 + percent / 100))

    # salva estado
    with open(AMOUNT_STATE_FILE, "w") as f:
        json.dump(state, f)

    return amount, state

def run_regolancer_with_live_logs(cmd, worker_id, prefix):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    if REGOLANCER_LIVE_LOGS:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"[W{worker_id}] {prefix} | {line}")
    else:
        # consome stdout para não travar o processo
        for _ in proc.stdout:
            pass

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

    # ✅ amount definido pelo ciclo
    cfg["amount"] = CURRENT_CYCLE_AMOUNT

    log_pair(worker_id, src, tgt, CURRENT_CYCLE_AMOUNT)
    if LOG_TEMPLATE_CONFIG:
        print(
            f"[W{worker_id}] CONFIG TEMPLATE "
            f"{src['alias']} → {tgt['alias']}\n"
            f"{json.dumps(cfg, indent=2)}"
        )

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
    print("[DEBUG] maybe_run_daily_report called")

    global _last_report_attempt_date

    # flag via env (default TRUE)
    if os.getenv("ENABLE_DAILY_REPORT", "TRUE").upper() != "TRUE":
        return

    now = datetime.now()
    today = now.date()

    # só depois das 23:59
    if now.hour < 23 or (now.hour == 23 and now.minute < 59):
        return

    # garante que o orchestrator só tente uma vez por dia
    if _last_report_attempt_date == today:
        return

    print("[INFO] Attempting to run daily report.py")

    try:
        subprocess.run(
            [sys.executable, REPORT_PY],
            check=True,
            capture_output=True,
            text=True
        )
        print("[INFO] daily report.py executed")

    except subprocess.CalledProcessError as e:
        print("[ERROR] report.py failed")
        print("stdout:", e.stdout)
        print("stderr:", e.stderr)

    # marca tentativa (independente de sucesso)
    _last_report_attempt_date = today

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

# =========================
# MAIN LOOP
# =========================

async def one_cycle():
    channels = await load_channels()
    pairs = build_pairs(channels)

    if not pairs:
        print("[INFO] No pairs to rebalance this cycle")
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []

        for i, pair in enumerate(pairs, start=1):
            futures.append(
                executor.submit(run_pair, i, pair)
            )

        # 🔒 BLOQUEIA ATÉ TODOS TERMINAREM
        for f in futures:
            f.result()

def run_loop():
    global CURRENT_CYCLE_AMOUNT

    print("=== REGOLANCER ORCHESTRATOR STARTED ===")

    while True:
        try:
            # 🔹 define amount UMA VEZ por ciclo
            CURRENT_CYCLE_AMOUNT, state = advance_cycle_and_get_amount()

            # executa rebalances
            asyncio.run(one_cycle())

            # envia mensagens de sucesso
            new_rebalances = read_new_rebalances()
            for line in new_rebalances:
                if SEND_REBALANCE_MSG:
                    msg = format_rebalance_msg(line)
                    send_telegram(msg)

        except KeyboardInterrupt:
            print("=== STOP REQUESTED (CTRL+C) ===")
            break
        except Exception:
            print("=== ERROR IN ORCHESTRATOR LOOP ===")
            traceback.print_exc()

        print(
                f"[CYCLE {state['cycle']}] "
                f"amount={CURRENT_CYCLE_AMOUNT} "
                f"(increase={state['increase']})"
            )

        print(f"=== CYCLE FINISHED — sleeping {SLEEP_SECONDS}s ===")
        time.sleep(SLEEP_SECONDS)

def scheduler_loop():
    # RELATORIO DIARIO
    while True:
        try:
            maybe_run_daily_report()
        except Exception:
            traceback.print_exc()

        time.sleep(30)  # checa a cada 30s

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop,daemon=True).start()
    run_loop()
