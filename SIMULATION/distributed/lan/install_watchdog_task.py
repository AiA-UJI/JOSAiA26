"""Registra el watchdog como TAREA PROGRAMADA de Windows para que reviva tras un
corte de luz / reinicio (trigger ONLOGON: al iniciar sesion el usuario).

Genera un .bat con los parametros elegidos y lo registra con schtasks. Al
reiniciar la maquina y entrar el usuario, la tarea lanza el watchdog, que retoma
la campana (resumible).

    python -m SIMULATION.distributed.lan.install_watchdog_task \
        --hosts labrob08.act.uji.es,labrob09.act.uji.es \
        --master labrob08.act.uji.es --protocols tcp,udp,http --reps 3

Desinstalar:
    python -m SIMULATION.distributed.lan.install_watchdog_task --uninstall
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = ROOT / "SIMULATION" / "distributed" / "lan" / "campaign_state"
BAT = STATE_DIR / "run_watchdog.bat"
TASK = "VK_Rotterdam_Watchdog"


def build_bat(args) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    parts = [
        f'"{py}" -u -m SIMULATION.distributed.lan.campaign_watchdog',
        f'--hosts {args.hosts}' if args.hosts else "",
        f'--master {args.master}' if args.master else "",
        f'--user {args.user}',
        f'--protocols {args.protocols}',
        f'--flotas {args.flotas}',
        f'--reps {args.reps}',
        f'--cap {args.cap}',
        f'--refresh-hours {args.refresh_hours}',
        f'--baselines-from "{args.baselines_from}"' if args.baselines_from else "",
        "--reverse" if args.reverse else "",
    ]
    cmd = " ".join(p for p in parts if p)
    lines = [
        "@echo off",
        f'cd /d "{ROOT}"',
        "set PYTHONUNBUFFERED=1",
        cmd,
    ]
    BAT.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    print(f"[install] bat generado: {BAT}")


def install(args) -> int:
    build_bat(args)
    rc = subprocess.run(
        ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK,
         "/TR", f'"{BAT}"', "/RL", "LIMITED", "/F"],
        check=False).returncode
    if rc == 0:
        print(f"[install] tarea '{TASK}' registrada (ONLOGON).")
        print("          El watchdog arrancara al iniciar sesion tras un reinicio.")
        print(f"          Para arrancarlo YA: schtasks /Run /TN {TASK}")
    else:
        print(f"[install] schtasks devolvio rc={rc}")
    return rc


def uninstall() -> int:
    rc = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"],
                        check=False).returncode
    print(f"[install] desinstalar tarea '{TASK}' rc={rc}")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--hosts", default="")
    ap.add_argument("--master", default="")
    ap.add_argument("--user", default="usuario")
    ap.add_argument("--protocols", default="tcp,udp,http,zmq,grpc")
    ap.add_argument("--flotas", default="15000,20000,30000")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--cap", default="14400")
    ap.add_argument("--refresh-hours", type=float, default=24.0)
    ap.add_argument("--baselines-from", default="")
    ap.add_argument("--reverse", action="store_true")
    args = ap.parse_args(argv)

    if args.uninstall:
        return uninstall()
    return install(args)


if __name__ == "__main__":
    raise SystemExit(main())
