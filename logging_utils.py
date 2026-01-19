def log_pair(worker_id, src, tgt, amount):
    amount_fmt = f"{amount:,}".replace(",", ".")

    tgt_max_local = 100 - tgt["ar_in_target"]

    print(
        f"[W{worker_id}] "
        f"[AMOUNT {amount_fmt}] "
        f"SRC {src['alias']} "
        f"(LOCAL {src['local_pct']}% → min {src['ar_out_target']}%)"
        f"  →  "
        f"TGT {tgt['alias']} "
        f"(LOCAL {tgt['local_pct']}% → max {tgt_max_local}%)"
    )
