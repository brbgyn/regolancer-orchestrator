import csv
import os
import requests
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =========================
# CONFIG
# =========================
SUCCESS_REBAL_CSV = "/home/admin/regolancer-orchestrator/success-rebal.csv"
DAILY_REPORT_CSV = "/home/admin/regolancer-orchestrator/daily-report.csv"

LNDG_BASE_URL = os.getenv("LNDG_BASE_URL")
LNDG_USER = os.getenv("LNDG_USER")
LNDG_PASS = os.getenv("LNDG_PASS")

TODAY = datetime.now(timezone.utc).date().isoformat()

# =========================
# ORCHESTRATOR COUNT
# =========================
def count_orchestrator_rebalances():
    if not os.path.exists(SUCCESS_REBAL_CSV):
        return 0

    count = 0
    with open(SUCCESS_REBAL_CSV) as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            ts = row[0]
            if ts.startswith(TODAY):
                count += 1
    return count

# =========================
# NODE TOTAL (LNDg)
# =========================
def count_node_rebalances():
    url = f"{LNDG_BASE_URL}/api/rebalancer/?status=success"
    auth = (LNDG_USER, LNDG_PASS)
    total = 0

    while url:
        r = requests.get(url, auth=auth, timeout=10)
        r.raise_for_status()
        data = r.json()

        for rb in data.get("results", []):
            ts = rb.get("created_at") or ""
            if ts.startswith(TODAY):
                total += 1

        url = data.get("next")

    return total
    
# =========================
# REPORT
# =========================
def report_already_written():
    if not os.path.exists(DAILY_REPORT_CSV):
        return False
    with open(DAILY_REPORT_CSV) as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0] == TODAY:
                return True
    return False

def write_daily_report(total, orchestrator):
    pct = round((orchestrator / total) * 100, 2) if total > 0 else 0.0

    new_file = not os.path.exists(DAILY_REPORT_CSV)
    with open(DAILY_REPORT_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow([
                "date",
                "total_node_rebalances",
                "orchestrator_rebalances",
                "orchestrator_pct"
            ])
        writer.writerow([
            TODAY,
            total,
            orchestrator,
            pct
        ])

# =========================
# MAIN
# =========================
def main():
    if report_already_written():
        return

    orchestrator = count_orchestrator_rebalances()
    total = count_node_rebalances()
    write_daily_report(total, orchestrator)

if __name__ == "__main__":
    main()