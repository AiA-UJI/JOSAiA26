"""Lanzador: localiza 60 sims y genera plots de flujo e intensidad."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINDER = HERE / "_find_sims.py"
PLOTTER = HERE / "plot_flow_count.py"


def run(label: str, script: Path):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    rc = subprocess.call([sys.executable, str(script)])
    if rc != 0:
        print(f"[ERROR] {label} exit={rc}")
        sys.exit(rc)


def main():
    run("1/2 Localizando carpetas (60 sims)", FINDER)
    run("2/2 Generando plots flujo + intensidad", PLOTTER)
    print(f"\n[ALL DONE] resultados en: {HERE / 'results'}")


if __name__ == "__main__":
    main()
