"""Grafica coches ACTIVOS por MAQUINA (nodo/grupo) a lo largo de los steps de
simulacion, para cada CORTE, cada CARGA (flota) y cada ACCIDENTE.

Lee SIMULATION/distributed/lan/occupancy/<label>/<group>.json (cosechado por
harvest_occupancy). Una figura por corte: rejilla filas=flota (15k/20k/30k) x
columnas=accidente (sin/con); en cada panel una linea por maquina (G0..GN-1).
Sombreado = ventana de accidente (7200-10800 s).

    python -m SIMULATION.distributed.lan.plot_occupancy
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
OCC = ROOT / "SIMULATION" / "distributed" / "lan" / "occupancy"
OUT = OCC / "plots"

FLEETS = ["15000", "20000", "30000"]
FLEET_LBL = {"15000": "15k", "20000": "20k", "30000": "30k"}
ACC_START, ACC_END = 7200, 10800


def _parse(label: str):
    # balanced-x-12_tcp_30000_acc -> (balanced-x-12, 30000, True)
    parts = label.split("_")
    partition = parts[0]
    fleet = None
    acc = label.endswith("_acc")
    for p in parts:
        if p.isdigit():
            fleet = p
    return partition, fleet, acc


def _load_group(fp: Path):
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        s = d.get("samples", [])
        if not s:
            return None, None, None
        arr = np.asarray(s, float)
        return arr[:, 0], arr[:, 1], d.get("group", fp.stem)
    except Exception:
        return None, None, None


def collect() -> dict:
    """partition -> {(fleet, acc): [(group, t, y), ...]}."""
    data: dict = {}
    for d in OCC.iterdir():
        if not d.is_dir() or d.name == "plots":
            continue
        partition, fleet, acc = _parse(d.name)
        if fleet is None:
            continue
        series = []
        for fp in sorted(d.glob("*.json")):
            t, y, g = _load_group(fp)
            if t is not None:
                series.append((g, t, y))
        if series:
            data.setdefault(partition, {})[(fleet, acc)] = series
    return data


def plot_partition(partition, cells, plt):
    fig, axes = plt.subplots(3, 2, figsize=(15, 12), sharex=True)
    ngroups = max((len(v) for v in cells.values()), default=1)
    cmap = plt.get_cmap("turbo", max(ngroups, 2))
    for i, fleet in enumerate(FLEETS):
        for j, acc in enumerate((False, True)):
            ax = axes[i][j]
            series = cells.get((fleet, acc))
            if acc:
                ax.axvspan(ACC_START, ACC_END, color="#ffcccc", alpha=0.5,
                           zorder=0, label="accidente")
            if series:
                series = sorted(series, key=lambda s: s[0])
                for k, (g, t, y) in enumerate(series):
                    ax.plot(t, y, lw=1.0, color=cmap(k), label=g)
            ax.set_title(f"{FLEET_LBL[fleet]} veh · "
                         f"{'con' if acc else 'sin'} accidente", fontsize=10)
            ax.grid(alpha=0.25)
            if j == 0:
                ax.set_ylabel("coches activos")
            if i == 2:
                ax.set_xlabel("paso de simulación (s)")
            if series and len(series) <= 12:
                ax.legend(fontsize=6, ncol=2, loc="upper right")
    fig.suptitle(f"Coches activos por máquina — corte {partition} (Rotterdam TCP)",
                 fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    out = OUT / f"ocupacion_{partition}.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data = collect()
    print(f"[occ-plot] {len(data)} cortes con datos")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for partition in sorted(data):
        out = plot_partition(partition, data[partition], plt)
        print("   ->", out.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
