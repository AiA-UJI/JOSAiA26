"""Valida 1 sim del cluster (4ways 30% acc, codigo f4b2dcb) contra mayo."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.run_simpatizantes import (  # noqa: E402
    load_cfg, ACCIDENT_START, ACCIDENT_DURATION, SHARING)
from SIMULATION.distributed.lan.cluster import Cluster  # noqa: E402

OUT = Path(r"D:\Simpatizantes\_validacion_30acc")
MAY = (PROJECT_ROOT / "SIMULATION" / "PYTHON_ETL" / "paper_simpatizantes" /
       "knowledge_output" / "20260515_234851_modified_4ways_all_30pct_accident")


def signature(folder: Path) -> dict:
    trips = json.loads((folder / "trip_data.json").read_text(encoding="utf-8"))
    n_int = sum(1 for t in trips if t.get("vType") == "intelligent")
    speeds = [float(t.get("speed_kmh", 0)) for t in trips]
    env = json.loads((folder / "environment_traffic.json").read_text(encoding="utf-8"))

    def seg_sum(seg_id):
        seg = env.get(seg_id, {})
        return sum(o.get("count", 0) for sl in seg.values()
                   if isinstance(sl, list) for o in sl)

    return {
        "n_trips": len(trips),
        "intel": n_int,
        "meanSpd": round(sum(speeds) / len(speeds), 2),
        "envSum": round(sum(o.get("count", 0) for seg in env.values()
                            if isinstance(seg, dict)
                            for sl in seg.values() if isinstance(sl, list)
                            for o in sl)),
        "accSeg": round(seg_sum("467787494")),
        "n225": round(seg_sum("22562073#2")),
    }


def main() -> int:
    cfg = load_cfg()
    cl = Cluster(cfg)
    payload = {
        "mode": "batch", "road": "modified_4ways_all", "ratio": "30",
        "sharing": list(SHARING), "accident": True,
        "accident_start": ACCIDENT_START, "accident_duration": ACCIDENT_DURATION,
        "force_cv": False, "vehicles": None,
        "save_environment": True, "save_knowledge": False, "save_tripinfo": True,
        "max_steps": 0, "workers": 1, "trips": None,
        "label": "VALIDACION/4ways/30/acc",
    }
    resp = cl.submit(payload)
    jid = (resp.get("job_ids") or [None])[0]
    if not jid:
        print(f"[val] SUBMIT FAILED: {resp}")
        return 1
    print(f"[val] submitted {jid}; waiting...")
    t0 = time.time()
    while True:
        st = cl.status()
        det = {d["job_id"]: d for d in st.get("completed_detail", [])}
        det.update({d["job_id"]: d for d in st.get("failed_detail", [])})
        if jid in det:
            d = det[jid]
            print(f"[val] finished success={d.get('success')} err={d.get('error')}")
            if not d.get("success"):
                return 1
            break
        print(f"[val] {int(time.time()-t0):4d}s ...", flush=True)
        time.sleep(20)

    OUT.mkdir(parents=True, exist_ok=True)
    ms = cl.master_ssh()
    remote = f"{cfg.remote_dir}/SIMULATION/distributed/master_work/uploads/{jid}/extracted/result"
    nf = ms.get_dir(remote, str(OUT))
    print(f"[val] harvested {nf} files -> {OUT}")

    new = signature(OUT)
    may = signature(MAY)
    print(f"\n{'':10s} {'MAYO':>10s} {'CLUSTER':>10s}")
    ok = True
    for k in new:
        flag = "OK " if may[k] == new[k] else "≈  " if abs(float(may[k]) - float(new[k])) / max(1.0, abs(float(may[k]))) < 0.15 else "!!!"
        if flag == "!!!":
            ok = False
        print(f"{k:10s} {may[k]:>10} {new[k]:>10}   {flag}")
    print(f"\n[val] {'VALIDACION SUPERADA' if ok else 'DESVIACION GRANDE — revisar'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
