"""Extract a focused speedup table: workers, division, protocol, vehicles, speedup.

Covers all FULL-HORIZON distributed runs. Keeps only ``status==aggregated``
rows (drops submit_error/failed noise), backfills 15k@6w speedups whose
same-run baseline failed, and averages duplicate measurements (pseudo-seeds).
"""

from __future__ import annotations
import csv, glob, os
from pathlib import Path
from SIMULATION.distributed.partitioning import parse_partition

ROOT = Path(__file__).resolve().parent / "results"
FULL = {"20260627_235113", "20260628_171534", "20260629_161107",
        "20260630_075315", "20260630_180959",
        "20260701_050719", "20260701_170050"}
FALLBACK_BASE_15K = {"False": 3553.7, "True": 3780.1}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    agg = {}
    for f in glob.glob(str(ROOT / "*" / "benchmark.csv")):
        sw = os.path.basename(os.path.dirname(f))
        if sw not in FULL:
            continue
        recs = list(csv.DictReader(open(f, encoding="utf-8")))
        base = {}
        for x in recs:
            if x["kind"] == "baseline" and x["status"] == "ok":
                base[(x["vehicles"], x["accident"])] = _f(x["runtime_sec"])
        for x in recs:
            if x["kind"] != "distributed" or x["status"] != "aggregated":
                continue
            sp = _f(x["speedup"])
            rt = _f(x["runtime_sec"])
            if sp is None and rt:
                b = base.get((x["vehicles"], x["accident"]))
                if b is None and x["vehicles"] == "15000":
                    b = FALLBACK_BASE_15K.get(x["accident"])
                if b:
                    sp = round(b / rt, 3)
            if sp is None:
                continue
            degraded = (x["self_disabled"] == "True" or x["handoffs_out"] == "0")
            key = (int(x["num_groups"]), x["partition"], x["transport"],
                   int(x["vehicles"]), "si" if x["accident"] == "True" else "no")
            d = agg.setdefault(key, {"vals": [], "deg": False})
            d["vals"].append(sp)
            d["deg"] = d["deg"] or degraded

    rows = []
    for key in sorted(agg):
        d = agg[key]
        mean = sum(d["vals"]) / len(d["vals"])
        spec = parse_partition(key[1])
        rows.append({
            "mapa": "Almenara", "workers": key[0], "tipo": spec.kind,
            "division": key[1],
            "eje": spec.axis if spec.kind == "balanced" else "",
            "protocolo": key[2], "vehiculos": key[3], "accidente": key[4],
            "speedup": f"{mean:.3f}", "n": len(d["vals"]),
            "degradado": "si" if d["deg"] else "no",
        })

    out = ROOT / "speedups_table.csv"
    cols = ["mapa", "workers", "tipo", "division", "eje", "protocolo",
            "vehiculos", "accidente", "speedup", "n", "degradado"]
    with open(out, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"[table] {out}  ({len(rows)} filas)")
    for r in rows:
        print(f"{r['workers']}w  {r['tipo']:9s} {r['division']:13s} {r['protocolo']:5s} "
              f"{r['vehiculos']:>6} {r['accidente']:>3}  {r['speedup']:>7} "
              f"(n={r['n']})" + ("  DEG" if r["degradado"] == "si" else ""))


if __name__ == "__main__":
    main()
