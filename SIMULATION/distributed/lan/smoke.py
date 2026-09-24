"""End-to-end smoke test on the lab cluster.

Brings up master + workers, runs ONE small distributed sim (balanced-x-2, tcp,
short horizon) and ONE baseline, prints timings, then tears everything down.

    python -m SIMULATION.distributed.lan.smoke
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.cluster import Cluster, MASTER_PORT  # noqa: E402
from SIMULATION.distributed.lan.hosts import load_config  # noqa: E402

MAX_STEPS = 600
VEHICLES = 8000


def main() -> int:
    cfg = load_config()
    cl = Cluster(cfg)
    print(f"[smoke] master={cfg.master} hosts={len(cfg.hosts)}")

    print("[smoke] tearing down any stale processes...")
    cl.stop_all()
    time.sleep(2)

    print("[smoke] starting master...")
    mpid = cl.start_master()
    print(f"[smoke] master pid={mpid}")
    if not cl.wait_master(30):
        print("[smoke] FAIL: master not responding")
        cl.stop_all()
        return 1
    print("[smoke] master up")

    print("[smoke] starting workers...")
    wpids = cl.start_workers()
    print(f"[smoke] worker pids={wpids}")
    alive = cl.wait_workers(2, 60)
    print(f"[smoke] workers alive={alive}")
    if alive < 2:
        print("[smoke] FAIL: need >=2 workers")
        cl.stop_all()
        return 1

    # ---- distributed run ----
    print("[smoke] submitting distributed balanced-x-2 / tcp ...")
    payload = {
        "mode": "synced-spatial",
        "road": "modified_4ways_all",
        "ratio": "99",
        "vehicles": VEHICLES,
        "workers": 2,
        "partition": "balanced-x-2",
        "barrier_transport": "tcp",
        "accident": False,
        "max_steps": MAX_STEPS,
        "save_environment": False,
        "save_knowledge": False,
        "save_tripinfo": True,
        "sync_master_url": f"{cfg.master}:{MASTER_PORT}",
        "label": "smoke-balx2-tcp",
    }
    res = cl.submit(payload)
    print(f"[smoke] submit -> {res}")
    pid = res.get("parent_sim_id")
    if not pid:
        print("[smoke] FAIL: no parent_sim_id")
        cl.stop_all()
        return 1

    p = cl.wait_parent(pid, timeout=1800, poll=5)
    print(f"[smoke] parent status={p.get('status')} agg={p.get('aggregated_dir')}")
    if p.get("status") == "aggregated" and p.get("aggregated_dir"):
        local = str(PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" /
                    "_smoke_result")
        n = cl.fetch_aggregated(p["aggregated_dir"], local)
        print(f"[smoke] fetched {n} files -> {local}")
        summ = Path(local) / "aggregate_summary.json"
        if summ.is_file():
            print("[smoke] aggregate_summary:")
            print(summ.read_text(encoding="utf-8"))

    # ---- baseline ----
    print("[smoke] running baseline (direct) ...")
    b = cl.run_baseline(cfg.hosts[-1], "fullnet_trips_intelligent_99_8000.xml",
                        VEHICLES, accident=False, out_subdir="_smoke_baseline",
                        max_steps=MAX_STEPS, timeout=1800)
    print(f"[smoke] baseline -> {b}")

    print("[smoke] tearing down...")
    cl.stop_all()
    print("[smoke] DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
