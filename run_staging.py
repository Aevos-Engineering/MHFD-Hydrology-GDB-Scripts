"""Preview what the loader will write, without ArcGIS.

Example:
  python run_staging.py --inp model.inp --rpt BSO_Ex_100yr.rpt --rpt BSO_Ex_010yr.rpt \
      --cuhp BSO_CUHP_v1.xlsm --study-id 0 --out staging_out

A report can be tagged explicitly as PATH::Condition::ReturnPeriod
(for example "model.rpt::Existing::100") when its file name does not say.
A CUHP interface file can stand in for a report (for example
"Interface_100yr.txt::Existing"): SWMM is run on it first (--engine, --run-dir).
Writes CSV/GeoJSON tables shaped like the Hydrology GDB plus QA_report.txt.
"""

import argparse
import configparser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mhfd_hydro import qa, staging  # noqa: E402
from mhfd_hydro.cuhp import read_cuhp  # noqa: E402
from mhfd_hydro.swmm_inp import read_inp  # noqa: E402
from mhfd_hydro.swmm_rpt import read_rpt  # noqa: E402
from mhfd_hydro import swmm_run  # noqa: E402


def pcswmm_epsg(inp_path):
    chi = os.path.splitext(inp_path)[0] + ".chi"
    if not os.path.exists(chi):
        return None
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(chi, encoding="latin-1")
        return cp.get("General", "Map EPSG", fallback=None)
    except configparser.Error:
        return None


def parse_run(arg):
    parts = arg.split("::")
    path = parts[0]
    cond = parts[1] if len(parts) > 1 else None
    rp = parts[2] if len(parts) > 2 else None
    return staging.RunSpec.from_filename(path, cond, rp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inp", required=True)
    ap.add_argument("--rpt", action="append", required=True)
    ap.add_argument("--cuhp")
    ap.add_argument("--drf", help="CSV from MHFD 'Calculate Depth Reduction Factors'")
    ap.add_argument("--engine", help="runswmm.exe or swmm5.dll (default: find one)")
    ap.add_argument("--run-dir", help="folder for SWMM runs (default: the .inp folder)")
    ap.add_argument("--study-id", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    model = read_inp(a.inp)
    runs = []
    for r in a.rpt:
        parts = r.split("::")
        if parts[0].lower().endswith(".rpt"):
            runs.append(parse_run(r))
            continue
        spec = staging.RunSpec.from_filename(parts[0], *(parts[1:3] + [None] * 2)[:2])
        (rpt, cond, rp, area), = swmm_run.run_storms(
            a.inp, [(parts[0], spec.condition, spec.return_period, spec.correction_area_sq_mi)],
            a.run_dir or os.path.dirname(os.path.abspath(a.inp)), swmm_run.find_engine(a.engine))
        runs.append(staging.RunSpec(rpt, cond, rp, area))
    reports = {r.rpt_path: read_rpt(r.rpt_path) for r in runs}
    subs, info = read_cuhp(a.cuhp) if a.cuhp else (None, {})
    drf = staging.read_drf_csv(a.drf) if a.drf else None

    st = staging.build(model, runs, reports, a.study_id, subcatchments=subs, drf=drf)
    staging.write_outputs(st, a.out)
    mb, msrc = swmm_run.model_build(a.inp)
    findings = qa.run_all(model, runs, reports, st, subs, drf_supplied=bool(drf),
                          model_epsg=pcswmm_epsg(a.inp), model_build=mb, model_source=msrc)
    title = "run_staging.py (no ArcGIS)"
    with open(os.path.join(a.out, "QA_report.txt"), "w", encoding="utf-8") as fh:
        fh.write(qa.to_text(findings, os.path.basename(a.inp), title))

    print(f"{len(st.design_points)} design points, {len(st.routing_elements)} routing elements, "
          f"{len(st.peak_flows)} peak flow rows, {len(st.detention_summary)} detention rows, "
          f"{len(st.rating_curves)} rating curve rows")
    for m in st.messages:
        print("NOTE:", m)
    for f in findings:
        print(f"[{f.status}] {f.check}: {f.detail}")


if __name__ == "__main__":
    main()
