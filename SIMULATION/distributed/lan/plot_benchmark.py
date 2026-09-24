"""Generate benchmark plots from the sweep CSV (speedups & wall times)."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _read(csv_path: Path) -> List[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bar(ax, labels, values, title, ylabel, color="#4363d8"):
    xs = range(len(labels))
    bars = ax.bar(xs, [v if v is not None else 0 for v in values], color=color)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.3)
    for b, v in zip(bars, values):
        if v is not None:
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.2f}",
                    ha="center", va="bottom", fontsize=7)


def plot_all(csv_path: Path, out_dir: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = _read(Path(csv_path))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dist = [r for r in rows if r["kind"] == "distributed"]

    # --- Stage T: transports ---
    t = [r for r in dist if r["stage"] == "T"]
    if t:
        t.sort(key=lambda r: r["transport"])
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 5))
        _bar(a1, [r["transport"] for r in t], [_f(r["speedup"]) for r in t],
             "Speedup by transport (balanced-x-2, 10k)", "speedup x", "#3cb44b")
        _bar(a2, [r["transport"] for r in t], [_f(r["barrier_total_sec"]) for r in t],
             "Barrier time by transport", "barrier total (s)", "#e6194B")
        fig.tight_layout(); fig.savefig(out_dir / "stage_T_transports.png", dpi=130)
        plt.close(fig)

    # --- Stage P: partitions ---
    p = [r for r in dist if r["stage"] == "P"]
    if p:
        p.sort(key=lambda r: (r["num_groups"], r["partition"]))
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(15, 5))
        _bar(a1, [r["partition"] for r in p], [_f(r["speedup"]) for r in p],
             "Speedup by partition (tcp, 10k)", "speedup x", "#4363d8")
        _bar(a2, [r["partition"] for r in p], [_f(r["runtime_sec"]) for r in p],
             "Wall time by partition", "runtime (s)", "#f58231")
        fig.tight_layout(); fig.savefig(out_dir / "stage_P_partitions.png", dpi=130)
        plt.close(fig)

    # --- Stage V/A: vehicle scaling (speedup vs vehicles) ---
    for stage, tag, color in (("V", "no accident", "#3cb44b"),
                              ("A", "accident", "#e6194B")):
        s = [r for r in dist if r["stage"] == stage and _f(r["speedup"])]
        if not s:
            continue
        s.sort(key=lambda r: int(r["vehicles"]))
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot([int(r["vehicles"]) for r in s], [_f(r["speedup"]) for r in s],
                "o-", color=color)
        ax.set_title(f"Speedup vs vehicles ({tag}, balanced-x-2 tcp)")
        ax.set_xlabel("vehicles"); ax.set_ylabel("speedup x")
        ax.grid(alpha=0.3)
        fig.tight_layout(); fig.savefig(out_dir / f"stage_{stage}_scaling.png", dpi=130)
        plt.close(fig)

    # --- Stage C: focused matrix (speedup vs vehicles, one line per
    #     partition x accident) ---
    c = [r for r in dist if r["stage"] == "C" and _f(r["speedup"])]
    if c:
        import itertools
        keyf = lambda r: (r["partition"], r["accident"])
        c.sort(key=lambda r: (r["partition"], r["accident"], int(r["vehicles"])))
        fig, ax = plt.subplots(figsize=(9, 6))
        for (part, acc), grp in itertools.groupby(c, key=keyf):
            grp = list(grp)
            style = "--" if acc == "True" else "-"
            ax.plot([int(r["vehicles"]) for r in grp],
                    [_f(r["speedup"]) for r in grp], "o" + style,
                    label=f"{part} {'acc' if acc=='True' else 'noacc'}")
        ax.axhline(1.0, color="#888", lw=1, ls=":")
        ax.set_title("Speedup vs vehicles (focused: best partitions)")
        ax.set_xlabel("vehicles"); ax.set_ylabel("speedup x")
        ax.grid(alpha=0.3); ax.legend(fontsize=8)
        fig.tight_layout(); fig.savefig(out_dir / "stage_C_focused.png", dpi=130)
        plt.close(fig)

    # --- Overview: baseline vs distributed runtime per scenario ---
    base = {(r["vehicles"], r["accident"]): _f(r["runtime_sec"])
            for r in rows if r["kind"] == "baseline"}
    sv = [r for r in dist if r["stage"] in ("V", "A", "C") and _f(r["runtime_sec"])]
    if sv and base:
        sv.sort(key=lambda r: (r["accident"], int(r["vehicles"])))
        labels = [f"{r['vehicles']}/{'acc' if r['accident']=='True' else 'no'}"
                  for r in sv]
        fig, ax = plt.subplots(figsize=(12, 5))
        xs = range(len(sv))
        ax.bar([x - 0.2 for x in xs],
               [base.get((r["vehicles"], r["accident"])) or 0 for r in sv],
               width=0.4, label="baseline (1 inst.)", color="#999999")
        ax.bar([x + 0.2 for x in xs], [_f(r["runtime_sec"]) for r in sv],
               width=0.4, label="distributed", color="#4363d8")
        ax.set_xticks(list(xs)); ax.set_xticklabels(labels, rotation=30, ha="right",
                                                    fontsize=8)
        ax.set_ylabel("runtime (s)"); ax.set_title("Baseline vs distributed wall time")
        ax.legend(); ax.grid(axis="y", alpha=0.3)
        fig.tight_layout(); fig.savefig(out_dir / "overview_runtime.png", dpi=130)
        plt.close(fig)

    print(f"[plot] benchmark plots -> {out_dir}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    csvp = Path(a.csv)
    plot_all(csvp, Path(a.out) if a.out else csvp.parent / "plots")
