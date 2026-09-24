"""Consolidate every sweep's benchmark.csv into one master CSV.

Merges all ``results/<ts>/benchmark.csv`` files, tags each row with its sweep
and horizon, and adds derived columns (parallel efficiency, ms per barrier,
degraded flag). Output: ``results/master_results.csv``.
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "results"

# Human-readable metadata per sweep dir.
SWEEPS = {
    "20260627_190108": ("pilot_broken", "bounded_1800",
                        "Piloto inicial: bug UDP/ZMQ mono-hilo, sin handoffs (descartado)"),
    "20260627_204719": ("barrido_principal", "bounded_1800",
                        "Barrido transportes/particiones/escalado/accidente (1800 pasos)"),
    "20260627_235113": ("horizonte_completo", "full",
                        "balanced-y-4 y balanced-x-4, tcp, horizonte completo"),
    "20260628_171534": ("protocolos_full", "full",
                        "5 protocolos x 4 recuentos x sin/con accidente, balanced-y-4, full"),
}

OUT_COLS = [
    "sweep_id", "sweep_label", "horizon", "stage", "kind", "label",
    "partition", "num_groups", "transport", "vehicles", "accident", "status",
    "wall_sec", "runtime_sec", "speedup", "parallel_efficiency",
    "merged_trips", "handoffs_out", "handoffs_in", "barriers",
    "barrier_total_sec", "ms_per_barrier", "barrier_timeouts",
    "handoff_failures", "self_disabled", "degraded", "notes", "ts",
]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def main() -> None:
    rows = []
    for sweep_dir, (label, horizon, notes) in SWEEPS.items():
        csvp = ROOT / sweep_dir / "benchmark.csv"
        if not csvp.is_file():
            continue
        with open(csvp, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                speedup = _f(r.get("speedup"))
                ng = _i(r.get("num_groups")) or 0
                bt = _f(r.get("barrier_total_sec"))
                nb = _i(r.get("barriers"))
                ho = _i(r.get("handoffs_out"))
                self_dis = str(r.get("self_disabled", "")).strip().lower() in ("true", "1")
                eff = (speedup / ng) if (speedup is not None and ng > 1) else None
                mspb = (bt / nb * 1000.0) if (bt is not None and nb) else None
                degraded = (r.get("kind") == "distributed" and
                            (self_dis or (ho is not None and ho == 0)))
                rows.append({
                    "sweep_id": sweep_dir,
                    "sweep_label": label,
                    "horizon": horizon,
                    "stage": r.get("stage", ""),
                    "kind": r.get("kind", ""),
                    "label": r.get("label", ""),
                    "partition": r.get("partition", ""),
                    "num_groups": r.get("num_groups", ""),
                    "transport": r.get("transport", ""),
                    "vehicles": r.get("vehicles", ""),
                    "accident": r.get("accident", ""),
                    "status": r.get("status", ""),
                    "wall_sec": r.get("wall_sec", ""),
                    "runtime_sec": r.get("runtime_sec", ""),
                    "speedup": r.get("speedup", ""),
                    "parallel_efficiency": f"{eff:.4f}" if eff is not None else "",
                    "merged_trips": r.get("merged_trips", ""),
                    "handoffs_out": r.get("handoffs_out", ""),
                    "handoffs_in": r.get("handoffs_in", ""),
                    "barriers": r.get("barriers", ""),
                    "barrier_total_sec": r.get("barrier_total_sec", ""),
                    "ms_per_barrier": f"{mspb:.2f}" if mspb is not None else "",
                    "barrier_timeouts": r.get("barrier_timeouts", ""),
                    "handoff_failures": r.get("handoff_failures", ""),
                    "self_disabled": r.get("self_disabled", ""),
                    "degraded": "true" if degraded else "false",
                    "notes": notes,
                    "ts": r.get("ts", ""),
                })

    # Sort: sweep, kind (baseline first), partition, transport, vehicles, accident
    order = {"baseline": 0, "distributed": 1}
    rows.sort(key=lambda r: (
        r["sweep_id"], order.get(r["kind"], 9), r["partition"],
        r["transport"], _i(r["vehicles"]) or 0, r["accident"],
    ))

    out = ROOT / "master_results.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(rows)

    nb = sum(1 for r in rows if r["kind"] == "baseline")
    nd = sum(1 for r in rows if r["kind"] == "distributed")
    ndeg = sum(1 for r in rows if r["degraded"] == "true")
    print(f"[master] {out}")
    print(f"[master] {len(rows)} filas: {nb} baselines + {nd} distribuidas "
          f"({ndeg} degradadas)")


if __name__ == "__main__":
    main()
