"""Orquestador del experimento "Simpatizantes" sobre el cluster labrob (UJI).

Corre, en modo BATCH (cada simulacion completa = 1 job independiente, sin
particionar la red) el barrido:

    3 redes  x  10 ratios (10..90, 99)  x  {sin accidente, con accidente}
    = 60 simulaciones por lote (batch).

Todo se ejecuta en un directorio remoto SEPARADO (``VehicleKnowledge_simpatizantes``)
para no mezclarlo con lo anterior del cluster, y TODOS los resultados se
recolectan en este PC bajo ``D:\\Simpatizantes`` (un subdirectorio por lote,
y dentro una carpeta por simulacion con todos sus ficheros: trip_data.json,
environment_traffic.json, tripinfo.xml, simulation_info.json, el trips usado,
etc.). Cada simulacion guarda ``_run_meta.json`` identificando el nodo (PC) que
la ejecuto, el tipo de sim y su configuracion.

Subcomandos::

    python -m SIMULATION.distributed.lan.run_simpatizantes deploy
    python -m SIMULATION.distributed.lan.run_simpatizantes start   [--workers-per-host 4]
    python -m SIMULATION.distributed.lan.run_simpatizantes run     [--batch A|B] [--trips FILE] [--limit N]
    python -m SIMULATION.distributed.lan.run_simpatizantes status
    python -m SIMULATION.distributed.lan.run_simpatizantes stop

Config por entorno (con defaults sensatos para este experimento)::

    VK_LAN_HOSTS       (default: labrob07..12.act.uji.es -- los que tienen SUMO 1.27.0)
    VK_LAN_MASTER      (default: primer host)
    VK_LAN_USER        (default: usuario)
    VK_LAN_REMOTE_DIR  (default: VehicleKnowledge_simpatizantes)
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from SIMULATION.distributed.lan.hosts import LanConfig  # noqa: E402
from SIMULATION.distributed.lan.ssh import Ssh  # noqa: E402
from SIMULATION.distributed.lan.cluster import (  # noqa: E402
    Cluster, env_prefix, KEY_PATH, LAN_DIR, MASTER_PORT,
)

# ==================== Experiment configuration ====================

DEFAULT_HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(7, 13)]
REMOTE_DIR = os.environ.get("VK_LAN_REMOTE_DIR", "VehicleKnowledge_simpatizantes")

ROADS = ["modified_2ways", "modified_3ways_top", "modified_4ways_all"]
RATIOS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 99]
ACCIDENTS = [False, True]

# Ventana del accidente identica al lote original del paper.
ACCIDENT_START = 7200
# OJO: los simulation_info.json de mayo decian 2400 pero era un bug de
# reporting; la corrida REAL del paper uso 3600 s. Verificado el 2026-07-21
# reproduciendo bit a bit la sim de mayo (30% acc 4ways: meanSpd 77.11,
# envSum 54494, accSeg 7395) con f4b2dcb + SUMO 1.24.0 + duration 3600.
ACCIDENT_DURATION = 3600

# Sharing identico al lote original del paper (verificado en los
# simulation_info.json de las 56 sims de mayo): v2v. Con "none" los
# inteligentes casi nunca tienen señal de congestion por delante y no
# reroutean (todas las curvas salen iguales).
SHARING = ["v2v"]

# Codigo de SIMULACION identico al usado para las graficas del paper:
# commit f4b2dcb, extraido en un worktree aparte. CLASS/ y SIMULATION/*.py se despliegan
# desde ahi; SIMULATION/distributed/ (solo orquestacion, no toca la sim)
# se despliega desde el arbol actual.
SIM_CODE_ROOT = Path(os.environ.get("VK_SIM_CODE_ROOT", r"D:\Iván\VK_f4b"))

# Salida local (este PC): todo se guarda aqui.
LOCAL_OUT = Path(r"D:\Simpatizantes")

# Ficheros a desplegar en cada nodo (ademas del codigo).
NET_FILES = [f"SIMULATION/roads/{r}.net.xml" for r in ROADS]
BASE_TRIPS = [
    "SIMULATION/trips/fullnet_trips_intelligent_10.xml",
    "SIMULATION/trips/fullnet_trips_intelligent_50.xml",
    "SIMULATION/trips/fullnet_trips_intelligent_99.xml",
]

STATE_FILE = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "_simpatizantes_state.json"


# ==================== Config helpers ====================

def load_cfg() -> LanConfig:
    hosts_env = os.environ.get("VK_LAN_HOSTS")
    hosts = ([h.strip() for h in hosts_env.split(",") if h.strip()]
             if hosts_env else list(DEFAULT_HOSTS))
    user = os.environ.get("VK_LAN_USER", "usuario")
    master = os.environ.get("VK_LAN_MASTER", hosts[0] if hosts else "")
    return LanConfig(user=user, password=os.environ.get("VK_LAN_PASSWORD", "practicas"),
                     hosts=hosts, master=master, remote_dir=REMOTE_DIR)


def _ssh(host: str, cfg: LanConfig, timeout: float = 25.0) -> Ssh:
    return Ssh(host, cfg.user, key_filename=KEY_PATH, timeout=timeout).connect()


# ==================== Deploy ====================

def _collect_deploy_files() -> List[Tuple[Path, str]]:
    """(local_abs_path, remote_rel_path) de todo lo que hay que desplegar.

    El codigo de SIMULACION (CLASS/, SIMULATION/*.py, data/, roads/, trips/)
    sale del worktree del commit del paper (SIM_CODE_ROOT = f4b2dcb) para que
    la fisica/logica sea EXACTAMENTE la de las graficas originales. Solo
    SIMULATION/distributed/ (orquestacion worker/master) sale de HEAD.
    """
    out: List[Tuple[Path, str]] = []

    def add_py(root: Path, rel_dir: str, recursive: bool):
        base = root / rel_dir
        if not base.is_dir():
            return
        it = base.rglob("*.py") if recursive else base.glob("*.py")
        for p in it:
            if "__pycache__" in p.parts:
                continue
            out.append((p, p.relative_to(root).as_posix()))

    # --- version del paper (f4b2dcb) ---
    add_py(SIM_CODE_ROOT, "CLASS", recursive=False)
    add_py(SIM_CODE_ROOT, "SIMULATION", recursive=False)
    for rel in NET_FILES + BASE_TRIPS:
        out.append((SIM_CODE_ROOT / rel, rel))
    # bifurcaciones validadas + config extra que main.py consulta
    data_dir = SIM_CODE_ROOT / "SIMULATION" / "data"
    if data_dir.is_dir():
        for p in data_dir.glob("*.json"):
            out.append((p, p.relative_to(SIM_CODE_ROOT).as_posix()))

    # --- orquestacion (HEAD actual) ---
    add_py(PROJECT_ROOT, "SIMULATION/distributed", recursive=True)
    return out


def cmd_deploy(args) -> int:
    cfg = load_cfg()
    pairs = _collect_deploy_files()
    for t in (args.extra_trips or []):
        pairs.append((PROJECT_ROOT / "SIMULATION" / "trips" / t,
                      f"SIMULATION/trips/{t}"))
    # de-dup + keep existing
    seen, files = set(), []
    for local, rel in pairs:
        if rel in seen:
            continue
        if local.is_file():
            files.append((local, rel))
            seen.add(rel)
        else:
            print(f"[deploy] WARN missing local file: {local}")

    total_mb = sum(local.stat().st_size for local, _ in files) / 1e6
    print(f"[deploy] {len(files)} files (~{total_mb:.1f} MB) -> {cfg.hosts}")
    print(f"[deploy] sim code from: {SIM_CODE_ROOT} (paper commit f4b2dcb)")
    print(f"[deploy] remote_dir=~/{cfg.remote_dir}")

    ok_hosts = []
    for host in cfg.hosts:
        print(f"\n=== deploy {host} ===", flush=True)
        try:
            with Ssh(host, cfg.user, key_filename=KEY_PATH, timeout=25) as s:
                s.run(f"mkdir -p '{cfg.remote_dir}'")
                for local, rel in files:
                    s.put(str(local), posixpath.join(cfg.remote_dir, rel))
                s.run(f"cd '{cfg.remote_dir}' && touch SIMULATION/__init__.py "
                      f"CLASS/__init__.py 2>/dev/null; true")
                s.run(f"find $HOME/{cfg.remote_dir} -name __pycache__ -type d "
                      f"-exec rm -rf {{}} + 2>/dev/null; true")
                s.run("python3 -m pip install --user --quiet psutil 2>&1 | tail -1; true",
                      timeout=180)
                chk = s.run(
                    env_prefix(cfg.remote_dir) +
                    " python3 -c 'import sumolib, traci; "
                    "from SIMULATION.CONSTANTS import get_trips_file, get_network_file; "
                    "print(\"IMPORTS_OK\")'",
                    timeout=60)
                ok = "IMPORTS_OK" in chk.out
                print(f"[{host}] {'OK' if ok else 'FAIL'} :: "
                      f"{(chk.out + ' ' + chk.err).strip()[-200:]}")
                if ok:
                    ok_hosts.append(host)
        except Exception as e:  # noqa: BLE001
            print(f"[{host}] FAIL :: {type(e).__name__}: {e}")
    print(f"\n[deploy] success {len(ok_hosts)}/{len(cfg.hosts)} -> {ok_hosts}")
    return 0 if len(ok_hosts) == len(cfg.hosts) else 1


# ==================== Start / stop cluster ====================

def _start_workers_multi(cfg: LanConfig, per_host: int) -> Dict[str, str]:
    pids: Dict[str, str] = {}
    master_url = f"{cfg.master}:{MASTER_PORT}"
    for host in cfg.hosts:
        try:
            s = _ssh(host, cfg)
            try:
                for i in range(per_host):
                    label = f"{host}#w{i}"
                    log = f"$HOME/{cfg.remote_dir}/{LAN_DIR}/worker_{i}.log"
                    out_root = f"{LAN_DIR}/worker_runs/{label}"
                    cmd = (env_prefix(cfg.remote_dir) +
                           " python3 -m SIMULATION.distributed.worker "
                           f"--master http://{master_url} --label '{label}' "
                           f"--output-root {out_root}")
                    r = s.start_background(cmd, log)
                    pids[label] = r.out.strip()
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            print(f"[start] worker launch failed on {host}: {e}")
    return pids


def cmd_start(args) -> int:
    cfg = load_cfg()
    cl = Cluster(cfg)
    print(f"[start] master on {cfg.master} (transports=tcp)")
    cl.start_master(transports="tcp")
    if not cl.wait_master(timeout=40):
        print("[start] ERROR: master did not come up")
        return 1
    print("[start] master up")
    per_host = args.workers_per_host
    print(f"[start] launching {per_host} workers x {len(cfg.hosts)} hosts "
          f"= {per_host * len(cfg.hosts)} workers")
    _start_workers_multi(cfg, per_host)
    n_expected = per_host * len(cfg.hosts)
    alive = cl.wait_workers(max(1, n_expected // 2), timeout=90)
    print(f"[start] workers registered (alive>={alive})")
    st = cl.status()
    print(f"[start] status workers={len(st.get('workers', []))}")
    return 0


def cmd_stop(args) -> int:
    cfg = load_cfg()
    Cluster(cfg).stop_all()
    print("[stop] master + workers killed on all hosts")
    return 0


def cmd_status(args) -> int:
    cfg = load_cfg()
    st = Cluster(cfg).status()
    ws = st.get("workers", [])
    alive = sum(1 for w in ws if w.get("alive"))
    print(f"workers alive : {alive}/{len(ws)}")
    print(f"pending       : {len(st.get('pending_jobs', []))}")
    print(f"running       : {len(st.get('running_jobs', []))}")
    print(f"completed     : {st.get('completed_jobs', 0)}")
    print(f"failed        : {st.get('failed_jobs', 0)}")
    return 0


# ==================== Submit + poll + harvest ====================

def _build_payloads(batch: str, trips: Optional[str]) -> List[dict]:
    payloads = []
    for road in ROADS:
        for ratio in RATIOS:
            for acc in ACCIDENTS:
                payloads.append({
                    "mode": "batch",
                    "road": road,
                    "ratio": str(ratio),
                    "sharing": list(SHARING),
                    "accident": acc,
                    "accident_start": ACCIDENT_START,
                    "accident_duration": ACCIDENT_DURATION,
                    "force_cv": False,
                    "vehicles": None,
                    "save_environment": True,
                    "save_knowledge": False,
                    "save_tripinfo": True,
                    "max_steps": 0,
                    "workers": 1,
                    "trips": trips,
                    "label": f"{batch}/{road}/{ratio}/{'acc' if acc else 'noacc'}",
                })
    return payloads


def _sim_dirname(road: str, ratio: int, acc: bool) -> str:
    return f"{road}_{ratio}pct_{'accident' if acc else 'noaccident'}"


def run_batch(batch: str, trips: Optional[str], limit: int = 0) -> int:
    cfg = load_cfg()
    cl = Cluster(cfg)

    batch = batch.upper()
    batch_dirname = (f"batch{batch}_" +
                     ("trips_fullnet_base" if not trips else f"trips_{Path(trips).stem}"))
    out_root = LOCAL_OUT / batch_dirname
    out_root.mkdir(parents=True, exist_ok=True)

    # sanity: master reachable
    st = cl.status()
    if "__error__" in st:
        print(f"[run] ERROR master unreachable: {st['__error__']}. Run 'start' first.")
        return 1
    alive = sum(1 for w in st.get("workers", []) if w.get("alive"))
    if alive == 0:
        print("[run] ERROR: no workers alive. Run 'start' first.")
        return 1

    payloads = _build_payloads(batch, trips)
    if limit:
        payloads = payloads[:limit]
    print(f"[run] batch {batch}: submitting {len(payloads)} sims "
          f"(trips={trips or 'ratio-resolved base'}) -> {out_root}")

    # job_id -> config meta
    jobmeta: Dict[str, dict] = {}
    for i, pl in enumerate(payloads, 1):
        resp = cl.submit(pl)
        jids = resp.get("job_ids") or []
        if not jids:
            print(f"[run] {i}/{len(payloads)} SUBMIT FAILED: {resp}")
            continue
        jid = jids[0]
        jobmeta[jid] = {
            "job_id": jid, "batch": batch, "road": pl["road"],
            "ratio": int(pl["ratio"]), "accident": pl["accident"],
            "trips": trips or "base(ratio-resolved)",
            "dirname": _sim_dirname(pl["road"], int(pl["ratio"]), pl["accident"]),
            "worker": None, "duration_sec": None, "success": None, "harvested": False,
        }
        print(f"[run] {i}/{len(payloads)} submitted {pl['label']} -> {jid}")

    total = len(jobmeta)
    _save_state({"batch": batch, "trips": trips, "out_root": str(out_root),
                 "jobmeta": jobmeta, "submitted_at": time.time()})

    # Poll until all completed/failed
    print(f"[run] waiting for {total} jobs to finish...")
    t0 = time.time()
    while True:
        st = cl.status()
        if "__error__" in st:
            time.sleep(10)
            continue
        for cd in st.get("completed_detail", []):
            m = jobmeta.get(cd["job_id"])
            if m:
                m["worker"] = cd.get("worker_id")
                m["duration_sec"] = cd.get("duration_sec")
                m["success"] = cd.get("success")
        for fd in st.get("failed_detail", []):
            m = jobmeta.get(fd["job_id"])
            if m:
                m["worker"] = fd.get("worker_id")
                m["success"] = False
                m["error"] = fd.get("error")
        done = sum(1 for m in jobmeta.values() if m["success"] is not None)
        running = len(st.get("running_jobs", []))
        pending = len(st.get("pending_jobs", []))
        el = int(time.time() - t0)
        print(f"[run] {el:5d}s  done={done}/{total}  running={running}  "
              f"pending={pending}", flush=True)
        if done >= total:
            break
        time.sleep(15)

    _save_state({"batch": batch, "trips": trips, "out_root": str(out_root),
                 "jobmeta": jobmeta, "submitted_at": t0})

    # Harvest
    print(f"[run] harvesting results -> {out_root}")
    ms = cl.master_ssh()
    upl_base = f"{cfg.remote_dir}/SIMULATION/distributed/master_work/uploads"
    n_ok = 0
    for jid, m in jobmeta.items():
        if not m.get("success"):
            print(f"[harvest] SKIP {m['dirname']} (job {jid} failed: "
                  f"{m.get('error', '?')})")
            continue
        remote_res = f"{upl_base}/{jid}/extracted/result"
        local_dir = out_root / m["dirname"]
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            nf = ms.get_dir(remote_res, str(local_dir))
        except Exception as e:  # noqa: BLE001
            print(f"[harvest] ERROR {m['dirname']}: {e}")
            continue
        # copiar el trips usado a la carpeta (todo debe quedar aqui)
        _copy_trips_used(local_dir, m, trips)
        (local_dir / "_run_meta.json").write_text(
            json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
        m["harvested"] = True
        n_ok += 1
        print(f"[harvest] OK   {m['dirname']}  ({nf} files, node={m['worker']})")

    # Manifest del lote
    manifest = {
        "batch": batch,
        "trips": trips or "base(ratio-resolved: fullnet_trips_intelligent_{10|50|99})",
        "accident_start": ACCIDENT_START,
        "accident_duration": ACCIDENT_DURATION,
        "sharing": list(SHARING),
        "roads": ROADS, "ratios": RATIOS,
        "hosts": cfg.hosts, "master": cfg.master,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_sims": total, "n_harvested": n_ok,
        "sims": list(jobmeta.values()),
    }
    (out_root / "_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[run] DONE batch {batch}: harvested {n_ok}/{total} -> {out_root}")
    print(f"[run] manifest: {out_root / '_manifest.json'}")
    return 0 if n_ok == total else 1


def _copy_trips_used(local_dir: Path, m: dict, trips: Optional[str]) -> None:
    """Copy the trips XML actually used into the sim folder (best effort)."""
    try:
        info = json.loads((local_dir / "simulation_info.json").read_text(encoding="utf-8"))
        used = info.get("files", {}).get("trips")
    except Exception:
        used = None
    src_name = used or trips
    if not src_name:
        return
    src = PROJECT_ROOT / "SIMULATION" / "trips" / Path(src_name).name
    if src.is_file():
        try:
            shutil.copy2(src, local_dir / "trips_used.xml")
        except Exception:
            pass


def cmd_run(args) -> int:
    return run_batch(args.batch, args.trips, args.limit)


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


# ==================== Push extra trips (targeted) ====================

def push_trips(cfg: LanConfig, filenames: List[str]) -> None:
    """SFTP one or more trips files to remote_dir/SIMULATION/trips on all hosts."""
    for host in cfg.hosts:
        try:
            with Ssh(host, cfg.user, key_filename=KEY_PATH, timeout=25) as s:
                for fn in filenames:
                    local = PROJECT_ROOT / "SIMULATION" / "trips" / fn
                    if not local.is_file():
                        print(f"[push] WARN missing local {local}")
                        continue
                    remote = posixpath.join(cfg.remote_dir, "SIMULATION", "trips", fn)
                    s.put(str(local), remote)
                print(f"[push] {host}: {', '.join(filenames)} OK")
        except Exception as e:  # noqa: BLE001
            print(f"[push] {host}: FAIL {type(e).__name__}: {e}")


def cmd_push_trips(args) -> int:
    push_trips(load_cfg(), args.files)
    return 0


# ==================== Documentation ====================

def write_docs(cfg: LanConfig) -> None:
    LOCAL_OUT.mkdir(parents=True, exist_ok=True)
    trips_b = "fullnet_trips_intelligent_regenB_10000.xml"
    doc = f"""# Experimento "Simpatizantes" - datos y resultados

Generado: {datetime.now().isoformat(timespec='seconds')}

## Que es esto
Barrido de simulaciones SUMO ejecutado en modo BATCH (cada simulacion completa
= 1 trabajo independiente, la red NO se particiona) sobre el cluster de la UJI,
usando el sistema distribuido solo como granja de trabajos. Todos los resultados
se recolectan en este PC bajo `D:\\Simpatizantes`.

## Barrido (por lote)
- Redes: {', '.join(ROADS)}
- Ratios de vehiculos inteligentes: {', '.join(str(r) for r in RATIOS)} %
- Escenarios: sin accidente y con accidente
- Accidente: start={ACCIDENT_START} s (t=2 h), duracion={ACCIDENT_DURATION} s (1 h)
  -> identico al lote original del paper (el 2400 que decian los
  simulation_info.json de mayo era un bug de reporting).
- 10.000 vehiculos, sharing=v2v (flag identico al lote original; en esta
  version el v2v real no comparte nada: el inteligente decide en cada
  bifurcacion consultando el maxSpeed actual de la via con findRoute),
  tripinfo + environment ON.
- El % inteligente se aplica en runtime por hash md5 del id de vehiculo
  (determinista y monotono); el atributo type= del XML se ignora.

## Lotes
- `batchA_trips_fullnet_base/`  -> trips ORIGINALES (resolucion por ratio:
  fullnet_trips_intelligent_{{10|50|99}}.xml; comparten O/D y tiempos de salida).
- `batchB_trips_{Path(trips_b).stem}/` -> trips REGENERADOS ({trips_b}):
  mismos ids veh_0..veh_9999 y mismos tiempos de salida que el original, pero
  O/D re-muestreado con semilla 20260721. Sirve para comprobar si el resultado
  "malo" de 99 % + accidente en la red de 3 itinerarios viene dado por el fichero
  de viajes.

## Estructura de carpetas
```
D:\\Simpatizantes\\
  <lote>\\
    _manifest.json                      # config del lote + lista de sims + nodo que corrio cada una
    <red>_<ratio>pct_<accident|noaccident>\\
        trip_data.json                  # por-vehiculo: velocidad, duracion, distancia, espera
        environment_traffic.json        # observaciones por segmento (conteo, velocidad)
        environment_traffic.edgedata.xml
        simulation_info.json            # red, ratio, accidente, trips usado, nº vehiculos
        tripinfo.xml
        trips_used.xml                  # copia del fichero de viajes usado
        _run_meta.json                  # nodo (PC) que ejecuto, duracion, exito, config
```

## Nodos (cluster UJI)
- Master: {cfg.master}
- Workers: {', '.join(cfg.hosts)} (SUMO 1.24.0 fijado via pip --user,
  la MISMA version que corrio el lote original del paper en este PC)
- Directorio remoto SEPARADO: ~/{cfg.remote_dir}
- Cada sim guarda en `_run_meta.json` el nodo `worker_id` (labrobNN#wK) que la ejecuto.

## Como recalcular tablas y graficas
Los ficheros son identicos a los de `knowledge_output`, asi que los scripts de
`SIMULATION/PYTHON_ETL/paper_simpatizantes/` funcionan tal cual: basta apuntar su
`BASE` a `D:\\Simpatizantes\\<lote>` y usar como mapa el `_manifest.json` (o un
`_found` con claves "<red>_<ratio>pct[_accident]"). El filtro de warm-up
(depart >= 3600, desde las 20:00) sigue viviendo en los scripts.

## Interfaz grafica
`simpatizantes_gui.py` (lanzador `run_gui.bat`) explora estos datos: elegir
ventana horaria (inicio 19:00; por defecto 20:00->fin, excluye precalentamiento),
seleccionar simulaciones, sacar medias por sim y por tipo de vehiculo, y graficar
una metrica frente al % de inteligentes. Ver `COMO_USAR_GUI.md`.

## Reproducibilidad
Las simulaciones son DETERMINISTAS (SUMO con semilla por defecto fija + hash de
ids), por lo que el nodo que ejecute cada sim no altera el resultado, solo el
tiempo. El fichero de trips B se regenera con `SIMULATION/trips/_gen_tripsB.py`
(semilla 20260721).

## Version exacta del paper (validado 2026-07-21)
Receta que reproduce BIT A BIT las simulaciones de mayo de las graficas:
- Codigo: commit `f4b2dcb` (worktree `D:\\Iván\\VK_f4b`).
- SUMO 1.24.0.
- Accidente: start 7200 s, duracion 3600 s, sharing v2v (inerte).
Validacion: 4ways 30% acc -> meanSpd 77.11, meanDur 568.0, envSum 54494,
accSeg 7395, n225 133, cv230 134 == identico a
`20260515_234851_modified_4ways_all_30pct_accident` de mayo.
"""
    (LOCAL_OUT / "README.md").write_text(doc, encoding="utf-8")
    print(f"[docs] wrote {LOCAL_OUT / 'README.md'}")


def cmd_docs(args) -> int:
    write_docs(load_cfg())
    return 0


# ==================== Chain: batch A -> batch B -> docs ====================

def cmd_chain(args) -> int:
    cfg = load_cfg()
    trips_b = args.trips_b
    a_manifest = LOCAL_OUT / "batchA_trips_fullnet_base" / "_manifest.json"

    print(f"[chain] waiting for batch A to finish ({a_manifest}) ...", flush=True)
    t0 = time.time()
    while not a_manifest.is_file():
        if time.time() - t0 > args.max_wait:
            print("[chain] TIMEOUT waiting for batch A manifest; aborting chain")
            return 1
        time.sleep(30)
    print(f"[chain] batch A finished after {int(time.time()-t0)}s. "
          f"Deploying batch B trips and launching batch B.")

    push_trips(cfg, [trips_b])
    rc = run_batch("B", trips_b, 0)

    write_docs(cfg)
    if args.stop:
        Cluster(cfg).stop_all()
        print("[chain] cluster stopped")
    print(f"[chain] ALL DONE (batch B rc={rc})")
    return rc


# ==================== CLI ====================

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Orquestador experimento Simpatizantes")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("deploy", help="Subir codigo+datos a los nodos")
    pd.add_argument("--extra-trips", nargs="*", default=None,
                    help="Ficheros trips extra (nombre en SIMULATION/trips) p.ej. batch B")
    pd.set_defaults(func=cmd_deploy)

    ps = sub.add_parser("start", help="Arrancar master + workers")
    ps.add_argument("--workers-per-host", type=int, default=4)
    ps.set_defaults(func=cmd_start)

    pr = sub.add_parser("run", help="Enviar un lote, esperar y recolectar")
    pr.add_argument("--batch", default="A", help="Etiqueta del lote (A, B, ...)")
    pr.add_argument("--trips", default=None,
                    help="Fichero trips (nombre en SIMULATION/trips) para batch B")
    pr.add_argument("--limit", type=int, default=0, help="Limitar #sims (smoke test)")
    pr.set_defaults(func=cmd_run)

    pp = sub.add_parser("push-trips", help="Subir ficheros trips a los nodos")
    pp.add_argument("files", nargs="+", help="Nombres en SIMULATION/trips")
    pp.set_defaults(func=cmd_push_trips)

    pc = sub.add_parser("chain", help="Esperar a batch A, luego lanzar batch B + docs")
    pc.add_argument("--trips-b", default="fullnet_trips_intelligent_regenB_10000.xml")
    pc.add_argument("--max-wait", type=float, default=6 * 3600)
    pc.add_argument("--stop", action="store_true", help="Parar cluster al terminar")
    pc.set_defaults(func=cmd_chain)

    sub.add_parser("docs", help="Escribir README en D:\\Simpatizantes").set_defaults(func=cmd_docs)
    sub.add_parser("status", help="Estado del cluster").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="Parar master + workers").set_defaults(func=cmd_stop)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
