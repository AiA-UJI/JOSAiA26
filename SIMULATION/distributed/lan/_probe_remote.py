"""Sondeo remoto: procesos vivos relacionados con la campana + sesiones
screen/tmux + fecha de los resultados remotos, en cada host."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from SIMULATION.distributed.lan.ssh import Ssh  # noqa

KEY = str(Path.home() / ".ssh" / "id_ed25519")
USER = "usuario"
HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(7, 19)]

CMD = (
    "echo '--- procs ---'; "
    "ps -eo pid,etimes,cmd | grep -E 'run_lan_sweep|run_rotterdam|campaign_watchdog|distributed.master|distributed.worker|run_rotterdam_all_reps' | grep -v grep; "
    "echo '--- screen ---'; screen -ls 2>/dev/null | grep -E 'Detached|Attached' ; "
    "echo '--- tmux ---'; tmux ls 2>/dev/null; "
    "echo '--- results mtime ---'; "
    "ls -dt $HOME/VehicleKnowledge/SIMULATION/distributed/lan/results/*/ 2>/dev/null | head -1; "
    "stat -c '%y' $(ls -dt $HOME/VehicleKnowledge/SIMULATION/distributed/lan/results/*/ 2>/dev/null | head -1) 2>/dev/null; "
    "echo '--- master_work ---'; ls -la $HOME/VehicleKnowledge/SIMULATION/distributed/master_work 2>/dev/null | head -5"
)

for h in HOSTS:
    print("\n" + "=" * 60 + f"\n{h}")
    try:
        with Ssh(h, USER, key_filename=KEY, timeout=12) as s:
            r = s.run(CMD, timeout=30)
            out = (r.out or "").strip()
            err = (r.err or "").strip()
            print(out if out else "(sin salida)")
            if err:
                print("[stderr]", err[:200])
    except Exception as e:
        print(f"  SSH FAIL {type(e).__name__}: {e}")
