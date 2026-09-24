"""Watchdog de la campana Rotterdam: PERSISTENCIA + reanudacion automatica.

- Lanza el orquestador resumible (run_rotterdam_all_reps) como subproceso.
- Si el orquestador CAE (crash, corte de luz + reinicio, o termina sin cerrar la
  matriz) lo RE-ARRANCA automaticamente (la campana es resumible: retoma donde
  se quedo leyendo los benchmark.csv en disco).
- Cada --refresh-hours (def 24h) regenera el INDICE y escribe una FOTO del hueco
  (gap) en el log -> "constancia" para analizar.
- Escribe heartbeat + estado JSON continuamente, de modo que siempre hay registro
  de que esta corriendo y cuanto falta.
- Se detiene solo cuando la matriz esta completa (orquestador rc=0).

Para sobrevivir a un corte de LUZ hay que registrarlo como tarea de arranque de
Windows (ver install_watchdog_task.py): al reiniciar la maquina, la tarea vuelve
a lanzar este watchdog, que a su vez retoma la campana.

    python -m SIMULATION.distributed.lan.campaign_watchdog \
        --hosts labrob08.act.uji.es,labrob09.act.uji.es,... \
        --master labrob08.act.uji.es \
        --protocols tcp,udp,http --reps 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = ROOT / "SIMULATION" / "distributed" / "lan" / "campaign_state"
LOG = STATE_DIR / "watchdog.log"
STATE = STATE_DIR / "watchdog_state.json"
HEARTBEAT = STATE_DIR / "heartbeat.txt"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def write_state(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    except Exception:
        pass


def heartbeat(extra: str = "") -> None:
    try:
        HEARTBEAT.write_text(f"{_now()} {extra}", encoding="utf-8")
    except Exception:
        pass


def gap_snapshot(protocols: str, reps: int, flotas: str) -> str:
    """Devuelve el texto del informe de huecos (y lo loguea)."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "SIMULATION.distributed.lan.gap_report",
             "--reps", str(reps), "--protocols", protocols, "--flotas", flotas],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        txt = out.stdout.strip()
        log("GAP SNAPSHOT:\n" + txt)
        return txt
    except Exception as ex:
        log(f"gap_snapshot fallo: {ex}")
        return ""


def rebuild_index() -> None:
    try:
        subprocess.run([sys.executable, "-m",
                        "SIMULATION.distributed.lan.build_index"],
                       cwd=str(ROOT), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=600)
    except Exception:
        pass


def build_env(hosts: str, master: str, user: str) -> dict:
    env = dict(os.environ)
    if hosts:
        env["VK_LAN_HOSTS"] = hosts
    if master:
        env["VK_LAN_MASTER"] = master
    if user:
        env["VK_LAN_USER"] = user
    return env


def orchestrator_cmd(args) -> list:
    if args.deadline:
        # Planificador multi-grupo con plazo: reparte el cluster en grupos
        # concurrentes dimensionados a cada division.
        cmd = [sys.executable, "-u", "-m",
               "SIMULATION.distributed.lan.run_campaign_week",
               "--reps", str(args.reps),
               "--protocols", args.protocols,
               "--fleets", args.flotas,
               "--cap", args.cap,
               "--deadline", args.deadline,
               "--sizes", args.sizes]
        if args.hosts:
            cmd += ["--hosts", args.hosts]
        if args.baselines_from:
            cmd += ["--baselines-from", args.baselines_from]
        return cmd
    cmd = [sys.executable, "-u", "-m",
           "SIMULATION.distributed.lan.run_rotterdam_all_reps",
           "--reps", str(args.reps),
           "--protocols", args.protocols,
           "--flotas", args.flotas,
           "--cap", args.cap]
    if args.baselines_from:
        cmd += ["--baselines-from", args.baselines_from]
    if args.reverse:
        cmd += ["--reverse"]
    return cmd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", default="", help="VK_LAN_HOSTS (coma-separado)")
    ap.add_argument("--master", default="", help="VK_LAN_MASTER")
    ap.add_argument("--user", default=os.environ.get("VK_LAN_USER", "usuario"))
    ap.add_argument("--protocols", default="tcp,udp,http,zmq,grpc")
    ap.add_argument("--flotas", default="15000,20000,30000")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--cap", default="14400")
    ap.add_argument("--baselines-from", default="")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--deadline", default="",
                    help="'YYYY-MM-DD HH:MM'; si se indica se usa el "
                         "planificador multi-grupo run_campaign_week")
    ap.add_argument("--sizes", default="6,4,3,3,2,2,2",
                    help="tamano de los grupos concurrentes")
    ap.add_argument("--refresh-hours", type=float, default=24.0)
    ap.add_argument("--restart-delay", type=int, default=120,
                    help="segundos de espera antes de re-arrancar tras una caida")
    ap.add_argument("--max-restarts", type=int, default=0,
                    help="0 = ilimitado (recomendado para sobrevivir cortes)")
    args = ap.parse_args(argv)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = build_env(args.hosts, args.master, args.user)
    cmd = orchestrator_cmd(args)

    log("=" * 70)
    log(f"WATCHDOG ARRANCA. hosts={args.hosts or '(default)'} "
        f"master={args.master or '(default)'} protos={args.protocols} "
        f"reps={args.reps} cap={args.cap}")
    log("cmd: " + " ".join(cmd))
    gap_snapshot(args.protocols, args.reps, args.flotas)

    refresh_sec = max(60.0, args.refresh_hours * 3600.0)
    restarts = 0
    last_refresh = time.time()

    while True:
        log(f"lanzando orquestador (reinicio #{restarts})")
        write_state({"status": "running", "restarts": restarts,
                     "started": _now(), "cmd": cmd,
                     "hosts": args.hosts, "master": args.master})
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)

        # monitor: heartbeat + refresh 24h mientras el hijo vive
        while True:
            try:
                rc = proc.wait(timeout=60)
                break
            except subprocess.TimeoutExpired:
                heartbeat(f"orquestador vivo pid={proc.pid} reinicios={restarts}")
                if time.time() - last_refresh >= refresh_sec:
                    log(f"--- refresco {args.refresh_hours}h ---")
                    rebuild_index()
                    gap_snapshot(args.protocols, args.reps, args.flotas)
                    last_refresh = time.time()

        log(f"orquestador termino rc={rc}")
        rebuild_index()
        txt = gap_snapshot(args.protocols, args.reps, args.flotas)
        complete = (f"faltan para {args.reps}x: 0" in txt) or (rc == 0)
        write_state({"status": "finished" if complete else "down",
                     "restarts": restarts, "last_rc": rc, "when": _now(),
                     "hosts": args.hosts, "master": args.master})

        if complete:
            log("MATRIZ COMPLETA. Watchdog finaliza.")
            heartbeat("COMPLETO")
            return 0

        if args.deadline:
            try:
                dl = datetime.strptime(args.deadline, "%Y-%m-%d %H:%M")
                if datetime.now() >= dl:
                    log(f"plazo {args.deadline} alcanzado. Watchdog finaliza.")
                    heartbeat("PLAZO ALCANZADO")
                    return 0
            except ValueError:
                pass

        restarts += 1
        if args.max_restarts and restarts > args.max_restarts:
            log(f"alcanzado max-restarts={args.max_restarts}, paro")
            return 2
        log(f"campana incompleta -> re-arranco en {args.restart_delay}s")
        heartbeat(f"caido, re-arranca en {args.restart_delay}s")
        time.sleep(max(1, args.restart_delay))


if __name__ == "__main__":
    raise SystemExit(main())
