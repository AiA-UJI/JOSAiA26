"""
Overnight benchmark: single-instance baseline vs balanced synced-spatial
distribution (2 SUMO instances, network split in load-balanced geographic
halves) on the Almenara network (``modified_4ways_all``).

Runs, sequentially and unattended, for ratio=99 %%:

    noacc  8000     baseline + distributed
    noacc  10000    baseline + distributed
    acc    8000     baseline + distributed
    acc    10000    baseline + distributed

For each config it records wall times and computes the speedup, writing the
results incrementally to a CSV (so partial results survive a crash).

The script manages its own master + 2 workers on a dedicated loopback port
and tears them down at the end.

Usage:
    python -m SIMULATION.distributed.run_overnight_benchmark
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PORT = 9300
MASTER_URL = f"http://127.0.0.1:{PORT}"
SYNC_URL = f"127.0.0.1:{PORT}"
ROAD = "modified_4ways_all"
RATIO = "99"
WORKERS = 2

RESULTS_DIR = PROJECT_ROOT / "SIMULATION" / "distributed" / "benchmark_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_PATH = RESULTS_DIR / f"benchmark_{STAMP}.csv"
LOG_DIR = RESULTS_DIR / f"logs_{STAMP}"
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = PROJECT_ROOT / "SIMULATION" / "distributed" / "master_work"
AGG_DIR = WORK_DIR / "aggregated"
UPLOADS_DIR = WORK_DIR / "uploads"

# (label, vehicles, accident)
CONFIGS: List[Tuple[str, int, bool]] = [
    ("noacc_8k", 8000, False),
    ("noacc_10k", 10000, False),
    ("acc_8k", 8000, True),
    ("acc_10k", 10000, True),
]

CSV_FIELDS = [
    "config", "accident", "vehicles",
    "baseline_sec", "dist_endtoend_sec", "dist_maxsubjob_sec",
    "speedup_endtoend", "speedup_subjob",
    "g0_trips", "g1_trips",
    "barriers", "handoffs_out", "handoffs_in", "handoff_failures",
    "dist_steps_max", "merged_trips", "sync_active", "notes",
]


def log(msg: str) -> None:
    print(f"[bench {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(path: str, timeout: float = 10.0):
    with urlreq.urlopen(MASTER_URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(path: str, payload: dict, timeout: float = 120.0):
    body = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(MASTER_URL + path, data=body,
                         headers={"Content-Type": "application/json"},
                         method="POST")
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def wait_master_up(timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            http_get("/api/status", timeout=3.0)
            return True
        except (HTTPError, URLError, OSError):
            time.sleep(0.5)
    return False


def wait_workers(n: int, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = http_get("/api/status")
            alive = sum(1 for w in d["workers"] if w.get("alive"))
            if alive >= n:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def run_baseline(vehicles: int, accident: bool, cfg_label: str) -> float:
    cmd = [sys.executable, "-m", "SIMULATION.main",
           "--road", ROAD, "--ratio", RATIO,
           "--vehicles", str(vehicles),
           "--save-tripinfo", "--no-save-environment", "--no-save-knowledge"]
    if accident:
        cmd.append("--accident")
    log(f"baseline START {cfg_label}: {' '.join(cmd)}")
    logf = LOG_DIR / f"baseline_{cfg_label}.log"
    t0 = time.perf_counter()
    with logf.open("w", encoding="utf-8") as fp:
        rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT), stdout=fp,
                             stderr=subprocess.STDOUT)
    dt = time.perf_counter() - t0
    log(f"baseline DONE  {cfg_label}: rc={rc} wall={dt:.1f}s")
    if rc != 0:
        raise RuntimeError(f"baseline rc={rc} (see {logf})")
    return dt


def _latest_agg_dir(parent_id: str) -> Optional[Path]:
    cands = sorted(AGG_DIR.glob(f"*_{parent_id}"))
    return cands[-1] if cands else None


def _read_sync_stats(job_ids: List[str]) -> Dict[str, int]:
    agg = {"barriers": 0, "handoffs_out": 0, "handoffs_in": 0,
           "handoff_failures": 0, "steps_max": 0, "sync_active": 0}
    for jid in job_ids:
        info = UPLOADS_DIR / jid / "extracted" / "result" / "simulation_info.json"
        if not info.is_file():
            continue
        try:
            d = json.loads(info.read_text(encoding="utf-8"))
        except Exception:
            continue
        agg["steps_max"] = max(agg["steps_max"], int(d.get("actual_steps", 0)))
        st = d.get("sync_stats")
        if st:
            agg["sync_active"] = 1
            agg["barriers"] += int(st.get("barriers", 0))
            agg["handoffs_out"] += int(st.get("handoffs_out", 0))
            agg["handoffs_in"] += int(st.get("handoffs_in", 0))
            agg["handoff_failures"] += int(st.get("handoff_failures", 0))
    return agg


def run_distributed(vehicles: int, accident: bool, cfg_label: str
                    ) -> Dict[str, object]:
    payload = {
        "mode": "synced-spatial",
        "road": ROAD,
        "ratio": RATIO,
        "vehicles": vehicles,
        "accident": accident,
        "workers": WORKERS,
        "balanced": True,
        "sharing": ["none"],
        "save_environment": False,
        "save_knowledge": False,
        "save_tripinfo": True,
        "max_steps": 0,
        "sync_master_url": SYNC_URL,
        "label": f"bench_{cfg_label}",
    }
    log(f"distributed SUBMIT {cfg_label}: {payload}")
    t0 = time.perf_counter()
    resp = http_post("/api/submit", payload, timeout=300.0)
    parent_id = resp.get("parent_sim_id")
    job_ids = resp.get("job_ids") or []
    log(f"distributed submitted {cfg_label}: parent={parent_id} jobs={job_ids}")
    if not parent_id:
        raise RuntimeError(f"submit failed: {resp}")

    # Poll until aggregated
    deadline = time.time() + 3 * 3600
    status = "pending"
    while time.time() < deadline:
        try:
            d = http_get("/api/status")
        except Exception:
            time.sleep(5)
            continue
        p = d["parent_sims"].get(parent_id)
        if p:
            status = p["status"]
            if status in ("aggregated", "aggregation_error", "partial_failure"):
                break
        if d.get("failed_jobs", 0) and status == "pending":
            # detect hard failures via running/failed counts is unreliable;
            # rely on parent status + timeout instead.
            pass
        time.sleep(5)
    dt = time.perf_counter() - t0
    log(f"distributed DONE   {cfg_label}: status={status} wall={dt:.1f}s")

    out: Dict[str, object] = {
        "dist_endtoend_sec": round(dt, 2),
        "status": status,
        "parent_id": parent_id,
    }
    agg_dir = _latest_agg_dir(parent_id)
    if agg_dir:
        summ = agg_dir / "aggregate_summary.json"
        if summ.is_file():
            s = json.loads(summ.read_text(encoding="utf-8"))
            out["dist_maxsubjob_sec"] = s.get("wallclock_sec")
            out["merged_trips"] = s.get("merged_trips")
            subs = s.get("sub_jobs", [])
            for sj in subs:
                if sj.get("corridor") == "G0":
                    out["g0_trips"] = sj.get("trip_count")
                elif sj.get("corridor") == "G1":
                    out["g1_trips"] = sj.get("trip_count")
    stats = _read_sync_stats(job_ids)
    out.update(stats)
    return out


def main() -> int:
    log(f"results CSV: {CSV_PATH}")
    log(f"logs dir   : {LOG_DIR}")

    # Start master
    master_log = (LOG_DIR / "master.log").open("w", encoding="utf-8")
    master = subprocess.Popen(
        [sys.executable, "-m", "SIMULATION.distributed", "master",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(PROJECT_ROOT), stdout=master_log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    procs = [master]
    try:
        if not wait_master_up():
            log("ERROR: master did not come up")
            return 1
        log("master up")

        # Start 2 workers
        for i in range(WORKERS):
            wlog = (LOG_DIR / f"worker{i}.log").open("w", encoding="utf-8")
            w = subprocess.Popen(
                [sys.executable, "-m", "SIMULATION.distributed", "worker",
                 "--master", SYNC_URL, "--label", f"benchw{i}"],
                cwd=str(PROJECT_ROOT), stdout=wlog, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            procs.append(w)
        if not wait_workers(WORKERS):
            log("ERROR: workers did not register")
            return 1
        log(f"{WORKERS} workers registered")

        # CSV header
        with CSV_PATH.open("w", newline="", encoding="utf-8") as fp:
            csv.DictWriter(fp, fieldnames=CSV_FIELDS).writeheader()

        for cfg_label, vehicles, accident in CONFIGS:
            row = {k: "" for k in CSV_FIELDS}
            row.update({"config": cfg_label, "accident": int(accident),
                        "vehicles": vehicles})
            try:
                base = run_baseline(vehicles, accident, cfg_label)
                row["baseline_sec"] = round(base, 2)
            except Exception as e:
                row["notes"] = f"baseline_error: {e}"
                base = None
                log(f"baseline ERROR {cfg_label}: {e}")

            try:
                dist = run_distributed(vehicles, accident, cfg_label)
                row["dist_endtoend_sec"] = dist.get("dist_endtoend_sec", "")
                row["dist_maxsubjob_sec"] = dist.get("dist_maxsubjob_sec", "")
                row["g0_trips"] = dist.get("g0_trips", "")
                row["g1_trips"] = dist.get("g1_trips", "")
                row["barriers"] = dist.get("barriers", "")
                row["handoffs_out"] = dist.get("handoffs_out", "")
                row["handoffs_in"] = dist.get("handoffs_in", "")
                row["handoff_failures"] = dist.get("handoff_failures", "")
                row["dist_steps_max"] = dist.get("steps_max", "")
                row["merged_trips"] = dist.get("merged_trips", "")
                row["sync_active"] = dist.get("sync_active", "")
                if dist.get("status") != "aggregated":
                    row["notes"] = (str(row["notes"]) + f" dist_status="
                                    f"{dist.get('status')}").strip()
                # Speedups
                d_e2e = dist.get("dist_endtoend_sec")
                d_sub = dist.get("dist_maxsubjob_sec")
                if base and d_e2e:
                    row["speedup_endtoend"] = round(base / float(d_e2e), 3)
                if base and d_sub:
                    row["speedup_subjob"] = round(base / float(d_sub), 3)
            except Exception as e:
                row["notes"] = (str(row["notes"]) + f" dist_error: {e}").strip()
                log(f"distributed ERROR {cfg_label}: {e}")

            with CSV_PATH.open("a", newline="", encoding="utf-8") as fp:
                csv.DictWriter(fp, fieldnames=CSV_FIELDS).writerow(row)
            log(f"row written for {cfg_label}: baseline={row['baseline_sec']} "
                f"dist={row['dist_endtoend_sec']} "
                f"speedup_e2e={row['speedup_endtoend']} "
                f"speedup_sub={row['speedup_subjob']} "
                f"sync_active={row['sync_active']}")

        log(f"ALL DONE. CSV at {CSV_PATH}")
        return 0
    finally:
        log("tearing down master + workers")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(2)
        for p in procs:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
