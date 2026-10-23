"""QA checks for MHFD 03 Hydrology deliverables.

Each check returns Finding objects. Thresholds come from MHFD guidance where
MHFD states one (cited in `source`); others are flagged as judgment calls.
"""

import os
from collections import defaultdict
from dataclasses import dataclass, field

from . import constants as C

PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"


@dataclass
class Finding:
    check: str
    status: str
    detail: str
    source: str = ""
    items: list = field(default_factory=list)


def _f(check, status, detail, source="", items=None):
    return Finding(check, status, detail, source, list(items or []))


# ---------------------------------------------------------------------------

def check_georeference(model, model_epsg=None):
    xs = [n.x for n in model.nodes.values() if n.x is not None]
    ys = [n.y for n in model.nodes.values() if n.y is not None]
    out = []
    missing = [n.name for n in model.nodes.values() if n.x is None]
    if missing:
        out.append(_f("SWMM georeferenced", FAIL, f"{len(missing)} nodes have no coordinates.",
                      "Hydrology guidance / workshop #3", missing))
        return out
    # NAD83(2011) CO Central (US ft) covers roughly X 2.9M-3.5M, Y 1.0M-2.0M.
    in_range = 2.5e6 < min(xs) and max(xs) < 3.8e6 and 0.9e6 < min(ys) and max(ys) < 2.1e6
    epsg_note = f" PCSWMM project EPSG {model_epsg}." if model_epsg else ""
    if not in_range:
        out.append(_f("SWMM georeferenced", FAIL,
                      f"Coordinates X {min(xs):,.0f}-{max(xs):,.0f}, Y {min(ys):,.0f}-{max(ys):,.0f} "
                      "are not in Colorado Central StatePlane feet." + epsg_note,
                      "Workshop #3: georeference in StatePlane Central"))
    elif model_epsg and str(model_epsg) != str(C.DEFAULT_WKID):
        out.append(_f("SWMM georeferenced", WARN,
                      f"Model is in EPSG {model_epsg}; the GDB uses {C.DEFAULT_WKID}. "
                      "The loader will project geometry.", "Data dictionary: WKID 6428"))
    else:
        out.append(_f("SWMM georeferenced", PASS,
                      f"Coordinates fall in Colorado Central StatePlane feet.{epsg_note}"))
    return out


ORPHAN_CHECK = "Topology: Orphan nodes"


def check_connectivity(model):
    linked = set()
    for lk in model.links.values():
        linked.add(lk.from_node.lower())
        linked.add(lk.to_node.lower())
    orphans = [n.name for n in model.nodes.values() if n.name.lower() not in linked]
    if orphans:
        return [_f(ORPHAN_CHECK, WARN, f"{len(orphans)} orphan nodes (SWMM nodes with no conduit "
                   "or other link).", "", orphans)]
    return [_f(ORPHAN_CHECK, PASS, "No orphan nodes (every SWMM node is connected to a conduit "
               "or other link).")]


def check_reports(runs, reports):
    out = []
    for run in runs:
        rpt = reports[run.rpt_path]
        label = f"{run.condition} {run.return_period}-yr" + (
            f" ({run.correction_area_sq_mi:g} sq mi DRF run)" if run.correction_area_sq_mi else "")
        if rpt.errors:
            out.append(_f(f"SWMM run {label}", FAIL, f"{len(rpt.errors)} errors.", "", rpt.errors))
            continue
        e = rpt.routing_continuity_pct
        issues = []
        status = PASS
        if e is None:
            status, issues = WARN, ["No flow routing continuity reported"]
        elif abs(e) > 5:
            status, issues = FAIL, [f"routing continuity error {e:+.2f}%"]
        elif abs(e) > 1:
            status, issues = WARN, [f"routing continuity error {e:+.2f}%"]
        if rpt.instability:
            status = WARN if status == PASS else status
            issues.append("unstable links: " + ", ".join(f"{l} ({i})" for l, i in rpt.instability))
        if rpt.flooding:
            status = WARN if status == PASS else status
            issues.append(f"{len(rpt.flooding)} flooded nodes")
        detail = (f"SWMM {rpt.version}; continuity {e:+.3f}%; {len(rpt.warnings)} warnings"
                  if e is not None else f"{len(rpt.warnings)} warnings")
        if issues:
            detail += "; " + "; ".join(issues)
        out.append(_f(f"SWMM run {label}", status, detail,
                      "CUHP-SWMM review handout (continuity, warnings, flooding, instability). "
                      "1%/5% continuity thresholds are a judgment call",
                      rpt.warnings + list(rpt.flooding)))
    return out


