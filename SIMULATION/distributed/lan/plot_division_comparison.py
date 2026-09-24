"""Speedup-by-division figures (full horizon, TCP) for the paper.

Reads results/speedups_table.csv and renders:
  - division_speedup_10k.png       : grouped bars sin/con accidente @10k
  - division_speedup_vehicles.png  : speedup vs vehiculos por division (sin accidente)
"""

from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent / "results"
CSV = ROOT / "speedups_table.csv"
OUT = ROOT / "plots_division"

DIV_ORDER = [
    "corridor-2", "highway-2", "balanced-x-2", "balanced-y-2",
    "corridor-3", "balanced-x-3", "balanced-y-3",
    "corridor-4", "balanced-x-4", "balanced-y-4", "balanced-y-6",
]
WORKERS = {"corridor-2": 2, "highway-2": 2, "balanced-x-2": 2, "balanced-y-2": 2,
           "corridor-3": 3, "balanced-x-3": 3, "balanced-y-3": 3,
           "corridor-4": 4, "balanced-x-4": 4, "balanced-y-4": 4, "balanced-y-6": 6}
VEH = ["8000", "10000", "12000", "15000"]


def load():
    # data[division][accident][vehicles] = speedup (TCP only)
    data = defaultdict(lambda: {"no": {}, "si": {}})
    for r in csv.DictReader(open(CSV, encoding="utf-8")):
        if r["protocolo"] != "tcp":
            continue
        data[r["division"]][r["accidente"]][r["vehiculos"]] = float(r["speedup"])
    return data


def plot_10k(data):
    labels = [f"{d}\n({WORKERS[d]}w)" for d in DIV_ORDER]
    no = [data[d]["no"].get("10000", 0.0) for d in DIV_ORDER]
    ac = [data[d]["si"].get("10000", 0.0) for d in DIV_ORDER]
    x = range(len(DIV_ORDER))
    w = 0.4
    fig, ax = plt.subplots(figsize=(12, 6))
    b1 = ax.bar([i - w / 2 for i in x], no, w, label="sin accidente", color="#2e7d32")
    b2 = ax.bar([i + w / 2 for i in x], ac, w, label="con accidente", color="#1565c0")
    ax.axhline(1.0, color="#b00020", ls="--", lw=1, label="speedup = 1 (sin ganancia)")
    ax.bar_label(b1, fmt="%.2f", fontsize=7, padding=2)
    ax.bar_label(b2, fmt="%.2f", fontsize=7, padding=2)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Speedup (x)")
    ax.set_title("Speedup por tipo de division - horizonte completo, TCP, 10k vehiculos\n"
                 "Mapa Almenara (baseline 1 instancia)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    p = OUT / "division_speedup_10k.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"[plot] {p}")


def plot_vehicles(data):
    fig, ax = plt.subplots(figsize=(10, 6))
    xs = [int(v) // 1000 for v in VEH]
    for d in DIV_ORDER:
        ys = [data[d]["no"].get(v) for v in VEH]
        if any(y is None for y in ys):
            continue
        ax.plot(xs, ys, marker="o", label=f"{d} ({WORKERS[d]}w)")
    ax.axhline(1.0, color="#b00020", ls="--", lw=1)
    ax.set_xlabel("Vehiculos (miles)")
    ax.set_ylabel("Speedup (x)")
    ax.set_xticks(xs)
    ax.set_title("Speedup vs numero de vehiculos por division - horizonte completo, TCP, sin accidente")
    ax.legend(fontsize=8, ncol=2)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = OUT / "division_speedup_vehicles.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"[plot] {p}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    data = load()
    plot_10k(data)
    plot_vehicles(data)


if __name__ == "__main__":
    main()
