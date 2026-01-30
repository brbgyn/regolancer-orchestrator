def log_pair(worker_id, src, tgt, amount, prefix=None):
    amount_fmt = f"{amount:,}".replace(",", ".")

    pfx = prefix or f"[W{worker_id}] [AMOUNT {amount_fmt}]"

    tgt_max_local = 100 - tgt["ar_in_target"]

    print(
        f"{pfx} "
        f"SRC {src['alias']} "
        f"(LOCAL {src['local_pct']}% → min {src['ar_out_target']}%)"
        f"  →  "
        f"TGT {tgt['alias']} "
        f"(LOCAL {tgt['local_pct']}% → max {tgt_max_local}%)"
    )