def check_runs_cover_scope(runs, required=("2", "5", "10", "25", "50", "100", "500")):
    by_cond = defaultdict(set)
    for r in runs:
        by_cond[r.condition].add(r.return_period)
    out = []
    for base in ("Existing", "Future"):
        have = by_cond.get(base, set())
        if not have:
            out.append(_f(f"Storms modeled: {base}", WARN, f"No {base} condition runs supplied.",
                          "Scope template 03: Existing and Future are base level"))
            continue
        miss = [rp for rp in required if rp not in have]
        status = WARN if miss else PASS
        out.append(_f(f"Storms modeled: {base}", status,
                      f"Have {', '.join(sorted(have, key=_rp_sort))}-yr"
                      + (f"; missing {', '.join(miss)}-yr" if miss else ""),
                      "Scope template 03: 1, 2, 5, 10, 25, 50, 100, (1% Plus), 500-yr"))
    for cond in by_cond:
        if cond not in ("Existing", "Future"):
            out.append(_f(f"Storms modeled: {cond}", INFO,
                          f"Have {', '.join(sorted(by_cond[cond], key=_rp_sort))}-yr"))
    return out


def _rp_sort(rp):
    return {"WQ": 0, "1%+": 150}.get(rp, None) or float(rp)


def check_cuhp(model, subcatchments):
    out = []
    names = {n.lower(): n for n in model.nodes}
    exact_missing, case_only = [], []
    for sc in subcatchments:
        t = sc.target_node or ""
        if t in model.nodes:
            continue
        if t.lower() in names:
            case_only.append(f"{sc.name}: '{t}' vs SWMM '{names[t.lower()]}'")
        else:
            exact_missing.append(f"{sc.name} -> {t}")
    if exact_missing:
        out.append(_f("CUHP target nodes in SWMM", FAIL,
                      f"{len(exact_missing)} subcatchments point at nodes that are not in the SWMM model.",
                      "Workshop #3: CUHP 'Check SWMM Nodes'", exact_missing))
    else:
        out.append(_f("CUHP target nodes in SWMM", PASS,
                      f"All {len(subcatchments)} subcatchment target nodes exist in SWMM."))
    if case_only:
        out.append(_f("CUHP target node spelling", WARN,
                      f"{len(case_only)} target nodes differ from SWMM only by upper/lower case. "
                      "SWMM accepts this, but exact-match tools (including MHFD's DRF tool) will miss them.",
                      "", case_only))

    acres = [sc.area_ac for sc in subcatchments if sc.area_ac]
    if acres:
        avg, big = sum(acres) / len(acres), [f"{sc.name}: {sc.area_ac:.1f} ac"
                                            for sc in subcatchments if (sc.area_ac or 0) > 130]
        # Over 130 ac is listed for information only (Jeremy: not a requirement).
        status = WARN if avg > 100 else PASS
        out.append(_f("Subcatchment size", status,
                      f"{len(acres)} subcatchments, average {avg:.1f} ac, largest {max(acres):.1f} ac"
                      + (f"; {len(big)} over 130 ac (for information, not a fail)" if big else ""),
                      "Scope template 03 suggests an average <= 100 ac", big))

    return out


def check_drf(staging, runs, drf_supplied):
    big = [p for p in staging.peak_flows if (p.get("contributing_drainage_area_sq_mi") or 0) >= 2]
    corrected = any(r.correction_area_sq_mi for r in runs)
    if not big:
        return [_f("Depth reduction factors", PASS, "No design point drains 2 sq mi or more; DRFs not needed.",
                   "Supplemental spreadsheet: DRF not applied under 2 sq mi")]
    nodes = sorted({p["swmm_node_name"] for p in big})
    if corrected or drf_supplied:
        return [_f("Depth reduction factors", PASS,
                   f"{len(nodes)} design points drain 2 sq mi or more; DRF runs/table supplied.")]
    return [_f("Depth reduction factors", WARN,
               f"{len(nodes)} design points drain 2 sq mi or more, but no area-corrected (DRF) runs "
               "or DRF table were supplied. Confirm DRFs were considered.",
               "USDCM Rainfall chapter; MHFD submittal #5 (DRFs)", nodes)]


def check_results_populated(runs, staging):
    """Every run actually put a peak flow on the design points."""
    out = []
    for r in runs:
        cid = C.CONDITION_IDS.get(r.condition)
        fld = C.PEAK_FLOW_FIELD.get(r.return_period)
        rows = [p for p in staging.peak_flows if p["hydro_modeled_condition_id"] == cid]
        if not rows or not fld:
            continue
        vals = [p.get(fld) for p in rows]
        missing = [p["swmm_node_name"] for p, v in zip(rows, vals) if v is None]
        zero_with_area = [p["swmm_node_name"] for p, v in zip(rows, vals)
                          if v == 0 and (p.get("contributing_drainage_area_sq_mi") or 0) > 0]
        filled = [v for v in vals if v]
        label = f"Results populated: {r.condition} {r.return_period}-yr"
        if not filled:
            out.append(_f(label, FAIL, f"No peak flows above zero at any of {len(rows)} design points. "
                          f"Check that {os.path.basename(r.rpt_path)} is a finished SWMM report (.rpt).",
                          "", missing))
        elif missing or zero_with_area:
            items = [f"{n}: not in report" for n in missing] + \
                    [f"{n}: 0 cfs with drainage area" for n in zero_with_area]
            out.append(_f(label, FAIL if missing else WARN,
                          f"{len(filled)} of {len(rows)} design points have a peak; "
                          f"{len(missing)} missing from the report, {len(zero_with_area)} are 0 cfs "
                          "though area drains to them.", "", items))
        else:
            out.append(_f(label, PASS, f"Peak flows at all {len(rows)} design points "
                          f"(max {max(filled):.1f} cfs)."))
    return out


