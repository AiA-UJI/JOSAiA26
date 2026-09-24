"""Build a master INDEX of every distributed simulation run.

Scans every ``results/<sweep>/benchmark.csv`` and emits a single locator CSV
so any run (Almenara or Rotterdam, baseline or distributed) can be found:

  * where the METRICS live      -> benchmark_csv (local)
  * where the RAW outputs live  -> agg_remote (on the master host) for
                                   distributed runs, or the baseline output
                                   dir pattern on the executing lab host.

Usage:
    python -m SIMULATION.distributed.lan.build_index
    python -m SIMULATION.distributed.lan.build_index --out some/path.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_ROOT = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "results"
MASTER_HOST = "labrob07.act.uji.es"

ROAD_TO_MAPA = {
    "rotterdam_arterial": "Rotterdam",
    "modified_4ways_all": "Almenara",
    "": "Almenara",              # legacy CSVs predate the road column
}

OUT_FIELDS = [
    "mapa", "road", "sweep_id", "kind", "label",
    "partition", "num_groups", "transport", "vehicles", "accident",
    "status", "speedup", "wall_sec", "runtime_sec",
    "handoffs_out", "handoffs_in", "barrier_total_sec", "barrier_timeouts",
    "sim_id", "master_host", "agg_remote", "agg_local",
    "sweep_dir", "benchmark_csv",
]


def _sim_id(agg_remote: str) -> str:
    """Extract the sim_<hash> token from an aggregated remote path."""
    if not agg_remote:
        return ""
    tail = agg_remote.rstrip("/").split("/")[-1]
    for tok in tail.split("_"):
        if tok.startswith("sim"):
            return tail  # keep the full <ts>_sim_<hash> folder name
    return tail


def build(out_path: Path) -> int:
    rows = []
    for sweep_dir in sorted(p for p in RESULTS_ROOT.iterdir() if p.is_dir()):
        csv_path = sweep_dir / "benchmark.csv"
        if not csv_path.is_file():
            continue
        with csv_path.open(newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                road = (rec.get("road") or "").strip()
                mapa = ROAD_TO_MAPA.get(road, road or "Almenara")
                kind = rec.get("kind", "")
                label = rec.get("label", "")
                agg_remote = (rec.get("agg_dir_remote") or "").strip()

                if kind == "baseline":
                    # Baselines run directly on a lab host under this override
                    # dir (round-robin host, not recorded in the CSV).
                    veh = rec.get("vehicles", "")
                    acc = "1" if str(rec.get("accident")).lower() == "true" \
                        else "0"
                    remote = f"(lab host):~/VehicleKnowledge/_lan/" \
                             f"baseline_{veh}_{acc}"
                    agg_local = ""
                    master = "(lab host round-robin)"
                    sim_id = ""
                else:
                    remote = f"{MASTER_HOST}:{agg_remote}" if agg_remote else ""
                    agg_local = str(sweep_dir / "aggregated" / label)
                    master = MASTER_HOST if agg_remote else ""
                    sim_id = _sim_id(agg_remote)

                rows.append({
                    "mapa": mapa,
                    "road": road or "modified_4ways_all",
                    "sweep_id": sweep_dir.name,
                    "kind": kind,
                    "label": label,
                    "partition": rec.get("partition", ""),
                    "num_groups": rec.get("num_groups", ""),
                    "transport": rec.get("transport", ""),
                    "vehicles": rec.get("vehicles", ""),
                    "accident": rec.get("accident", ""),
                    "status": rec.get("status", ""),
                    "speedup": rec.get("speedup", ""),
                    "wall_sec": rec.get("wall_sec", ""),
                    "runtime_sec": rec.get("runtime_sec", ""),
                    "handoffs_out": rec.get("handoffs_out", ""),
                    "handoffs_in": rec.get("handoffs_in", ""),
                    "barrier_total_sec": rec.get("barrier_total_sec", ""),
                    "barrier_timeouts": rec.get("barrier_timeouts", ""),
                    "sim_id": sim_id,
                    "master_host": master,
                    "agg_remote": remote,
                    "agg_local": agg_local,
                    "sweep_dir": str(sweep_dir),
                    "benchmark_csv": str(csv_path),
                })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(rows)

    # Console summary by map + status.
    by_mapa: dict = {}
    for r in rows:
        key = (r["mapa"], r["kind"], r["status"])
        by_mapa[key] = by_mapa.get(key, 0) + 1
    print(f"[index] {len(rows)} simulaciones -> {out_path}")
    for (mapa, kind, status), n in sorted(by_mapa.items()):
        print(f"  {mapa:10} {kind:12} {status:14} {n}")
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build simulation locator index")
    ap.add_argument("--out", default=str(RESULTS_ROOT / "INDEX_simulaciones.csv"))
    args = ap.parse_args(argv)
    build(Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
