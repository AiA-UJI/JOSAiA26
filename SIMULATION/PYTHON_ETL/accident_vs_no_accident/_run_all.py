"""Run ratio comparisons with shared Y-axis limits across con/sin accidente."""
import subprocess, sys, os, json

base = os.path.dirname(os.path.abspath(__file__))
etl_dir = os.path.dirname(base)
script = os.path.join(etl_dir, "plot_compare_ratios.py")
ko = os.path.join(etl_dir, "..", "knowledge_output")

runs = [
    {
        "label": "10% vs 99% — CON accidente",
        "sim_a": os.path.join(ko, "20260415_231453_modified_4ways_all_10pct_accident"),
        "sim_b": os.path.join(ko, "20260415_232607_modified_4ways_all_99pct_accident"),
        "out":   os.path.join(base, "results_con_accidente"),
        "ylim_file": os.path.join(base, "_ylim_con.json"),
    },
    {
        "label": "10% vs 99% — SIN accidente",
        "sim_a": os.path.join(ko, "20260415_231210_modified_4ways_all_10pct"),
        "sim_b": os.path.join(ko, "20260415_231823_modified_4ways_all_99pct"),
        "out":   os.path.join(base, "results_sin_accidente"),
        "ylim_file": os.path.join(base, "_ylim_sin.json"),
    },
]

# ── Pass 1: generate plots and export Y limits ──
print("=== PASS 1: Computing Y limits ===")
for r in runs:
    print(f"\n  {r['label']}")
    rc = subprocess.call([
        sys.executable, script,
        r["sim_a"], r["sim_b"],
        "--output", r["out"],
        "--ylim-out", r["ylim_file"],
    ])
    if rc != 0:
        print(f"  [ERROR] Exit code {rc}")
        sys.exit(1)

# ── Merge: take max per key ──
print("\n=== Merging Y limits (max of both) ===")
all_ylims = []
for r in runs:
    with open(r["ylim_file"], "r", encoding="utf-8") as f:
        all_ylims.append(json.load(f))

merged = {}
all_keys = set()
for yl in all_ylims:
    all_keys.update(yl.keys())
for k in all_keys:
    merged[k] = max(yl.get(k, 0) for yl in all_ylims)

merged_file = os.path.join(base, "_ylim_merged.json")
with open(merged_file, "w", encoding="utf-8") as f:
    json.dump(merged, f)
print(f"  Merged {len(merged)} keys -> {merged_file}")

# ── Pass 2: regenerate with shared limits ──
print("\n=== PASS 2: Regenerating with shared Y limits ===")
for r in runs:
    print(f"\n  {r['label']}")
    rc = subprocess.call([
        sys.executable, script,
        r["sim_a"], r["sim_b"],
        "--output", r["out"],
        "--ylim-in", merged_file,
    ])
    if rc != 0:
        print(f"  [ERROR] Exit code {rc}")
    else:
        print("  [SUCCESS]")

# cleanup temp files
for r in runs:
    try:
        os.remove(r["ylim_file"])
    except OSError:
        pass
try:
    os.remove(merged_file)
except OSError:
    pass

print("\n[DONE] All comparisons finished with shared Y limits.")
