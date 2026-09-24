"""LAN benchmark orchestrator.

Brings up the cluster, runs the baseline + distributed experiment matrix,
collects metrics into an incremental CSV, fetches aggregated artefacts, and
generates partition maps + benchmark plots into a fresh results directory.

    python -m SIMULATION.distributed.lan.run_lan_sweep [options]

Everything is resumable-friendly: the CSV is appended row by row, and each
distributed run's aggregated output is fetched immediately.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.cluster import Cluster, MASTER_PORT  # noqa: E402
from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402
from SIMULATION.distributed.lan.matrix import (  # noqa: E402
    Experiment, build_matrix, build_custom_matrix, trips_for,
)

CSV_FIELDS = [
    "ts", "stage", "kind", "label", "road", "partition", "num_groups",
    "transport", "vehicles", "accident", "status",
    "wall_sec", "runtime_sec", "speedup",
    "merged_trips", "handoffs_out", "handoffs_in", "barriers",
    "barrier_total_sec", "barrier_timeouts", "handoff_failures",
    "self_disabled", "agg_dir_remote",
]


class CsvLog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

    def row(self, **kw):
        rec = {k: kw.get(k, "") for k in CSV_FIELDS}
        with self.lock:
            with self.path.open("a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(rec)
        print(f"[csv] {rec['label']:<34} status={rec['status']:<10} "
              f"wall={rec['wall_sec']}s rt={rec['runtime_sec']}s "
              f"speedup={rec['speedup']}")


def run_baselines(cl: Cluster, baselines: List[Experiment], hosts: List[str],
                  csvlog: CsvLog, max_steps: int, accident_start: Optional[int],
                  accident_duration: Optional[int], timeout: float,
                  results: Dict, road: str = "modified_4ways_all") -> Dict:
    """Run baselines in parallel: one per host at a time (per-host queue)."""
    # distribute round-robin into per-host queues
    queues: Dict[str, List[Experiment]] = {h: [] for h in hosts}
    for i, e in enumerate(baselines):
        queues[hosts[i % len(hosts)]].append(e)

    base_runtime: Dict[tuple, float] = {}
    lock = threading.Lock()

    def worker(host: str, q: List[Experiment]):
        for e in q:
            trips = trips_for(road, e.vehicles)
            print(f"[baseline] {host} -> {e.label}", flush=True)
            t0 = time.time()
            try:
                b = cl.run_baseline(
                    host, trips, e.vehicles, e.accident,
                    out_subdir=f"baseline_{e.vehicles}_{int(e.accident)}",
                    max_steps=max_steps, timeout=timeout, road=road)
                rt = b.get("runtime_sec")
                status = "ok" if b.get("rc") == 0 and rt else "failed"
            except Exception as ex:
                b = {"wall_sec": round(time.time() - t0, 1), "tail": str(ex)}
                rt = None
                status = "error"
            if rt:
                with lock:
                    base_runtime[(e.vehicles, e.accident)] = float(rt)
            csvlog.row(ts=datetime.now().isoformat(timespec="seconds"),
                       stage="baseline", kind="baseline", label=e.label,
                       road=road, partition="", num_groups=1, transport="",
                       vehicles=e.vehicles, accident=e.accident, status=status,
                       wall_sec=b.get("wall_sec"), runtime_sec=rt, speedup="")

    threads = [threading.Thread(target=worker, args=(h, q))
               for h, q in queues.items() if q]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results["base_runtime"] = base_runtime
    return base_runtime


def run_distributed(cl: Cluster, exps: List[Experiment], csvlog: CsvLog,
                    base_runtime: Dict, agg_root: Path, cfg, max_steps: int,
                    accident_start: Optional[int], accident_duration: Optional[int],
                    per_run_timeout: float, road: str = "modified_4ways_all"):
    for e in exps:
        payload = {
            "mode": "synced-spatial",
            "road": road,
            "ratio": "99",
            "vehicles": e.vehicles,
            "workers": e.num_groups,
            "partition": e.partition,
            "barrier_transport": e.transport,
            "accident": e.accident,
            "max_steps": max_steps,
            "save_environment": False,
            "save_knowledge": False,
            "save_tripinfo": True,
            "sync_master_url": f"{cfg.master}:{MASTER_PORT}",
            "label": e.label,
        }
        if e.accident and accident_start is not None:
            payload["accident_start"] = accident_start
            payload["accident_duration"] = accident_duration

        print(f"\n[dist] === {e.label} (stage {e.stage}) ===", flush=True)
        t0 = time.time()
        res = cl.submit(payload)
        pid = res.get("parent_sim_id")
        if not pid:
            csvlog.row(ts=datetime.now().isoformat(timespec="seconds"),
                       stage=e.stage, kind="distributed", label=e.label,
                       road=road, partition=e.partition,
                       num_groups=e.num_groups,
                       transport=e.transport, vehicles=e.vehicles,
                       accident=e.accident, status="submit_error")
            print(f"[dist] submit error: {res}")
            continue

        p = cl.wait_parent(pid, timeout=per_run_timeout, poll=5)
        wall = round(time.time() - t0, 1)
        status = p.get("status", "timeout")

        summary = {}
        agg_remote = p.get("aggregated_dir")
        if status == "aggregated" and agg_remote:
            local = agg_root / e.label
            try:
                cl.fetch_aggregated(agg_remote, str(local))
                sp = local / "aggregate_summary.json"
                if sp.is_file():
                    summary = json.loads(sp.read_text(encoding="utf-8"))
            except Exception as ex:
                print(f"[dist] fetch error: {ex}")

        runtime = summary.get("wallclock_sec")
        sync = summary.get("sync") or {}
        denom = base_runtime.get((e.vehicles, e.accident))
        speedup = round(denom / runtime, 3) if (denom and runtime) else ""

        csvlog.row(
            ts=datetime.now().isoformat(timespec="seconds"),
            stage=e.stage, kind="distributed", label=e.label,
            road=road, partition=e.partition, num_groups=e.num_groups,
            transport=e.transport, vehicles=e.vehicles, accident=e.accident,
            status=status, wall_sec=wall, runtime_sec=runtime, speedup=speedup,
            merged_trips=summary.get("merged_trips"),
            handoffs_out=sync.get("handoffs_out"),
            handoffs_in=sync.get("handoffs_in"),
            barriers=sync.get("barriers_max"),
            barrier_total_sec=sync.get("barrier_total_sec_max"),
            barrier_timeouts=sync.get("barrier_timeouts"),
            handoff_failures=sync.get("handoff_failures"),
            self_disabled=sync.get("self_disabled"),
            agg_dir_remote=agg_remote,
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LAN distributed benchmark sweep")
    ap.add_argument("--max-steps", type=int, default=0,
                    help="0 = full simulation; else cap sim steps")
    ap.add_argument("--accident-start", type=int, default=1800)
    ap.add_argument("--accident-duration", type=int, default=3600)
    ap.add_argument("--per-run-timeout", type=float, default=5400.0)
    ap.add_argument("--baseline-timeout", type=float, default=7200.0)
    ap.add_argument("--max-groups", type=int, default=6)
    ap.add_argument("--only-stage", default=None,
                    help="comma list to restrict stages, e.g. baseline,T,P")
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--keep-cluster", action="store_true",
                    help="don't tear down master/workers at the end")
    # Custom (focused) matrix overrides; when --partitions is given the staged
    # matrix is replaced by the cartesian product of these lists + baselines.
    ap.add_argument("--partitions", default=None,
                    help="comma list, e.g. balanced-y-4,balanced-x-4")
    ap.add_argument("--vehicles", default=None,
                    help="comma list, e.g. 8000,10000,12000,15000")
    ap.add_argument("--transports", default="tcp",
                    help="comma list for custom matrix (default tcp)")
    ap.add_argument("--accidents", default="false,true",
                    help="comma list of false/true for custom matrix")
    ap.add_argument("--road", default="modified_4ways_all",
                    help="network name (e.g. rotterdam_arterial)")
    ap.add_argument("--no-baselines", action="store_true",
                    help="skip baseline runs (use with --baselines-from)")
    ap.add_argument("--run-tag", default="",
                    help="suffix for the results dir; required when several "
                         "sweeps run concurrently so they don't share a dir")
    ap.add_argument("--no-report", action="store_true",
                    help="skip the global HTML presentation (slow and racy "
                         "when several sweeps run concurrently)")
    ap.add_argument("--baselines-from", default=None,
                    help="comma list of benchmark.csv paths to reuse baseline "
                         "runtimes from (kind=baseline, status=ok)")
    args = ap.parse_args(argv)

    cfg = load_config()
    cl = Cluster(cfg)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.run_tag:
        ts = f"{ts}_{args.run_tag}"
    results_root = (PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" /
                    "results" / ts)
    agg_root = results_root / "aggregated"
    agg_root.mkdir(parents=True, exist_ok=True)
    csvlog = CsvLog(results_root / "benchmark.csv")
    print(f"[sweep] results -> {results_root}")

    if args.partitions:
        parts = [p.strip() for p in args.partitions.split(",") if p.strip()]
        vehs = ([int(v) for v in args.vehicles.split(",")]
                if args.vehicles else [10000])
        txs = [t.strip() for t in args.transports.split(",") if t.strip()]
        accs = [a.strip().lower() in ("1", "true", "yes")
                for a in args.accidents.split(",")]
        exps = build_custom_matrix(parts, vehs, accs, txs,
                                   max_groups=args.max_groups)
    else:
        exps = build_matrix(max_groups=args.max_groups)
    if args.only_stage:
        keep = set(args.only_stage.split(","))
        exps = [e for e in exps if e.stage in keep]
    baselines = [e for e in exps if e.kind == "baseline"]
    distributed = [e for e in exps if e.kind == "distributed"]
    print(f"[sweep] {len(baselines)} baselines + {len(distributed)} distributed runs")

    print("[sweep] cleaning stale processes...")
    cl.stop_all()
    time.sleep(2)

    results: Dict = {}
    # 1) baselines in parallel (no cluster needed)
    if baselines and not args.no_baselines:
        print("[sweep] running baselines (parallel across hosts)...")
        run_baselines(cl, baselines, cfg.hosts, csvlog, args.max_steps,
                      args.accident_start, args.accident_duration,
                      args.baseline_timeout, results, road=args.road)
    base_runtime = results.get("base_runtime", {})

    # 1b) reuse baseline runtimes from previous sweeps' CSVs
    if args.baselines_from:
        for p in args.baselines_from.split(","):
            p = p.strip()
            if not p:
                continue
            try:
                with open(p, newline="", encoding="utf-8") as f:
                    for rec in csv.DictReader(f):
                        if (rec.get("kind") == "baseline"
                                and rec.get("status") == "ok"
                                and rec.get("runtime_sec")
                                and rec.get("road", args.road) == args.road):
                            key = (int(rec["vehicles"]),
                                   rec["accident"].strip().lower() == "true")
                            base_runtime.setdefault(
                                key, float(rec["runtime_sec"]))
            except Exception as ex:
                print(f"[sweep] could not read baselines from {p}: {ex}")
        print(f"[sweep] baseline runtimes: "
              f"{ {k: round(v, 1) for k, v in base_runtime.items()} }")

    # 2) start cluster for distributed runs
    if distributed:
        print("[sweep] starting master + workers...")
        cl.start_master()
        if not cl.wait_master(40):
            print("[sweep] FATAL: master did not come up")
            return 1
        cl.start_workers()
        alive = cl.wait_workers(min(args.max_groups, len(cfg.hosts)), 90)
        print(f"[sweep] workers alive={alive}")

        run_distributed(cl, distributed, csvlog, base_runtime, agg_root, cfg,
                        args.max_steps, args.accident_start,
                        args.accident_duration, args.per_run_timeout,
                        road=args.road)

    # 3) plots + partition maps
    if not args.no_plots:
        try:
            from SIMULATION.distributed.lan.plot_benchmark import plot_all
            plot_all(results_root / "benchmark.csv", results_root / "plots")
        except Exception as ex:
            print(f"[sweep] plot_benchmark failed: {ex}")
        try:
            from SIMULATION.distributed.lan.plot_partition_map import plot_all_partitions
            parts_to_plot = None
            if args.partitions:
                parts_to_plot = [p.strip() for p in args.partitions.split(",")
                                 if p.strip()]
            plot_all_partitions(results_root / "partitions",
                                partitions=parts_to_plot, road=args.road)
        except Exception as ex:
            print(f"[sweep] plot_partition_map failed: {ex}")

    if not args.keep_cluster:
        print("[sweep] tearing down cluster...")
        cl.stop_all()

    # Build the combined HTML presentation across ALL sweeps (this one + any
    # previous), embedding every partition map, plot and results table.
    if args.no_report:
        print(f"[sweep] DONE -> {results_root}")
        return 0
    try:
        from SIMULATION.distributed.lan.report import build as build_report
        from SIMULATION.distributed.lan import report as _rep
        out = _rep.RESULTS_ROOT / (
            f"PRESENTACION_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        build_report(out)
        print(f"[sweep] PRESENTATION -> {out}")
    except Exception as ex:
        print(f"[sweep] report generation failed: {ex}")

    print(f"[sweep] DONE -> {results_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
