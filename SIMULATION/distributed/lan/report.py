"""Build a single self-contained HTML presentation from ALL sweep results.

Scans every ``lan/results/<ts>/`` directory, merges their ``benchmark.csv``,
and embeds every benchmark plot and partition map (as base64) into one HTML
file with:

* executive summary (best speedups, key findings)
* partition maps gallery (the network "cuts")
* transport/protocol comparison (times + speedups)
* partition comparison
* vehicle scaling (no accident vs accident)
* baseline vs distributed wall-time
* full tables of every run

Usage::

    python -m SIMULATION.distributed.lan.report           # scans all results
    python -m SIMULATION.distributed.lan.report --out X.html
"""

from __future__ import annotations

import base64
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_ROOT = PROJECT_ROOT / "SIMULATION" / "distributed" / "lan" / "results"


def _img_b64(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _read_csv(path: Path) -> List[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v, nd=2):
    x = _f(v)
    return f"{x:.{nd}f}" if x is not None else "-"


def _collect() -> Dict:
    runs = []
    if RESULTS_ROOT.is_dir():
        for d in sorted(RESULTS_ROOT.iterdir()):
            csvp = d / "benchmark.csv"
            if not d.is_dir() or not csvp.is_file():
                continue
            rows = _read_csv(csvp)
            for r in rows:
                r["_run"] = d.name
            runs.append({
                "name": d.name,
                "dir": d,
                "rows": rows,
                "plots": sorted((d / "plots").glob("*.png")) if (d / "plots").is_dir() else [],
                "partitions": sorted((d / "partitions").glob("*.png")) if (d / "partitions").is_dir() else [],
            })
    return {"runs": runs}


def _img_tag(path: Path, width="100%") -> str:
    b = _img_b64(path)
    if not b:
        return f"<p><i>missing {path.name}</i></p>"
    return f'<img src="{b}" style="max-width:{width};border:1px solid #ddd;border-radius:6px;margin:6px 0;">'


def _table(rows: List[dict], cols: List[str], headers: List[str]) -> str:
    th = "".join(f"<th>{h}</th>" for h in headers)
    trs = []
    for r in rows:
        tds = "".join(f"<td>{r.get(c, '')}</td>" for c in cols)
        trs.append(f"<tr>{tds}</tr>")
    return (f'<table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>')


def build(out_html: Path) -> Path:
    data = _collect()
    runs = data["runs"]
    all_rows = [r for run in runs for r in run["rows"]]
    dist = [r for r in all_rows if r.get("kind") == "distributed"]
    base = [r for r in all_rows if r.get("kind") == "baseline"]

    # best speedups
    ok = [r for r in dist if _f(r.get("speedup"))]
    ok.sort(key=lambda r: _f(r["speedup"]), reverse=True)
    top = ok[:8]

    parts = []
    parts.append(f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Simulación distribuida SUMO - Presentación de resultados</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
   background:#f6f7f9;color:#1a1a2e;line-height:1.5;}}
 header{{background:linear-gradient(135deg,#16213e,#0f3460);color:#fff;
   padding:36px 48px;}}
 header h1{{margin:0 0 6px;font-size:28px;}}
 header p{{margin:0;opacity:.85;}}
 main{{max-width:1180px;margin:0 auto;padding:24px 32px 80px;}}
 section{{background:#fff;border-radius:10px;padding:24px 28px;margin:22px 0;
   box-shadow:0 1px 4px rgba(0,0,0,.07);}}
 h2{{border-left:5px solid #0f3460;padding-left:12px;color:#0f3460;}}
 table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0;}}
 th,td{{border:1px solid #e1e4e8;padding:6px 9px;text-align:center;}}
 th{{background:#0f3460;color:#fff;}}
 tr:nth-child(even){{background:#f3f5f8;}}
 .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;}}
 .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;}}
 .card{{background:#fafbfc;border:1px solid #e1e4e8;border-radius:8px;padding:10px;}}
 .card h4{{margin:4px 0 8px;font-size:13px;color:#0f3460;text-align:center;}}
 .kpi{{display:inline-block;background:#0f3460;color:#fff;border-radius:8px;
   padding:10px 16px;margin:6px;font-size:14px;}}
 .kpi b{{font-size:20px;display:block;}}
 code{{background:#eef;padding:1px 5px;border-radius:4px;}}
 .muted{{color:#666;font-size:12px;}}
</style></head><body>
<header>
 <h1>Simulación distribuida SUMO — Presentación de resultados</h1>
 <p>Mapa Almenara (modified_4ways_all) · clúster labrob07-12 · generado {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</header><main>""")

    # ---- Executive summary ----
    best = top[0] if top else None
    parts.append('<section><h2>Resumen ejecutivo</h2>')
    parts.append(f'<p>{len(all_rows)} ejecuciones en total '
                 f'({len(base)} baselines + {len(dist)} distribuidas) '
                 f'sobre {len(runs)} barridos.</p>')
    if best:
        parts.append(
            f'<div><span class="kpi"><b>{_fmt(best["speedup"])}×</b>mejor speedup</span>'
            f'<span class="kpi"><b>{best.get("partition","")}</b>{best.get("num_groups","")} workers</span>'
            f'<span class="kpi"><b>{best.get("transport","")}</b>transporte</span>'
            f'<span class="kpi"><b>{best.get("vehicles","")}</b>vehículos</span></div>')
    parts.append('<h4>Top configuraciones por speedup</h4>')
    parts.append(_table(
        top, ["_run", "partition", "num_groups", "transport", "vehicles",
              "accident", "runtime_sec", "speedup"],
        ["barrido", "partición", "workers", "transp.", "veh.", "acc.",
         "runtime(s)", "speedup"]))
    parts.append('</section>')

    # ---- Partition maps gallery ----
    pmaps = []
    for run in runs:
        pmaps.extend(run["partitions"])
    # de-dup by filename (keep first)
    seen = set(); uniq = []
    for p in pmaps:
        if p.name in seen:
            continue
        seen.add(p.name); uniq.append(p)
    if uniq:
        parts.append('<section><h2>Mapas de partición (los "cortes" de la red)</h2>'
                     '<p class="muted">Cada color es el grupo (worker) dueño de esas '
                     'aristas. Los vehículos circulan cruzando fronteras entre workers.</p>'
                     '<div class="grid3">')
        for p in uniq:
            parts.append(f'<div class="card"><h4>{p.stem.replace("partition_","")}</h4>'
                         f'{_img_tag(p)}</div>')
        parts.append('</div></section>')

    # ---- All benchmark plots ----
    for run in runs:
        if not run["plots"]:
            continue
        parts.append(f'<section><h2>Gráficas — barrido {run["name"]}</h2>'
                     '<div class="grid">')
        for p in run["plots"]:
            parts.append(f'<div class="card"><h4>{p.stem}</h4>{_img_tag(p)}</div>')
        parts.append('</div></section>')

    # ---- Transport comparison table ----
    tx = [r for r in dist if r.get("stage") == "T"]
    if tx:
        tx.sort(key=lambda r: r.get("transport", ""))
        parts.append('<section><h2>Comparación de protocolos de comunicación</h2>'
                     '<p class="muted">balanced-x-2, 10k vehículos. Se midió tiempo de '
                     'barrera y speedup por transporte.</p>')
        parts.append(_table(
            tx, ["transport", "runtime_sec", "speedup", "barrier_total_sec",
                 "handoffs_out", "barriers"],
            ["transporte", "runtime(s)", "speedup", "t.barrera(s)",
             "handoffs", "barreras"]))
        parts.append('</section>')

    # ---- Full results table ----
    parts.append('<section><h2>Tabla completa de resultados</h2>')
    alld = sorted(dist, key=lambda r: (r.get("_run", ""), r.get("stage", ""),
                                       r.get("partition", ""),
                                       int(r.get("vehicles") or 0)))
    parts.append(_table(
        alld, ["_run", "stage", "partition", "num_groups", "transport",
               "vehicles", "accident", "status", "runtime_sec", "speedup"],
        ["barrido", "etapa", "partición", "workers", "transp.", "veh.",
         "acc.", "estado", "runtime(s)", "speedup"]))
    parts.append('</section>')

    # ---- Baselines table ----
    if base:
        parts.append('<section><h2>Baselines (1 instancia) — denominadores</h2>')
        base.sort(key=lambda r: (int(r.get("vehicles") or 0), r.get("accident", "")))
        parts.append(_table(
            base, ["_run", "vehicles", "accident", "runtime_sec", "wall_sec"],
            ["barrido", "veh.", "acc.", "runtime(s)", "wall(s)"]))
        parts.append('</section>')

    parts.append('</main></body></html>')

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text("".join(parts), encoding="utf-8")
    print(f"[report] -> {out_html}  ({out_html.stat().st_size/1024:.0f} KB, "
          f"{len(all_rows)} runs)")
    return out_html


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = Path(a.out) if a.out else (RESULTS_ROOT /
          f"PRESENTACION_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    build(out)
