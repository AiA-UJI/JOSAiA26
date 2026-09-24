"""Empaqueta los datos de equivalencia y de speedup TCP de Rotterdam.

Genera dos ZIP en ``_export/`` en la raiz del proyecto:

* ``rotterdam_analisis.zip``  derivados (CSV de resumen y de detalle), metadatos
  de cada run, scripts de analisis y un ``manifest.json`` con la procedencia y
  las cifras clave. Pequeno, pensado para revisar el analisis.
* ``rotterdam_crudo.zip``     los ficheros de los que salen esos derivados:
  ``environment_traffic.json`` (conteo y velocidad por segmento cada 5 min) y
  ``trip_data.json`` (un registro por tramo de viaje) del baseline secuencial y
  de las seis particiones. Permite rehacer el analisis desde cero.

Se excluyen ``tripinfo.xml`` y los ``*.edgedata.xml`` por worker porque son
redundantes con los dos anteriores y multiplican por cuatro el tamano.

    python -m SIMULATION.distributed.lan._export_equiv_zip
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LAN = ROOT / "SIMULATION" / "distributed" / "lan"
EQ = LAN / "equivalence"
OUT = ROOT / "_export"

BASELINE_RUN = "20260710_163101"

CUT_DIRS = {
    "balanced-y-6": EQ / "20260710_163101" / "distributed",
    "balanced-x-6": EQ / "20260715_130537_balanced-x-6" / "distributed",
    "balanced-x-8": EQ / "20260715_111146_balanced-x-8" / "distributed",
    "balanced-y-8": EQ / "20260715_120508_balanced-y-8" / "distributed",
    "balanced-x-12": EQ / "20260715_095134_balanced-x-12" / "distributed",
    "balanced-y-12": EQ / "20260715_103435_balanced-y-12" / "distributed",
}

SCRIPTS = [
    "_equiv_segments.py",
    "_equiv_trips.py",
    "_equiv_handoff_speed.py",
    "build_rotterdam_tables.py",
    "compare_nexus_flows.py",
    "plot_nexus_edgedata.py",
    "plot_multicut_equivalence.py",
    "find_nexus_edges.py",
]

# Ficheros del repo que documentan la implementacion cuestionada.
SOURCES = [
    ROOT / "SIMULATION" / "distributed" / "sync_runtime.py",
    ROOT / "SIMULATION" / "distributed" / "balanced_partition.py",
    ROOT / "SIMULATION" / "distributed" / "trip_splitter.py",
    ROOT / "SIMULATION" / "distributed" / "transports" / "tcp.py",
    ROOT / "SIMULATION" / "distributed" / "transports" / "base.py",
]


def manifest() -> dict:
    return {
        "generado": datetime.now().isoformat(timespec="seconds"),
        "red": "rotterdam_arterial",
        "escenario": {
            "vehiculos": 20000,
            "accidente": False,
            "horizonte_pasos": 14400,
            "step_length_s": 1.0,
            "intervalo_edgedata_s": 300,
        },
        "baseline": {
            "run": BASELINE_RUN,
            "descripcion": "ejecucion secuencial en un solo nodo, sin particion",
            "viajes_completados": 17302,
            "distancia_total_km": 832274,
            "tiempo_conduccion_h": 9120,
            "velocidad_media_ponderada_kmh": 91.26,
            "espera_acumulada_h": 0.4,
            "wall_sec": 23595.5,
        },
        "cortes": sorted(CUT_DIRS),
        "ficheros_crudos": {
            "environment_traffic.json": (
                "diccionario edge -> intervalo -> lista de observaciones "
                "{speed (m/s), count (numero medio de vehiculos en el edge "
                "durante el intervalo), time (s)}. En el agregado distribuido "
                "las observaciones de un mismo intervalo provienen de workers "
                "distintos y son aportaciones disjuntas: hay que sumarlas, no "
                "promediarlas."
            ),
            "trip_data.json": (
                "lista de registros {id, depart, arrival, duration, "
                "routeLength, waitingTime, speed_kmh, vType}. En el "
                "distribuido cada vehiculo genera un registro por subred "
                "atravesada, asi que hay que reagrupar por id para "
                "reconstruir el viaje completo."
            ),
        },
        "derivados": {
            "segment_equivalence.csv": "resumen por corte sobre los 5621 segmentos",
            "segment_detail_<corte>.csv": "una fila por segmento: intensidad y velocidad en ambos lados",
            "trip_equivalence.csv": "conservacion de vehiculos, distancia, tiempo y velocidad",
            "multicut_conservacion.csv": "conservacion de conteos entered en aristas frontera",
            "multicut_equivalencia.csv": "conteos entered por segmento frontera y corte",
            "nexus_edgedata_summary.json": "correlacion de conteos reales en frontera (run y-6)",
            "nexus_flow_summary.json": "correlacion de flujo derivado en frontera (run y-6)",
            "rotterdam_tcp_full.csv": "84 ejecuciones TCP con speedup, barreras y hand-offs",
        },
        "hallazgos": {
            "conservado": {
                "handoffs_out_in": [62012, 62012],
                "handoff_failures": 0,
                "barrier_timeouts": 0,
                "ratio_distancia_total": [0.929, 1.011],
                "pearson_r_conteos_frontera": 0.9868,
                "error_mediano_conteos_frontera_pct": 5.6,
                "dif_mediana_velocidad_por_segmento_kmh": [0.98, 2.82],
                "pct_segmentos_velocidad_dentro_5kmh": [66.9, 89.5],
            },
            "no_conservado": {
                "velocidad_media_red_kmh": {
                    "baseline": 91.12,
                    "distribuido": [52.3, 71.32],
                },
                "ratio_tiempo_total": [1.214, 1.436],
                "espera_acumulada_h": {"baseline": 0.4, "distribuido": 21.2},
                "segmentos_atascados_y6": {
                    "n": 31,
                    "pct_vehiculo_tiempo_total": 27.7,
                    "criterio": "velocidad media < 1 m/s y ocupacion acumulada > 10",
                },
                "pearson_r_intensidad_y6": {
                    "toda_la_red": 0.5405,
                    "excluyendo_31_atascados": 0.9277,
                },
            },
            "firma_del_artefacto": {
                "descripcion": (
                    "velocidad mediana de cada tramo de viaje segun cuantos "
                    "traspasos lleva ya el vehiculo, corte balanced-y-6"
                ),
                "posicion_tramo": [1, 2, 3, 4, 5, 6, "7+"],
                "n": [18867, 16904, 14206, 11048, 7470, 4469, 2992],
                "mediana_kmh": [87.6, 87.6, 84.5, 79.8, 77.4, 73.0, 72.9],
                "media_kmh": [85.2, 82.0, 79.0, 76.6, 74.0, 70.6, 69.2],
                "baseline_sin_particion_mediana_kmh": 92.2,
                "traspasos_por_vehiculo_por_corte": {
                    "balanced-x-6": 3.74,
                    "balanced-y-6": 4.03,
                    "balanced-x-8": 4.49,
                    "balanced-y-8": 5.93,
                    "balanced-x-12": 6.37,
                    "balanced-y-12": 8.31,
                },
            },
        },
        "hipotesis_sin_verificar": [
            {
                "fichero": "SIMULATION/distributed/sync_runtime.py",
                "linea": 462,
                "codigo": "traci.vehicle.setSpeed(vid, speed)",
                "problema": (
                    "en SUMO setSpeed fija la velocidad hasta liberarla con "
                    "setSpeed(-1); esa liberacion no aparece en ningun punto "
                    "del repositorio"
                ),
            },
            {
                "fichero": "SIMULATION/distributed/sync_runtime.py",
                "linea": "436-460",
                "codigo": "vehicle.add(departPos='0') seguido de moveTo(lane_id, lane_pos)",
                "problema": (
                    "si moveTo falla la excepcion se descarta en silencio y el "
                    "vehiculo se queda al principio del carril"
                ),
            },
        ],
        "advertencia": (
            "los speedups de rotterdam_tcp_full.csv son validos porque miden "
            "tiempo de pared; lo que estos datos NO permiten afirmar es que la "
            "particion preserve las metricas de trafico"
        ),
    }


def add(z: zipfile.ZipFile, src: Path, arc: str) -> float:
    if not src.is_file():
        print(f"  [falta] {src}")
        return 0.0
    z.write(src, arc)
    return src.stat().st_size / 1e6


def build_analisis() -> Path:
    out = OUT / "rotterdam_analisis.zip"
    mb = 0.0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", json.dumps(manifest(), indent=2, ensure_ascii=False))

        for name in ("segment_equivalence.csv", "trip_equivalence.csv",
                     "multicut_conservacion.csv", "multicut_equivalencia.csv"):
            mb += add(z, EQ / name, f"derivados/{name}")
        for f in sorted(EQ.glob("segment_detail_*.csv")):
            mb += add(z, f, f"derivados/{f.name}")

        run = EQ / BASELINE_RUN
        for name in ("nexus_flow_summary.json", "nexus_edgedata_summary.json",
                     "nexus_flow_compare.csv", "nexus_edgedata_compare.csv",
                     "nexus_correlacion.csv", "meta.json"):
            mb += add(z, run / name, f"nexus_y6/{name}")
        mb += add(z, run / "distributed" / "aggregate_summary.json",
                  "nexus_y6/aggregate_summary.json")
        mb += add(z, LAN / "nexus" / "nexus_balanced-y-6_rotterdam_arterial.json",
                  "nexus_y6/nexus_edges.json")

        for name in ("rotterdam_tcp_full.csv", "rotterdam_speedups.csv",
                     "rotterdam_matriz.csv", "rotterdam_cobertura.csv"):
            mb += add(z, LAN / "results" / name, f"speedups/{name}")

        for name in SCRIPTS:
            mb += add(z, LAN / name, f"scripts/{name}")
        for p in SOURCES:
            mb += add(z, p, f"codigo/{p.name}")

        for cut, folder in CUT_DIRS.items():
            mb += add(z, folder.parent / "meta.json", f"runs/{cut}/meta.json")
            mb += add(z, folder / "aggregate_summary.json",
                      f"runs/{cut}/aggregate_summary.json")

    print(f"[zip] {out}  ({out.stat().st_size / 1e6:.1f} MB "
          f"comprimido, {mb:.0f} MB en crudo)")
    return out


def build_crudo() -> Path:
    out = OUT / "rotterdam_crudo.zip"
    mb = 0.0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("manifest.json", json.dumps(manifest(), indent=2, ensure_ascii=False))

        base = EQ / BASELINE_RUN / "baseline"
        for name in ("environment_traffic.json", "trip_data.json",
                     "simulation_info.json"):
            mb += add(z, base / name, f"baseline/{name}")

        for cut, folder in CUT_DIRS.items():
            for name in ("environment_traffic.json", "trip_data.json",
                         "aggregate_summary.json", "simulation_info.json"):
                mb += add(z, folder / name, f"{cut}/{name}")
            mb += add(z, folder.parent / "meta.json", f"{cut}/meta.json")
            print(f"  {cut}: acumulado {mb:.0f} MB")

    print(f"[zip] {out}  ({out.stat().st_size / 1e6:.1f} MB "
          f"comprimido, {mb:.0f} MB en crudo)")
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    build_analisis()
    build_crudo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