def check_flows_increase(staging, runs=None, tol_cfs=0.5, tol_pct=0.01):
    """A larger storm should not give a smaller peak.

    Missing or zero peaks fail (nothing to compare). Drops within
    max(0.5 cfs, 1%) are listed separately as minor: they are usually flow
    caps at dividers or full pipes, not data problems.
    """
    order = ["1", "2", "5", "10", "25", "50", "100", "500"]
    supplied = {}
    for r in runs or []:
        supplied.setdefault(C.CONDITION_IDS.get(r.condition), set()).add(r.return_period)
    if runs is not None and all(len([rp for rp in v if rp in order]) < 2 for v in supplied.values()):
        return [_f("Peak flows rise with return period", INFO,
                   "Needs at least two storms for one condition.")]
    bad, empty, minor = [], [], []
    for p in staging.peak_flows:
        cid = p["hydro_modeled_condition_id"]
        storms = [rp for rp in order if rp in supplied.get(cid, ())] if runs is not None else \
            [rp for rp in order if p.get(C.PEAK_FLOW_FIELD[rp]) is not None]
        vals = [(rp, p.get(C.PEAK_FLOW_FIELD[rp])) for rp in storms]
        cond = next((k for k, v in C.CONDITION_IDS.items() if v == cid), cid)
        name = f"{p['swmm_node_name']} ({cond})"
        if any(q is None for _, q in vals):
            empty.append(f"{name}: no peak for " + ", ".join(f"{rp}-yr" for rp, q in vals if q is None))
            continue
        if vals and all(q == 0 for _, q in vals) and (p.get("contributing_drainage_area_sq_mi") or 0) > 0:
            empty.append(f"{name}: 0 cfs in every storm though area drains to it")
            continue
        for (r0, q0), (r1, q1) in zip(vals, vals[1:]):
            if q1 < q0:
                msg = f"{name}: {r1}-yr {q1} < {r0}-yr {q0}"
                (bad if q0 - q1 > max(tol_cfs, tol_pct * q0) else minor).append(msg)
                break
    problems = empty + bad
    if problems:
        detail = []
        if empty:
            detail.append(f"{len(empty)} design points have missing or all-zero peaks")
        if bad:
            detail.append(f"{len(bad)} where a larger storm gives a clearly smaller peak "
                          "(check that every storm was run from the same CUHP version)")
        out = [_f("Peak flows rise with return period", FAIL, "; ".join(detail) + ".", "", problems)]
    else:
        out = [_f("Peak flows rise with return period", PASS,
                  "Peak flows increase with return period at every design point.")]
    if minor:
        out.append(_f("Peak flows: small decreases", INFO,
                      f"{len(minor)} design points drop by less than 0.5 cfs or 1% between storms "
                      "(typically flow caps at dividers or full pipes).", "", minor))
    return out


def check_area_balance(staging, model, subcatchments):
    """All CUHP area reaches an outfall. Contributing area is the full upstream
    area, so where flow splits both branches carry it and outfall areas can
    add up to more than the CUHP total; only a shortfall is a problem."""
    total = sum(sc.area_sq_mi or 0 for sc in subcatchments)
    outfalls = {n.name for n in model.nodes.values() if n.kind == "OUTFALL"}
    reaching = sum(p.get("contributing_drainage_area_sq_mi") or 0 for p in staging.peak_flows
                   if p["swmm_node_name"] in outfalls and p["hydro_modeled_condition_id"] ==
                   min(q["hydro_modeled_condition_id"] for q in staging.peak_flows))
    lost = sum(sc.area_sq_mi or 0 for sc in subcatchments
               if not sc.target_node or model.node(sc.target_node) is None)
    if lost > 0.0005:
        return [_f("Drainage area balance", WARN,
                   f"CUHP total {total:.3f} sq mi; {lost:.3f} sq mi drains to target nodes missing "
                   "from SWMM and is not counted at any design point.")]
    extra = reaching - total
    return [_f("Drainage area balance", PASS,
               f"All {total:.3f} sq mi of CUHP area reaches the SWMM network"
               + (f"; outfalls total {reaching:.3f} sq mi because split flows carry the full "
                  "upstream area on each branch." if extra > 0.0005 else "."))]


def check_hour_rainfall(rainfall):
    """rainfall: {"point": bool, "depths": [...], "loaded": bool} from the load tool."""
    basis = "hydro_hour_rainfall needs the 1-hr/6-hr depths and the NOAA Atlas 14 point"
    if not rainfall.get("depths"):
        return [_f("Hour rainfall", WARN, "No 1-hr/6-hr depths found (give the CUHP workbook); "
                   "Hour Rainfall table not loaded.", basis)]
    if not rainfall.get("point"):
        return [_f("Hour rainfall", FAIL, f"Loaded {len(rainfall['depths'])} return periods of "
                   "1-hr/6-hr depths, but there is no NOAA Atlas 14 point, so the rows aren't "
                   "linked to hydro_point_precipitation.", basis)]
    return [_f("Hour rainfall", PASS, f"Loaded {len(rainfall['depths'])} return periods of 1-hr/6-hr "
               "depths from the CUHP storm sheets, linked to the NOAA Atlas 14 point.", basis)]


