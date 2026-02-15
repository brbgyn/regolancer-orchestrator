#!/usr/bin/env python3

import csv
import os
import requests
import time
import fcntl
import sys
from datetime import datetime, date, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

_session = None
load_dotenv()

# =========================
# CONFIG
# =========================

BASE_DIR = "/home/admin/regolancer-orchestrator"
LOCK_FILE = "/tmp/regolancer-report.lock"
DAILY_REPORT_CSV = f"{BASE_DIR}/daily-report.csv"
SUCCESS_REBAL_CSV = f"{BASE_DIR}/success-rebal.csv"

DAYS_BACK = 365
TZ = ZoneInfo("America/Sao_Paulo")
TODAY = datetime.now(TZ).date()
START_DATE = TODAY - timedelta(days=DAYS_BACK)

# LNDg
LNDG_BASE_URL = os.getenv("LNDG_BASE_URL", "http://localhost:8889").rstrip("/")
LNDG_USER = os.getenv("LNDG_USER")
LNDG_PASS = os.getenv("LNDG_PASS")

# LOS (LightningOS)
LOS_BASE_URL = os.getenv("LOS_BASE_URL", "https://localhost:8443").rstrip("/")

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
    else None
)

# =========================
# UTILS
# =========================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def pct(part, total):
    return (part / total * 100) if total > 0 else 0.0

def acquire_lock():
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except BlockingIOError:
        log("Another report.py instance is already running → exiting")
        sys.exit(0)

# =========================
# HTTP
# =========================

def get_lndg_session():
    global _session
    if _session is None:
        s = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
    return _session

def lndg_get(url):
    session = get_lndg_session()
    r = session.get(
        url,
        auth=(LNDG_USER, LNDG_PASS),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

# =========================
# LOAD EXISTING REPORT
# =========================

def load_existing_report():
    daily = {}

    if not os.path.exists(DAILY_REPORT_CSV):
        return daily

    with open(DAILY_REPORT_CSV) as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                d = datetime.fromisoformat(r["date"]).date()
                daily[d] = {
                    "lndg": int(r.get("lndg_sats", 0)),
                    "rego": int(r.get("regolancer_sats", 0)),
                    "fw_sats": int(r.get("forwards_sats", 0)),
                }
            except Exception:
                continue

    log(f"Loaded {len(daily)} days from daily-report.csv")
    return daily

# =========================
# LNDg REBALANCES
# =========================

def fetch_lndg_rebalances(skip_days):
    daily = defaultdict(int)

    url = f"{LNDG_BASE_URL}/api/rebalancer/?status=2&limit=100"
    page = 0

    log("Starting LNDg rebalances fetch")

    while url:
        page += 1
        data = lndg_get(url)

        processed = 0
        skipped_existing = 0
        oldest_date = None

        for rb in data.get("results", []):
            try:
                completed = rb.get("stop") or rb.get("requested")
                if not completed:
                    continue

                # 🔥 Parse robusto de timestamp
                dt = datetime.fromisoformat(
                    completed.replace("Z", "+00:00")
                )

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))

                dt_br = dt.astimezone(TZ)
                d = dt_br.date()

            except Exception:
                continue

            oldest_date = d if oldest_date is None else min(oldest_date, d)

            if d < START_DATE:
                log("Reached rebals older than DAYS_BACK → stopping")
                return daily

            if d > TODAY:
                continue

            processed += 1

            if d in skip_days:
                skipped_existing += 1
                continue

            daily[d] += int(rb.get("value") or 0)

        log(
            f"Rebals page {page}"
            + (f" (oldest date: {oldest_date})" if oldest_date else "")
            + f" | processed={processed}, skipped_existing={skipped_existing}"
        )

        time.sleep(0.3)

        if processed > 0 and processed == skipped_existing:
            log("All rebals in this page already processed → stopping early")
            break

        url = data.get("next")

    return daily

# =========================
# REGOLANCER REBALANCES
# =========================

def load_regolancer_rebalances():
    daily = defaultdict(int)

    if not os.path.exists(SUCCESS_REBAL_CSV):
        log("success-rebal.csv not found → Regolancer totals = 0")
        return daily

    log("Loading Regolancer-Orchestrator rebalances")

    with open(SUCCESS_REBAL_CSV) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 4:
                continue
            try:
                d = datetime.fromtimestamp(int(row[0])).date()
                if d < START_DATE or d > TODAY:
                    continue
                daily[d] += int(row[3]) // 1000
            except Exception:
                continue

    log(f"Loaded Regolancer rebalances for {len(daily)} days")
    return daily

