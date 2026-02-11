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
import random
sys.stdout.reconfigure(line_buffering=True)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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

# ------------------------------------------------------------
# GENERAL EXECUTION
# ------------------------------------------------------------
LOG_OPERATIONAL      = env_bool("LOG_OPERATIONAL", False)
RUN_FOREVER          = env_bool("RUN_FOREVER", True)
SLEEP_SECONDS        = int(os.getenv("SLEEP_SECONDS", "5"))
MAX_WORKERS          = int(os.getenv("MAX_WORKERS", "1"))
MAX_CYCLE_SECONDS    = int(os.getenv("MAX_CYCLE_SECONDS", "300"))
RANDOMIZE_PAIRS      = env_bool("RANDOMIZE_PAIRS", True)
ENABLE_FILE_LOGS     = env_bool("ENABLE_FILE_LOGS", False)

DRY_RUN              = env_bool("DRY_RUN", True)
REGOLANCER_LIVE_LOGS = env_bool("REGOLANCER_LIVE_LOGS", True)
LOG_TEMPLATE_CONFIG  = env_bool("LOG_TEMPLATE_CONFIG", False)

SYNC_LOS_TO_LNDG     = env_bool("SYNC_LOS_TO_LNDG", False)


# ------------------------------------------------------------
# TELEGRAM — REBALANCE NOTIFICATIONS
# ------------------------------------------------------------
SEND_REBALANCE_MSG_REGO_ORCH = env_bool("SEND_REBALANCE_MSG_REGO_ORCH", True)
SEND_REBALANCE_MSG_LNDG      = env_bool("SEND_REBALANCE_MSG_LNDG", True)
SEND_REBALANCE_MSG_LOS       = env_bool("SEND_REBALANCE_MSG_LOS", True)


# ------------------------------------------------------------
# LNDg CONFIG
# ------------------------------------------------------------
LNDG_BASE_URL = os.getenv("LNDG_BASE_URL", "http://localhost:8889").rstrip("/")
LNDG_USER     = os.getenv("LNDG_USER")
LNDG_PASS     = os.getenv("LNDG_PASS")


# ------------------------------------------------------------
# LOS CONFIG
# ------------------------------------------------------------
LOS_BASE_URL  = os.getenv("LOS_BASE_URL", "https://localhost:8443").rstrip("/")
LOS_VERIFY_TLS = os.getenv("LOS_VERIFY_TLS", "false").lower() == "true"


# ------------------------------------------------------------
# TELEGRAM STATE DIRECTORY
# ------------------------------------------------------------
TELEGRAM_STATE_DIR = "/home/admin/regolancer-orchestrator/telegram"
os.makedirs(TELEGRAM_STATE_DIR, exist_ok=True)

REGO_STATE_FILE = os.path.join(TELEGRAM_STATE_DIR, "last_rego_offset.txt")
LNDG_STATE_FILE = os.path.join(TELEGRAM_STATE_DIR, "last_lndg_id.txt")
LOS_STATE_FILE  = os.path.join(TELEGRAM_STATE_DIR, "last_los_id.txt")


# ------------------------------------------------------------
# SYSTEM PATHS
# ------------------------------------------------------------
REGOLANCER_BIN   = "/home/admin/regolancer-orchestrator/regolancer"
TEMPLATE_FILE    = "/home/admin/regolancer-orchestrator/config.template.json"
REPORT_PY        = "/home/admin/regolancer-orchestrator/report.py"

AMOUNT_STATE_FILE          = "/home/admin/regolancer-orchestrator/amount_state.json"
LAST_REPORT_FILE           = "/home/admin/regolancer-orchestrator/last_report_date.txt"
LAST_REPORT_ERROR_FILE     = "/home/admin/regolancer-orchestrator/last_report_error_date.txt"

SUCCESS_REBAL_FILE         = "/home/admin/regolancer-orchestrator/success-rebal.csv"
ERROR_LOG_FILE             = "/home/admin/regolancer-orchestrator/errors.log"

SYNC_SCRIPT_PATH           = "/home/admin/regolancer-orchestrator/sync_los_to_lndg.sh"

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

    if LOG_OPERATIONAL:
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
        cycle_start = time.monotonic()

        try:
            amount, state = advance_cycle_and_get_amount()

            channels = asyncio.run(load_channels())
            pairs = build_pairs(channels)

            if pairs and RANDOMIZE_PAIRS:
                random.shuffle(pairs)

            pair_counter = 0  # 🔁 RESET A CADA CICLO

            if pairs:
                for pair in pairs:
                    elapsed = time.monotonic() - cycle_start

                    # ⏱️ TIMEOUT DO CICLO
                    if elapsed > MAX_CYCLE_SECONDS:
                        src_alias = pair.get("source", {}).get("alias", "?")
                        tgt_alias = pair.get("target", {}).get("alias", "?")

                        msg = (
                            f"[W{worker_id}] CYCLE TIMEOUT: "
                            f"elapsed={int(elapsed)}s max={MAX_CYCLE_SECONDS}s "
                            f"(cycle={state['cycle']}, pair=SRC {src_alias} → TGT {tgt_alias})"
                        )

                        print(f"[W{worker_id}] ⚠️ {msg}, restarting cycle")
                        #log_error(msg)
                        break

                    pair_counter += 1
                    run_regolancer(worker_id, pair, amount, pair_counter)

            total = int(time.monotonic() - cycle_start)

            print(
                f"[W{worker_id}] === CYCLE FINISHED "
                f"(cycle={state['cycle']} amount={amount} duration={total}s) "
                f"— sleeping {SLEEP_SECONDS}s ==="
            )
        except Exception:
            err = traceback.format_exc()
            print(f"[W{worker_id}] ERROR")
            print(err)
            log_error(f"[W{worker_id}] Unhandled exception:\n{err}")

        time.sleep(SLEEP_SECONDS)