def subcatchment_load_findings(gdb_rows, subcatchments, tol=0.01):
    """Tool B's load, checked from the GDB: every CUHP subcatchment has a polygon,
    polygon areas match CUHP within 1%, and imperviousness is filled for both
    conditions. gdb_rows: [(name, acres, existing %, future %)]."""
    if not subcatchments:
        return []
    if not gdb_rows:
        return [_f("Subcatchment polygons loaded", FAIL, "No subcatchment polygons in the GDB for "
                   "this study. Run tool B before C.", "")]
    have = {str(r[0]).lower(): r[1] for r in gdb_rows if r[0]}
    missing = [sc.name for sc in subcatchments if sc.name.lower() not in have]
    off = []
    for sc in subcatchments:
        a = have.get(sc.name.lower())
        if a is not None and sc.area_ac and abs(a - sc.area_ac) / sc.area_ac > tol:
            off.append(f"{sc.name}: polygon {a:.2f} ac, CUHP {sc.area_ac:.2f} ac "
                       f"({(a - sc.area_ac) / sc.area_ac:+.0%})")
    out = [_f("Subcatchment polygons loaded", WARN if missing else PASS,
              f"{len(missing)} of {len(subcatchments)} CUHP subcatchments have no polygon in the GDB."
              if missing else f"All {len(subcatchments)} CUHP subcatchments have a polygon.",
              "", missing),
           _f("Subcatchment area vs CUHP", WARN if off else PASS,
              f"{len(off)} polygons differ from the CUHP area by more than 1%." if off
              else "Every polygon is within 1% of its CUHP area.", "", off)]
    imp = []
    for i, label in ((2, "Existing"), (3, "Future")):
        vals = [(r[0], (list(r) + [None, None])[i]) for r in gdb_rows]
        if all(not v for _, v in vals):  # the whole condition is missing (a real 0 is per basin)
            imp.append(f"{label} imperviousness is 0 or blank for all {len(vals)} subcatchments")
        else:
            imp += [f"{n}: {label} imperviousness is blank" for n, v in vals if v is None]
    out.append(_f("Subcatchment imperviousness", WARN if imp else PASS,
                  "; ".join(imp[:2]) + ("; ..." if len(imp) > 2 else "") + "." if imp
                  else "Existing and Future imperviousness are filled for every subcatchment.",
                  "", imp))
    return out


def vertical_datum_finding(datum):
    if not datum:
        return _f("Vertical datum", WARN, "No vertical datum given; vertical_datum is blank in "
                  "hydro_detention_rating_curve.", "")
    return _f("Vertical datum", PASS, f"Rating curve elevations recorded as {datum}.", "")


def related_records_finding(links, ids_filled=None):
    """links: [(link, linked, total, problem)] from the load tool's relationship check."""
    bad = [f"{link}: {problem}" if problem else f"{link}: {total - linked} of {total} rows don't "
           "link to a parent" for link, linked, total, problem in links
           if problem or linked < total]
    note = ""
    if ids_filled:
        note = (" The tool filled blank ids the template's attribute rules left empty: "
                + ", ".join(f"{t} ({n})" for t, n in sorted(ids_filled.items())) + ".")
    if bad:
        return _f("Related records", FAIL, f"{len(bad)} relationship classes have rows that don't "
                  "link to their parent feature." + note, "", bad)
    return _f("Related records", PASS, f"All rows link to their parent feature across "
              f"{len(links)} relationship classes." + note, "",
              [f"{link}: {linked} rows" for link, linked, _, _ in links])


def check_future_vs_existing(staging, threshold=0.30):
    ex = {p["swmm_node_name"]: p for p in staging.peak_flows if p["hydro_modeled_condition_id"] == 1}
    fu = {p["swmm_node_name"]: p for p in staging.peak_flows if p["hydro_modeled_condition_id"] == 2}
    if not ex or not fu:
        return [_f("Future vs existing 100-yr", INFO, "Needs both Existing and Future runs.",
                   "Hydrology guidance: note > 30% increase")]
    flagged = []
    for name, e in ex.items():
        q0, q1 = e.get("flow_100yr_cfs"), fu.get(name, {}).get("flow_100yr_cfs")
        if q0 and q1 is not None and (q1 - q0) / q0 > threshold:
            flagged.append(f"{name}: {q0:.0f} -> {q1:.0f} cfs (+{(q1 - q0) / q0:.0%})")
    return [_f("Future vs existing 100-yr", WARN if flagged else PASS,
               f"{len(flagged)} design points increase more than 30%." if flagged
               else "No design point increases more than 30%.",
               "Hydrology guidance: note when future exceeds existing by more than 30%", flagged)]


