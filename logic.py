def valid_source(c):
    return (
        not c["ar"] and
        c["local_pct"] > c["ar_out_target"]
    )


def valid_target(c):
    return (
        c["ar"] and
        c["local_pct"] < (100 - c["ar_in_target"])
    )


def compute_pfrom(c):
    return 100 - c["ar_out_target"]


def compute_pto(c):
    # pto é outbound %, e outbound % = local %
    return 100 - c["ar_in_target"]

def build_pairs(channels):
    sources = [c for c in channels if valid_source(c)]
    targets = [c for c in channels if valid_target(c)]

    pairs = []

    for s in sources:
        for t in targets:
            if s["chan_id"] == t["chan_id"]:
                continue

            pairs.append({
                "source": s,
                "target": t,
                "pfrom": compute_pfrom(s),
                "pto": compute_pto(t),
            })

    return pairs