# =========================
# SUCCESS REBAL READER
# =========================

def read_new_rebalances():
    # FIRST RUN → não enviar histórico
    if not os.path.exists(REGO_STATE_FILE):
        if os.path.exists(SUCCESS_REBAL_FILE):
            with open(SUCCESS_REBAL_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                write_last_id(REGO_STATE_FILE, f.tell())
        return []

    last_offset = read_last_id(REGO_STATE_FILE)

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

    write_last_id(REGO_STATE_FILE, new_offset)

    return new_lines

# =========================
# MESSAGE
# =========================

def telegram_notifier_loop():
    print("[TELEGRAM] notifier started")

    while True:
        try:
            # ---------------------------
            # Regolancer-Orchestrator
            # ---------------------------
            if SEND_REBALANCE_MSG_REGO_ORCH:
                new_rebalances = read_new_rebalances()

                for line in new_rebalances:
                    try:
                        parts = line.split(",")
                        amount_msat = int(parts[3])
                        amount_sat = amount_msat // 1000
                        msg = format_rebalance_source_msg(
                            amount_sat,
                            "Regolancer-Orchestrator"
                        )
                        send_telegram(msg)
                    except Exception:
                        continue

            # ---------------------------
            # LNDg
            # ---------------------------
            lndg_events = read_new_lndg_rebalances()

            for rb_id, amount in lndg_events:
                msg = format_rebalance_source_msg(amount, "LNDg")
                send_telegram(msg)

            # ---------------------------
            # LOS
            # ---------------------------
            los_events = read_new_los_rebalances()

            for at_id, amount in los_events:
                msg = format_rebalance_source_msg(amount, "LOS")
                send_telegram(msg)

        except Exception:
            err = traceback.format_exc()
            print("[TELEGRAM] ERROR")
            print(err)
            log_error(f"[TELEGRAM] Unhandled exception:\n{err}")

        time.sleep(30)

def format_rebalance_source_msg(amount_sat: int, source: str) -> str:
    return f"☯️ ⚡ {amount_sat:,} by {source}"

def read_last_id(path):
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def write_last_id(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
    except Exception:
        pass

def read_new_lndg_rebalances():
    if not SEND_REBALANCE_MSG_LNDG:
        return []

    try:
        r = requests.get(
            f"{LNDG_BASE_URL}/api/rebalancer/?status=2&limit=50",
            auth=(LNDG_USER, LNDG_PASS),
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    results = data.get("results", [])

    # FIRST RUN → salvar maior ID e sair
    if not os.path.exists(LNDG_STATE_FILE):
        if results:
            max_id = max(rb.get("id", 0) for rb in results)
            write_last_id(LNDG_STATE_FILE, max_id)
        return []

    last_id = read_last_id(LNDG_STATE_FILE)

    new_events = []
    max_id_seen = last_id

    for rb in results:
        rb_id = rb.get("id", 0)

        if rb_id > last_id:
            amount = int(rb.get("value", 0))
            new_events.append((rb_id, amount))

        if rb_id > max_id_seen:
            max_id_seen = rb_id

    if max_id_seen > last_id:
        write_last_id(LNDG_STATE_FILE, max_id_seen)

    return new_events

def read_new_los_rebalances():
    if not SEND_REBALANCE_MSG_LOS:
        return []

    try:
        r = requests.get(
            f"{LOS_BASE_URL}/api/rebalance/history",
            verify=LOS_VERIFY_TLS,
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    attempts = [
        at for at in data.get("attempts", [])
        if at.get("status") == "succeeded"
    ]

    # FIRST RUN → salvar maior ID e sair
    if not os.path.exists(LOS_STATE_FILE):
        if attempts:
            max_id = max(at.get("id", 0) for at in attempts)
            write_last_id(LOS_STATE_FILE, max_id)
        return []

    last_id = read_last_id(LOS_STATE_FILE)

    new_events = []
    max_id_seen = last_id

    for at in attempts:
        at_id = at.get("id", 0)

        if at_id > last_id:
            amount = int(at.get("amount_sat", 0))
            new_events.append((at_id, amount))

        if at_id > max_id_seen:
            max_id_seen = at_id

    if max_id_seen > last_id:
        write_last_id(LOS_STATE_FILE, max_id_seen)

    return new_events

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
            log_error(msg)
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

_error_log_lock = threading.Lock()

def log_error(msg: str):
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = f"[{timestamp}] {msg}\n"

    try:
        with _error_log_lock:
            with open(ERROR_LOG_FILE, "a") as f:
                f.write(line)
    except Exception:
        # último recurso: não deixa crashar por erro de log
        pass

def los_sync_loop():
    if not SYNC_LOS_TO_LNDG:
        print("[LOS-SYNC] Disabled")
        return

    print("[LOS-SYNC] Sync loop started (interval=60s)")

    while True:
        try:
            #print("[LOS-SYNC] Running sync_los_to_lndg.sh")

            subprocess.run(
                [SYNC_SCRIPT_PATH],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

        except Exception as e:
            err = f"[LOS-SYNC] ERROR: {e}"
            print(err)
            log_error(err)

        time.sleep(60)

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

    # LOS → LNDg sync loop
    threading.Thread(
        target=los_sync_loop,
        daemon=True
    ).start()


    # keep main alive
    while True:
        time.sleep(3600)
