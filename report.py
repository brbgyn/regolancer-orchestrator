import csv
import os
import requests
import time
from datetime import datetime, date, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = None
load_dotenv()

# =========================
# CONFIG
# =========================

BASE_DIR = "/home/admin/regolancer-orchestrator"

DAILY_REPORT_CSV = f"{BASE_DIR}/daily-report.csv"
SUCCESS_REBAL_CSV = f"{BASE_DIR}/success-rebal.csv"

DAYS_BACK = 365
TODAY = date.today()
START_DATE = TODAY - timedelta(days=DAYS_BACK)

# LNDg
LNDG_BASE_URL = os.getenv("LNDG_BASE_URL", "http://localhost:8889").rstrip("/")
LNDG_USER = os.getenv("LNDG_USER")
LNDG_PASS = os.getenv("LNDG_PASS")

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

# =========================
# HTTP
# =========================

def get_lndg_session():
    global _session
    if _session is None:
        s = requests.Session()

        retries = Retry(
            total=5, backoff_factor=1.5,
            status_forcelist=[500, 502, 503,
            504], allowed_methods=["GET"],
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
                    "lndg": int(r["lndg_sats"]),
                    "rego": int(r["regolancer_sats"]),
                }
            except Exception:
                continue

    log(f"Loaded {len(daily)} days from daily-report.csv")
    return daily

# =========================
# LNDg REBALANCES (BACKFILL)
# =========================

def fetch_lndg_rebalances(skip_days):
    daily = defaultdict(int)

    url = f"{LNDG_BASE_URL}/api/rebalancer/?limit=100"
    page = 0

    log("Starting LNDg fetch")

    while url:
        page += 1
        data = lndg_get(url)

        oldest_date = None
        processed = 0
        skipped_existing = 0

        for rb in data.get("results", []):
            if rb.get("status") != 2:
                continue

            try:
                completed = rb.get("stop") or rb.get("requested")
                d = datetime.fromisoformat(
                    completed.replace("Z", "+00:00")
                ).date()
            except Exception:
                continue

            oldest_date = d if oldest_date is None else min(oldest_date, d)

            # fora da janela → para tudo
            if d < START_DATE:
                log("Reached records older than DAYS_BACK → stopping")
                return daily

            if d > TODAY:
                continue

            processed += 1

            # já está no CSV → skip
            if d in skip_days:
                skipped_existing += 1
                continue

            try:
                daily[d] += int(rb.get("value") or 0)
            except Exception:
                continue

        log(
            f"Fetching page {page}"
            + (f" (oldest date: {oldest_date})" if oldest_date else "")
            + f" | processed={processed}, skipped_existing={skipped_existing}"
        )
        time.sleep(0.3)

        # 🔴 REGRA NOVA E CRÍTICA
        if processed > 0 and processed == skipped_existing:
            log("All records in this page already present in daily-report.csv → stopping early")
            break

        url = data.get("next")

    return daily

# =========================
# REGOLANCER CSV
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

                # msat → sat
                daily[d] += int(row[3]) // 1000
            except Exception:
                continue

    log(f"Loaded Regolancer rebalances for {len(daily)} days")
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
        ])

        d = START_DATE
        while d <= TODAY:
            writer.writerow([
                d.isoformat(),
                daily.get(d, {}).get("lndg", 0),
                daily.get(d, {}).get("rego", 0),
            ])
            d += timedelta(days=1)

    log("daily-report.csv written successfully")

# =========================
# TELEGRAM MESSAGE
# =========================

def build_telegram_message(daily):
    today = daily.get(TODAY, {})
    lndg_today = today.get("lndg", 0)
    rego_today = today.get("rego", 0)
    total_today = lndg_today + rego_today

    month_start = TODAY.replace(day=1)
    lndg_month = sum(v.get("lndg", 0) for d, v in daily.items() if d >= month_start)
    rego_month = sum(v.get("rego", 0) for d, v in daily.items() if d >= month_start)
    total_month = lndg_month + rego_month

    msg = (
        "📊 *regolancer-orchestrator*\n\n"
        f"⚡ Total Rebals Hoje: {total_today:,}\n"
        f"☯️ LNDg: {lndg_today:,} ({pct(lndg_today, total_today):.2f}%)\n"
        f"☯️ Regolancer-Orchestrator: {rego_today:,} ({pct(rego_today, total_today):.2f}%)\n\n"
        "📊 *Mês Atual*\n\n"
        f"⚡ Total Rebals: {total_month:,}\n"
        f"☯️ LNDg: {lndg_month:,} ({pct(lndg_month, total_month):.2f}%)\n"
        f"☯️ Regolancer-Orchestrator: {rego_month:,} ({pct(rego_month, total_month):.2f}%)\n\n"
        "📊 *Histórico 12m*\n\n"
    )

    for i in range(11, -1, -1):
        m = (TODAY.replace(day=1) - timedelta(days=30*i))
        lndg_m = sum(v.get("lndg", 0) for d, v in daily.items() if d.year == m.year and d.month == m.month)
        rego_m = sum(v.get("rego", 0) for d, v in daily.items() if d.year == m.year and d.month == m.month)
        total_m = lndg_m + rego_m

        msg += (
            f"☯️ {m.strftime('%b')}: Total {total_m:,} "
            f"(LNDg {pct(lndg_m, total_m):.2f}% / "
            f"Rego {pct(rego_m, total_m):.2f}%)\n"
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
    log("=== REPORT START ===")

    daily = load_existing_report()
    skip_days = set(daily.keys())

    rego = load_regolancer_rebalances()
    lndg = fetch_lndg_rebalances(skip_days)

    for d, amt in lndg.items():
        daily.setdefault(d, {})["lndg"] = amt

    for d, amt in rego.items():
        daily.setdefault(d, {})["rego"] = amt

    save_daily_report(daily)

    msg = build_telegram_message(daily)
    print("\n" + msg + "\n")
    send_telegram(msg)

    log("=== REPORT END ===")

if __name__ == "__main__":
    main()