def check_detention(staging, model):
    no_q = sorted({r["node_name"] for r in staging.rating_curves if r["stage_discharge_cfs"] is None})
    partial = sorted({r["node_name"] for r in staging.rating_curves
                      if r.get("comments") and r["node_name"] not in no_q})
    storages = [n.name for n in model.nodes.values() if n.kind == "STORAGE"]
    with_curve = {r["node_name"] for r in staging.rating_curves}
    out = [_f("Detention rating curves", INFO,
              f"{len(with_curve)} of {len(storages)} storage nodes produced stage-area-volume rows.")]
    if no_q or partial:
        out.append(_f("Detention discharge", WARN,
                      "; ".join(x for x in (
                          f"{len(no_q)} ponds have no tabular outlet, so their discharge is blank"
                          if no_q else "",
                          f"{len(partial)} ponds have weirs/orifices not included in the discharge"
                          if partial else "") if x) + ".",
                      "Discharge comes from TABULAR outlet curves only", no_q + partial))
    return out


def check_swmm_build(runs, reports, model_build=None, model_source=None):
    """Every run on one SWMM build, and the same build the model was made with."""
    builds = {}
    for r in runs:
        b = getattr(reports[r.rpt_path], "build", None)
        builds[f"{r.condition} {r.return_period}-yr"] = b
    known = {b for b in builds.values() if b}
    items = [f"{k}: SWMM {v or 'unknown'}" for k, v in builds.items()]
    src = f" ({os.path.basename(model_source)})" if model_source else ""
    if not known:
        return [_f("SWMM engine build", INFO, "Reports don't state the SWMM build.", BUILD_BASIS, items)]
    if len({tuple(int(x) for x in b.split(".")) for b in known}) > 1:
        return [_f("SWMM engine build", FAIL, "Runs were made with different SWMM builds: "
                   + ", ".join(sorted(known)) + ". Peak flows are not comparable.", BUILD_BASIS, items)]
    run_b = known.pop()
    if model_build and [int(x) for x in model_build.split(".")] != [int(x) for x in run_b.split(".")]:
        return [_f("SWMM engine build", FAIL, f"Runs used SWMM {run_b}; the model was made with "
                   f"{model_build}{src}. Peak flows may not match the model of record.", BUILD_BASIS, items)]
    detail = f"All runs used SWMM {run_b}" + (f", same as the model{src}." if model_build else
                                              "; the model's own build is unknown.")
    return [_f("SWMM engine build", PASS if model_build else INFO, detail, BUILD_BASIS, items)]


def filled_fields_finding(filled):
    """QA line for blanks the loader filled in fields that can't be blank."""
    if not filled:
        return _f("Blank required fields", PASS, "No blank values needed filling.")
    return _f("Blank required fields", WARN,
              f"{len(filled)} fields had no value and were filled with a default.", "",
              [f"{k} = {v!r}" for k, v in sorted(filled.items())])


def topology_findings(errors, checks, orphans=None):
    """QA lines from the GDB topology check: errors are (check, detail, x, y).
    orphans is the SWMM orphan node finding, folded into the orphan node line."""
    out = []
    for check in checks:
        name, noun = TOPOLOGY_NAMES.get(check, (check, check.lower()))
        items = [d for c, d, _, _ in errors if c == check]
        status = FAIL if items else PASS
        if check == "Unconnected point" and orphans is not None and orphans.items:
            items = [f"{n}: no link in the SWMM model" for n in orphans.items] + items
            status = FAIL
        out.append(_f(f"Topology: {name}", status,
                      f"{noun[0].upper() + noun[1:]} found: {len(items)}. See the error points layer on the map." if items
                      else f"No {noun}.", "", items))
    return out


BUILD_BASIS = "Results only reproduce on the same SWMM engine build"


def run_all(model, runs, reports, staging, subcatchments=None, drf_supplied=False, model_epsg=None,
            model_build=None, model_source=None, rainfall=None):
    findings = []
    findings += check_georeference(model, model_epsg)
    findings += check_connectivity(model)
    findings += check_swmm_build(runs, reports, model_build, model_source)
    findings += check_reports(runs, reports)
    findings += check_results_populated(runs, staging)
    findings += check_runs_cover_scope(runs)
    if subcatchments:
        findings += check_cuhp(model, subcatchments)
        findings += check_drf(staging, runs, drf_supplied)
        findings += check_area_balance(staging, model, subcatchments)
    findings += check_flows_increase(staging, runs)
    findings += check_future_vs_existing(staging)
    findings += check_detention(staging, model)
    if rainfall is not None:
        findings += check_hour_rainfall(rainfall)
    return findings


