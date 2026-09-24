"""Espera a que el job de prueba termine y valida la recoleccion end-to-end."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from SIMULATION.distributed.lan.run_simpatizantes import load_cfg  # noqa: E402
from SIMULATION.distributed.lan.cluster import Cluster  # noqa: E402

JOB = "b48b5ce80929"
DIRNAME = "modified_2ways_10pct_noaccident"
OUT = Path(r"D:\Simpatizantes\batchSMOKE_trips_fullnet_base") / DIRNAME


def main() -> int:
    cfg = load_cfg()
    cl = Cluster(cfg)
    t0 = time.time()
    res = None
    while time.time() - t0 < 900:
        st = cl.status()
        if "__error__" in st:
            time.sleep(8)
            continue
        for cd in st.get("completed_detail", []):
            if cd["job_id"] == JOB:
                res = cd
                break
        for fd in st.get("failed_detail", []):
            if fd["job_id"] == JOB:
                print(f"[validate] job FAILED: {fd}")
                return 1
        if res:
            break
        print(f"[validate] {int(time.time()-t0):4d}s waiting... "
              f"running={len(st.get('running_jobs', []))} "
              f"completed={st.get('completed_jobs')}", flush=True)
        time.sleep(12)

    if not res:
        print("[validate] timeout waiting for job")
        return 1
    print(f"[validate] job done: node={res['worker_id']} "
          f"dur={res['duration_sec']}s success={res['success']}")

    OUT.mkdir(parents=True, exist_ok=True)
    ms = cl.master_ssh()
    remote = f"{cfg.remote_dir}/SIMULATION/distributed/master_work/uploads/{JOB}/extracted/result"
    n = ms.get_dir(remote, str(OUT))
    print(f"[validate] harvested {n} files -> {OUT}")
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(OUT).as_posix():45s} {p.stat().st_size:>10d} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