# =========================
# LOS REBALANCES
# =========================

def fetch_los_rebalances():
    daily = defaultdict(int)

    url = f"{LOS_BASE_URL}/api/rebalance/history"

    log("Starting LOS rebalances fetch")

    try:
        r = requests.get(url, verify=False, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"LOS fetch error: {e}")
        return daily

    month_start = TODAY.replace(day=1)

    processed = 0

    for at in data.get("attempts", []):
        try:
            if at.get("status") != "succeeded":
                continue

            d = datetime.fromisoformat(
                at["finished_at"].replace("Z", "+00:00")
            ).astimezone(TZ).date()

            if d < month_start or d > TODAY:
                continue

            processed += 1
            daily[d] += int(at.get("amount_sat", 0))

        except Exception:
            continue

    log(f"LOS processed (month rebuild) = {processed}")

    return daily

# =========================
# LNDg FORWARDS (VOLUME)
# =========================

def fetch_lndg_forwards(skip_days):
    daily = defaultdict(int)

    url = f"{LNDG_BASE_URL}/api/forwards/?limit=100"
    page = 0

    log("Starting LNDg forwards fetch")

    while url:
        page += 1
        data = lndg_get(url)

        processed = 0
        skipped_existing = 0
        oldest_date = None

        for fw in data.get("results", []):
            try:
                raw_ts = fw.get("forward_date")
                if not raw_ts:
                    continue

                # 🔥 Parse robusto de timestamp
                dt = datetime.fromisoformat(
                    raw_ts.replace("Z", "+00:00")
                )

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))

                dt_br = dt.astimezone(TZ)
                d = dt_br.date()

            except Exception:
                continue

            oldest_date = d if oldest_date is None else min(oldest_date, d)

            if d < START_DATE:
                log("Reached forwards older than DAYS_BACK → stopping")
                return daily

            if d > TODAY:
                continue

            processed += 1

            if d in skip_days:
                skipped_existing += 1
                continue

            daily[d] += int(fw.get("amt_out_msat", 0)) // 1000

        log(
            f"Forwards page {page}"
            + (f" (oldest date: {oldest_date})" if oldest_date else "")
            + f" | processed={processed}, skipped_existing={skipped_existing}"
        )

        time.sleep(0.3)

        if processed > 0 and processed == skipped_existing:
            log("All forwards in this page already processed → stopping early")
            break

        url = data.get("next")

    return daily

# =========================
# SAVE REPORT
# =========================

def save_daily_report(daily):
    log("Writing daily-report.csv")

    with open(DAILY_REPORT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date",
            "lndg_sats",
            "regolancer_sats",
            "los_sats",
            "forwards_sats",
        ])

        d = START_DATE
        while d <= TODAY:
            writer.writerow([
                d.isoformat(),
                daily.get(d, {}).get("lndg", 0),
                daily.get(d, {}).get("rego", 0),
                daily.get(d, {}).get("los", 0),
                daily.get(d, {}).get("fw_sats", 0),
            ])

            d += timedelta(days=1)

    log("daily-report.csv written successfully")

# =========================
# TELEGRAM MESSAGE
# =========================

