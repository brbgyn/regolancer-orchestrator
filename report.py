import csv
import json
import subprocess
import os
from datetime import datetime, date
import requests
from dotenv import load_dotenv
load_dotenv()

# =========================
# CONFIG
# =========================

BASE_DIR = "/home/admin/regolancer-orchestrator"

SUCCESS_REBAL_CSV = f"{BASE_DIR}/success-rebal.csv"
LND_SUCCESS_REBAL_CSV = f"{BASE_DIR}/lnd-success-rebal.csv"
DAILY_REPORT_CSV = f"{BASE_DIR}/daily-report.csv"

TODAY = date.today()

# Telegram (opcional)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
    else None
)

# =========================
# LND HELPERS
# =========================

def lncli(cmd):
    result = subprocess.run(
        ["lncli"] + cmd,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)

def get_node_pubkey():
    return lncli(["getinfo"])["identity_pubkey"]

# =========================
# REBALANCE DETECTION
# =========================

def is_rebalance(payment, my_pubkey):
    for htlc in payment.get("htlcs", []):
        route = htlc.get("route")
        if not route:
            continue

        hops = route.get("hops", [])
        if hops and hops[-1].get("pub_key") == my_pubkey:
            return True

    return False

# =========================
# BUILD lnd-success-rebal.csv (HOJE)
# =========================

def build_lnd_success_rebal_csv():
    my_pubkey = get_node_pubkey()

    data = lncli(["listpayments", "--include_incomplete=false"])
    payments = data.get("payments", [])

    rows = []

    for p in payments:
        if p.get("status") != "SUCCEEDED":
            continue

        if not is_rebalance(p, my_pubkey):
            continue

        try:
            epoch_ts = int(p["creation_time_ns"]) // 1_000_000_000
            created_date = datetime.fromtimestamp(epoch_ts).date()
        except Exception:
            continue

        if created_date != TODAY:
            continue

        try:
            amount_msat = int(p["value_msat"])
            fee_msat = int(p.get("fee_msat", 0))
        except Exception:
            continue

        htlc = p["htlcs"][0]
        hops = htlc["route"]["hops"]

        chan_out = hops[0]["chan_id"]
        chan_in = hops[-1]["chan_id"]

        rows.append([
            epoch_ts,
            chan_out,
            chan_in,
            amount_msat,
            fee_msat,
        ])

    with open(LND_SUCCESS_REBAL_CSV, "w", newline="") as f:
        csv.writer(f).writerows(rows)

# =========================
# CSV AGGREGATION (HOJE)
# =========================

def aggregate_csv_today(path):
    """
    Returns:
      total_amount_msat, total_fee_msat
    """
    if not os.path.exists(path):
        return 0, 0

    total_amount = 0
    total_fee = 0

    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 5:
                continue
            try:
                epoch_ts = int(row[0])
                created_date = datetime.fromtimestamp(epoch_ts).date()
                if created_date != TODAY:
                    continue

                amount_msat = int(row[3])
                fee_msat = int(row[4])
            except Exception:
                continue

            total_amount += amount_msat
            total_fee += fee_msat

    return total_amount, total_fee

# =========================
# DAILY REPORT CSV
# =========================

def write_daily_report(
    node_amount_msat,
    orchestrator_amount_msat,
    node_fee_msat,
    orchestrator_fee_msat,
):
    pct = (
        orchestrator_amount_msat / node_amount_msat * 100
        if node_amount_msat > 0
        else 0.0
    )

    node_ppm = (
        node_fee_msat / node_amount_msat * 1_000_000
        if node_amount_msat > 0
        else 0.0
    )

    orchestrator_ppm = (
        orchestrator_fee_msat / orchestrator_amount_msat * 1_000_000
        if orchestrator_amount_msat > 0
        else 0.0
    )

    rows = []

    if os.path.exists(DAILY_REPORT_CSV):
        with open(DAILY_REPORT_CSV) as f:
            reader = csv.reader(f)
            next(reader, None)
            rows = list(reader)

    rows = [r for r in rows if r and r[0] != TODAY.isoformat()]

    rows.append([
        TODAY.isoformat(),
        node_amount_msat // 1000,
        orchestrator_amount_msat // 1000,
        round(node_ppm, 1),
        round(orchestrator_ppm, 1),
        round(pct, 2),
    ])

    with open(DAILY_REPORT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date",
            "total_node_sats",
            "orchestrator_sats",
            "node_fee_ppm",
            "orchestrator_fee_ppm",
            "orchestrator_pct",
        ])
        writer.writerows(rows)

    return node_ppm, orchestrator_ppm, pct

# =========================
# TELEGRAM MESSAGE
# =========================

def build_telegram_summary(
    node_sats,
    orchestrator_sats,
    node_ppm,
    orchestrator_ppm,
    pct,
):
    return (
        "📊 *regolancer-orchestrator*\n\n"
        f"⚡ Node Total Rebals: `{node_sats:,}`\n"
        f"☯️ Regolancer-Orchestrator: `{orchestrator_sats:,}`\n"
        f"☯️ Outros: `{node_sats - orchestrator_sats:,}`\n\n"
        #"💸 Fee média node: `{node_ppm:.1f} ppm`\n"
        #f"💸 Fee média regolancer: `{orchestrator_ppm:.1f} ppm`\n\n"
        f"📈 Rebal Share Hoje: `{pct:.2f}%`"
    )

def send_telegram(msg):
    if not TELEGRAM_API_URL:
        return

    requests.post(
        TELEGRAM_API_URL,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=5,
    )

# =========================
# MAIN
# =========================

def main(send_telegram_msg=True):
    # 1️⃣ build source of truth
    build_lnd_success_rebal_csv()

    # 2️⃣ aggregate
    node_amount_msat, node_fee_msat = aggregate_csv_today(LND_SUCCESS_REBAL_CSV)
    orchestrator_amount_msat, orchestrator_fee_msat = aggregate_csv_today(SUCCESS_REBAL_CSV)

    # 3️⃣ write report
    node_ppm, orchestrator_ppm, pct = write_daily_report(
        node_amount_msat,
        orchestrator_amount_msat,
        node_fee_msat,
        orchestrator_fee_msat,
    )

    # 4️⃣ telegram summary
    msg = build_telegram_summary(
        node_sats=node_amount_msat // 1000,
        orchestrator_sats=orchestrator_amount_msat // 1000,
        node_ppm=node_ppm,
        orchestrator_ppm=orchestrator_ppm,
        pct=pct,
    )

    if send_telegram_msg:
        send_telegram(msg)

    print(msg)

if __name__ == "__main__":
    main()