# Plain-language text for each check, used by the text report:
# (name, topic, what it checks, why it matters, what to do on FAIL/WARN).
# Checks run once per storm or condition share one entry (matched by prefix).
CHECK_INFO = [
    ("SWMM georeferenced", "SWMM model",
     "The SWMM nodes have real-world coordinates in Colorado Central StatePlane (US feet).",
     "MHFD maps the design points and conduits straight from the SWMM coordinates. A model "
     "drawn at 0,0 or in another coordinate system lands in the wrong place.",
     "Georeference the model in PCSWMM or SWMM (NAD83 StatePlane Colorado Central, US feet) "
     "and save it again."),
    ("SWMM engine build", "SWMM model",
     "All storm runs used one SWMM version, the same version the model was built with.",
     "Different SWMM builds can give slightly different peak flows, so results would not match "
     "the model of record.",
     "Rerun the storms on the SWMM version the model was built with (tool A shows both)."),
    ("SWMM run ", "SWMM runs",
     "Each storm run finished without errors, its flow routing continuity error is small "
     "(WARN over 1%, FAIL over 5%), and it notes unstable links and flooded nodes.",
     "Continuity error measures water gained or lost by the solver. A large error, unstable "
     "links or unexpected flooding mean the peak flows can't be trusted.",
     "Open the .rpt for that storm, fix the listed errors, and shorten the routing time step or "
     "adjust the unstable links, then rerun. Flooding may be real; confirm it is expected."),
    ("Storms modeled: ", "SWMM runs",
     "The runs supplied include the storms MHFD's scope asks for (2, 5, 10, 25, 50, 100 and "
     "500-yr) for both Existing and Future conditions.",
     "The Hydrology GDB and supplemental tables expect a peak flow for every standard storm "
     "and condition.",
     "Run the missing storms, or confirm with MHFD that they are not in your scope."),
    ("Results populated: ", "Results",
     "Each storm run put a peak flow on every design point. Flags points missing from the "
     "report and points showing 0 cfs even though area drains to them.",
     "Blank or zero peaks end up in the Peak Flow table and on MHFD's maps.",
     "Check that the report is a finished SWMM .rpt. A 0 cfs point is often on the dry side of "
     "a flow divider in small storms; confirm that is expected."),
    ("CUHP target nodes in SWMM", "CUHP",
     "Every CUHP subcatchment's outlet (target node) exists in the SWMM model.",
     "Runoff sent to a node that isn't in SWMM is dropped, so all downstream peak flows are "
     "too low.",
     "Fix the target node name in CUHP, or add the node to SWMM, then rerun CUHP and SWMM."),
    ("CUHP target node spelling", "CUHP",
     "CUHP target node names match SWMM exactly, including upper and lower case.",
     "SWMM accepts a case difference, but exact-match tools (including MHFD's DRF tool) will "
     "miss those subcatchments.",
     "Make the names match exactly in CUHP or SWMM."),
    ("Subcatchment size", "CUHP",
     "CUHP subcatchments average 100 acres or less; any over 130 acres are listed for "
     "information only.",
     "MHFD's scope template suggests these sizes. CUHP assumes one runoff response per "
     "subcatchment, so very large ones blur the hydrograph timing and leave fewer design points "
     "along the drainageway.",
     "Consider splitting large subcatchments if the average is over 100 acres."),
    ("Depth reduction factors", "CUHP",
     "Wherever a design point drains 2 square miles or more, the area-corrected (depth "
     "reduction factor, DRF) runs were supplied.",
     "Rain doesn't fall at the same depth over a whole large watershed. MHFD requires CUHP "
     "rainfall to be reduced by the DRF for areas of 2 sq mi or more (USDCM Rainfall chapter, "
     "submittal #5). Without it, peak flows at those points are too high.",
     "Make the DRF runs for the listed design points (MHFD's DRF tool or CUHP's area "
     "correction), add those reports or the DRF table to tool C, and rerun it."),
    ("Drainage area balance", "CUHP",
     "All CUHP subcatchment area reaches the SWMM network; none drains to a missing node.",
     "Area that never reaches SWMM is missing from every downstream peak flow and contributing "
     "area.",
     "Fix the CUHP target nodes listed under 'CUHP target nodes in SWMM'."),
    ("Peak flows rise with return period", "Results",
     "At every design point the peak flow goes up as the storm gets bigger, from the 2-yr to "
     "the 500-yr.",
     "A bigger storm giving a smaller peak usually means the storms were run from different CUHP "
     "versions or a run is incomplete.",
     "Rerun all storms from the same, current CUHP workbook and check the listed points."),
    ("Peak flows: small decreases", "Results",
     "Drops between storms smaller than 0.5 cfs or 1%.",
     "These usually come from flow caps at dividers or full pipes and are listed for "
     "information only.",
     "No action needed unless a drop looks wrong."),
    ("Future vs existing 100-yr", "Results",
     "Design points where the Future 100-yr peak is more than 30% above Existing.",
     "MHFD hydrology guidance asks the report to note and explain increases over 30%.",
     "Explain the listed increases in the hydrology report, or check the Future imperviousness."),
    ("Detention rating curves", "Detention",
     "Each SWMM storage node (pond) produced stage-area-volume rows for the Detention Rating "
     "Curve table.",
     "MHFD's GDB stores each pond's storage curve.",
     "Add a storage curve to any pond that has none in SWMM."),
    ("Detention discharge", "Detention",
     "Each pond's outflow (cfs at each depth) could be filled in the Detention Rating Curve "
     "table. The tool can only read outflow from a SWMM tabular outlet curve.",
     "Without the outflow column, MHFD can't see how much a pond releases at each depth.",
     "For ponds with no tabular outlet, the discharge column is blank. Fill it in by hand in "
     "hydro_detention_rating_curve from the outlet structure rating, or model the outlet as a "
     "tabular outlet. Ponds with weirs or orifices have only part of their outflow listed."),
    ("Hour rainfall", "Rainfall",
     "The 1-hr and 6-hr rainfall depths (from the CUHP storm sheets) and a NOAA Atlas 14 point "
     "are available to fill the Point Precipitation and Hour Rainfall tables.",
     "MHFD's GDB records the design rainfall used and where it was taken.",
     "Pick or draw the NOAA Atlas 14 point in tool C and rerun it; the hour rainfall rows are "
     "then linked to it."),
    ("Subcatchment polygons loaded", "Subcatchments (tool B)",
     "Every CUHP subcatchment has a polygon in hydro_subcatchment for this study.",
     "A subcatchment with no polygon is missing from the GDB and MHFD's maps.",
     "Check the polygon names match CUHP (the tool ignores case and spaces), then rerun B and C."),
    ("Subcatchment area vs CUHP", "Subcatchments (tool B)",
     "Each subcatchment polygon's area is within 1% of the area in the CUHP workbook.",
     "A bigger difference usually means the polygons and CUHP are from different delineations, "
     "so the GDB doesn't match the model.",
     "Make sure the polygons and the CUHP workbook are the same version, then rerun B and C."),
    ("Subcatchment imperviousness", "Subcatchments (tool B)",
     "Each subcatchment has both Existing and Future imperviousness in the GDB.",
     "A CUHP workbook usually covers one condition. The other condition's field is left blank, "
     "or 0 when the GDB won't allow blank, and a 0 looks like a real value on MHFD's maps.",
     "Check the CUHP workbook condition picked in tool B. Load the other condition's "
     "imperviousness (its CUHP workbook, or the Multiple Runs sheet) or note it in the report."),
    ("Vertical datum", "Detention",
     "The vertical datum of the SWMM elevations was given for the rating curve table.",
     "Stage elevations mean nothing to a reviewer without their datum.",
     "Type the datum (e.g. NAVD88) in tool C and rerun it."),
    ("Related records", "Results",
     "Every peak flow, runoff volume, detention and rainfall row links to its parent feature "
     "through the GDB relationship classes.",
     "These links are what make a design point's or pond's pop-up show its results. An "
     "unlinked row can't be reached from the map.",
     "Rerun C. If a relationship uses a field the table doesn't have, the GDB template is out of "
     "date; use the latest MHFD template."),
    ("Blank required fields", "Other",
     "Fields the geodatabase does not allow to be blank but the model has no value for.",
     "The load would fail on these, so the tool filled them with the field's default (0 or "
     "empty when there is none).",
     "Check the listed fields and enter real values where they apply."),
    ("Topology: ", "Topology check", None, None, None),
]
TOPOLOGY_WHY = ("MHFD's maps and contributing areas rely on a clean network and subcatchments "
                "that tile the study area exactly.")