def build_telegram_message(daily):
    msg = "📊 *regolancer-orchestrator*\n\n"

    # =========================
    # HOJE
    # =========================

    today = daily.get(TODAY, {})
    fw_today = today.get("fw_sats", 0)
    lndg_today = today.get("lndg", 0)
    rego_today = today.get("rego", 0)
    los_today = today.get("los", 0)

    rebals_today = lndg_today + rego_today + los_today

    msg += (
        f"🗓️ *{TODAY.strftime('%d/%m/%Y')}*:\n"
        f"💰 Forwards Hoje: {fw_today:,}\n"
        f"☯️ Rebals Hoje: {rebals_today:,}\n"
        f"☯️ LOS: {los_today:,} ({pct(los_today, rebals_today):.2f}%)\n"
        f"☯️ LNDg: {lndg_today:,} ({pct(lndg_today, rebals_today):.2f}%)\n"
        f"☯️ Rego-Orchestrator: {rego_today:,} ({pct(rego_today, rebals_today):.2f}%)\n\n"
    )

    # =========================
    # MÊS ATUAL
    # =========================

    month_start = TODAY.replace(day=1)
    month_name = TODAY.strftime("%B").capitalize()

    fw_month = sum(v.get("fw_sats", 0) for d, v in daily.items() if d >= month_start)
    lndg_month = sum(v.get("lndg", 0) for d, v in daily.items() if d >= month_start)
    rego_month = sum(v.get("rego", 0) for d, v in daily.items() if d >= month_start)
    los_month = sum(v.get("los", 0) for d, v in daily.items() if d >= month_start)

    rebals_month = lndg_month + rego_month + los_month

    msg += (
        f"📊 *Mês Atual - {month_name}*\n\n"
        f"💰 Total Forwards: {fw_month:,}\n"
        f"☯️ Total Rebals: {rebals_month:,}\n"
        f"☯️ LOS: {los_month:,} ({pct(los_month, rebals_month):.2f}%)\n"
        f"☯️ LNDg: {lndg_month:,} ({pct(lndg_month, rebals_month):.2f}%)\n"
        f"☯️ Rego-Orchestrator: {rego_month:,} ({pct(rego_month, rebals_month):.2f}%)\n\n"
    )

    # =========================
    # HISTÓRICO 12M
    # =========================

    msg += "📊 *Histórico 12m*\n\n"

    for i in range(11, -1, -1):
        m = (TODAY.replace(day=1) - timedelta(days=30 * i))
        year, month = m.year, m.month

        fw_m = sum(v.get("fw_sats", 0) for d, v in daily.items() if d.year == year and d.month == month)
        lndg_m = sum(v.get("lndg", 0) for d, v in daily.items() if d.year == year and d.month == month)
        rego_m = sum(v.get("rego", 0) for d, v in daily.items() if d.year == year and d.month == month)
        los_m = sum(v.get("los", 0) for d, v in daily.items() if d.year == year and d.month == month)

        rebals_m = lndg_m + rego_m + los_m

        msg += (
            f"🗓️ *{m.strftime('%B')}*:\n"
            f"💰 Forwards {fw_m:,}\n"
            f"☯️ Rebals Total {rebals_m:,}\n"
            f"☯️ LOS {pct(los_m, rebals_m):.2f}%\n"
            f"☯️ LNDg {pct(lndg_m, rebals_m):.2f}%\n"
            f"☯️ Rego-Orch {pct(rego_m, rebals_m):.2f}%\n\n"
        )

    return msg

def send_telegram(msg):
    if not TELEGRAM_API_URL:
        log("Telegram not configured → skipping send")
        return

    requests.post(
        TELEGRAM_API_URL,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
        },
        timeout=5,
    )
    log("Telegram message sent")

# =========================
# MAIN
# =========================

def main():
    lock_fd = acquire_lock()

    log("=== REPORT START ===")

    # 🔍 TIME DEBUG
    log("=== TIME DEBUG ===")
    log(f"datetime.now() (naive system): {datetime.now()}")
    log(f"datetime.now(TZ): {datetime.now(TZ)}")
    log(f"TODAY (BR): {TODAY}")

    START_OF_DAY = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    NOW_BR = datetime.now(TZ)

    log(f"START_OF_DAY (BR): {START_OF_DAY}")
    log(f"NOW_BR: {NOW_BR}")
    log("===================")

    daily = load_existing_report()

    # 🔥 Força reset do dia atual
    daily[TODAY] = {}

    skip_days = {d for d in daily.keys() if d != TODAY}

    rego = load_regolancer_rebalances()
    lndg = fetch_lndg_rebalances(skip_days)
    fw = fetch_lndg_forwards(skip_days)

    los = fetch_los_rebalances()

    for d, amt in los.items():
        daily.setdefault(d, {})["los"] = amt

    for d, amt in lndg.items():
        daily.setdefault(d, {})["lndg"] = amt

    for d, amt in rego.items():
        daily.setdefault(d, {})["rego"] = amt

    for d, amt in fw.items():
        daily.setdefault(d, {})["fw_sats"] = amt

    save_daily_report(daily)

    msg = build_telegram_message(daily)
    print("\n" + msg + "\n")
    send_telegram(msg)

    log("=== REPORT END ===")

if __name__ == "__main__":
    main()