# GDB topology check name -> (name in the report, what "No ..." / "3 ..." counts)
TOPOLOGY_NAMES = {
    "Overlap": ("Subcatchment overlaps", "overlaps in subcatchments"),
    "Gap": ("Subcatchment gaps", "gaps in subcatchments"),
    "Multipart subcatchment": ("Multipart subcatchments", "multipart subcatchments"),
    "Dangle": ("Conduit dangles", "conduit dangles"),
    "Wrong end point": ("Conduit end points", "conduits ending on the wrong design point"),
    "Unconnected point": ("Orphan nodes", "orphan nodes"),
    "Stacked points": ("Stacked design points", "stacked design points"),
}
TOPOLOGY_INFO = {
    "Subcatchment overlaps": ("Subcatchment polygons don't overlap each other.",
                              "Edit the listed polygons so they share boundaries (see the error "
                              "points layer), then rerun B and C."),
    "Subcatchment gaps": ("There are no gaps or holes between or inside subcatchments, so "
                          "neighboring boundaries are coincident.",
                          "Snap the neighboring boundaries together or fill the hole (see the "
                          "error points layer), then rerun B and C."),
    "Multipart subcatchments": ("Each subcatchment is a single polygon, not several pieces.",
                                "Merge or split the listed polygons so each is one piece, then "
                                "rerun B and C."),
    "Conduit dangles": ("Every conduit (routing element) starts and ends on a design point.",
                        "Snap the conduit end to its node in SWMM and rerun C."),
    "Conduit end points": ("Each conduit's ends sit on the design points its from/to nodes name.",
                           "Fix the conduit's From/To node or move its end in SWMM, then rerun C."),
    "Orphan nodes": ("Every SWMM node and design point is connected to a conduit or other link.",
                     "Connect the listed nodes to the network or delete them from SWMM, then "
                     "rerun C."),
    "Stacked design points": ("No two design points sit in the same place.",
                              "Move or remove the duplicate node in SWMM and rerun C."),
}
_FAMILY_NAMES = {"SWMM run ": "SWMM run quality"}
TOPICS = ["SWMM model", "SWMM runs", "CUHP", "Subcatchments (tool B)", "Results", "Detention",
          "Rainfall", "Topology check", "Other"]
_RANK = {FAIL: 0, WARN: 1, PASS: 2, INFO: 3}


def _family(check):
    """(family name, topic, (checks, why, fix), member label or None) for a check name."""
    for key, topic, desc, why, fix in CHECK_INFO:
        if key.endswith(" ") and check.startswith(key):
            member = check[len(key):]
            if key == "Topology: ":
                d, f = TOPOLOGY_INFO.get(member, ("", ""))
                return member, topic, (d, TOPOLOGY_WHY, f), None
            return _FAMILY_NAMES.get(key, key.strip().rstrip(":")), topic, (desc, why, fix), member
        if check == key:
            return check, topic, (desc, why, fix), None
    return check, "Other", ("", "", ""), None


def to_text(findings, model_name="", tool_label="", when=None, max_items=None, width=100):
    """Plain-text QA report: summary, checks run, then FAIL, WARN, PASS, INFO blocks.
    Every flagged item is listed unless max_items caps the lists."""
    import datetime
    import textwrap

    fams = {}  # family -> {"topic", "desc", "members": [(label, finding)]}
    for f in findings:
        name, topic, desc, member = _family(f.check)
        fam = fams.setdefault(name, {"topic": topic, "desc": desc, "members": []})
        fam["members"].append((member, f))
    for fam in fams.values():
        fam["status"] = min((f.status for _, f in fam["members"]), key=_RANK.get)
        fam["source"] = next((f.source for _, f in fam["members"] if f.source), "")

    def wrap(text, indent, first=""):
        return textwrap.fill(text, width, initial_indent=indent + first,
                             subsequent_indent=indent + " " * len(first))

    counts = defaultdict(int)
    for fam in fams.values():
        counts[fam["status"]] += 1
    when = when or datetime.datetime.now()
    out = ["HYDROLOGY QA REPORT", ""]
    if model_name:
        out.append(f"Model:  {model_name}")
    if tool_label:
        out.append(f"Tool:   {tool_label}")
    out.append(f"Run:    {when:%Y-%m-%d %H:%M}")
    out.append("Result: " + "   ".join(f"{counts[s]} {s}" for s in (FAIL, WARN, PASS, INFO)))
    out += ["", "SUMMARY OF QC CHECKS"]
    for topic in TOPICS:
        names = [n for n, fam in fams.items() if fam["topic"] == topic]
        if not names:
            continue
        out += ["", f"  {topic.upper()}"]
        for n in names:
            desc = fams[n]["desc"][0]
            first = desc.split(". ")[0].rstrip(".") + "." if desc else ""
            out.append(wrap(f"{n}: {first}" if first else n, "    ",
                            f"[{fams[n]['status']}]".ljust(7)))

    titles = {FAIL: "FAIL: fix before submitting", WARN: "WARN: review",
              PASS: "PASS", INFO: "INFO: for information"}
    for status in (FAIL, WARN, PASS, INFO):
        names = [n for n, fam in fams.items() if fam["status"] == status]
        if not names:
            continue
        out += ["", "=" * width, f"{titles[status]} ({len(names)})", "=" * width]
        for n in names:
            fam = fams[n]
            out += ["", f"{n} ".ljust(width - len(status) - 1, ".") + f" {status}"]
            desc, why, fix = fam["desc"]
            if desc:
                out.append(wrap(desc, "  ", "Checks: "))
            if why:
                out.append(wrap(why, "  ", "Why:    "))
            if fam["source"]:
                out.append(wrap(fam["source"], "  ", "Basis:  "))
            members = fam["members"]
            if len(members) == 1 and members[0][0] is None:
                f = members[0][1]
                out.append(wrap(f.detail, "  ", "Found:  "))
                if f.status in (FAIL, WARN):
                    out += [wrap(str(i), "      ", "- ") for i in f.items[:max_items]]
                    if max_items and len(f.items) > max_items:
                        out.append(f"      ... and {len(f.items) - max_items} more")
            else:
                out.append("  Found:")
                shown = None
                for member, f in members:
                    out.append(wrap(f.detail, "    ", f"{member or n:<16} {f.status:<5} "))
                    if f.status in (FAIL, WARN) and f.items and f.items == shown:
                        out.append("        - same list as above")
                    elif f.status in (FAIL, WARN) and f.items:
                        shown = f.items
                        cap = max(5, max_items // 2) if max_items else None
                        out += [wrap(str(i), "        ", "- ") for i in f.items[:cap]]
                        if cap and len(f.items) > cap:
                            out.append(f"        ... and {len(f.items) - cap} more")
            if fix and status in (FAIL, WARN):
                out.append(wrap(fix, "  ", "To do:  "))
    return "\n".join(out) + "\n"
