"""Turn a SWMM model, its reports and (optionally) the CUHP workbook into rows
shaped like the MHFD Hydrology GDB v4.2 tables.

Nothing here depends on arcpy, so the same staging runs in ArcGIS Pro (the
.pyt loader) and in plain Python (run_staging.py, tests).
"""

import csv
import json
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from . import constants as C
from .swmm_inp import outlet_rating, stage_storage_table


@dataclass
class RunSpec:
    """One SWMM report: which modeled condition and storm it represents."""
    rpt_path: str
    condition: str                   # domain label, e.g. "Existing"
    return_period: str               # canonical key, e.g. "100"
    correction_area_sq_mi: float = 0  # CUHP area-correction (DRF) run, 0 = none

    @classmethod
    def from_filename(cls, path, condition=None, return_period=None):
        """Infer condition / storm / correction area from a file name.

        Understands MHFD/CUHP names such as 1_Ex_100yr_5mi^2_Name.rpt and
        loose names such as BSO_Fut_010yr.rpt. Explicit arguments win.
        """
        stem = os.path.splitext(os.path.basename(path))[0]
        parts = re.split(r"[_\s\-.]+", stem)
        cond = C.normalize_condition(condition) if condition else None
        rp = C.normalize_return_period(return_period) if return_period else None
        area = 0.0
        for p in parts:
            if cond is None and C.normalize_condition(p):
                cond = C.normalize_condition(p)
            m = re.match(r"(?i)^(\d+(\.\d+)?)mi(\^?2)?$", p)
            if m:
                area = float(m.group(1))
        if rp is None:
            # Prefer explicit storm tokens (100yr, Q100, WQ, 1%+) over bare
            # numbers, which are often run indexes (1_Ex_100yr_...).
            for pattern in (r"(?i)^(q\d+|\d+(yr|yrs|y|year)|wq|1%\+?)$", r"^\d+$"):
                hits = [C.normalize_return_period(p) for p in parts if re.match(pattern, p)]
                hits = [h for h in hits if h]
                if hits:
                    rp = hits[0]
                    break
        if cond is None or rp is None:
            raise ValueError(f"Cannot tell condition/return period from '{stem}'; "
                             "pass them explicitly.")
        return cls(rpt_path=path, condition=cond, return_period=rp, correction_area_sq_mi=area)


@dataclass
class Staging:
    design_points: list = field(default_factory=list)
    routing_elements: list = field(default_factory=list)
    peak_flows: list = field(default_factory=list)
    runoff_volumes: list = field(default_factory=list)
    detention_summary: list = field(default_factory=list)
    rating_curves: list = field(default_factory=list)
    subcatchments: list = field(default_factory=list)
    messages: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Network helpers

def upstream_map(model):
    """node(lower) -> set of nodes(lower) directly upstream (all link types)."""
    up = defaultdict(set)
    for lk in model.links.values():
        up[lk.to_node.lower()].add(lk.from_node.lower())
    return up


def contributing_areas(model, subcatchments, link_flows=None):
    """Contributing drainage area (sq mi) for every node.

    Each subcatchment's area is carried to every node downstream of its
    target node along all outgoing links, so where flow splits (dividers,
    ponds with overflow weirs) both branches carry the full upstream area.
    Outfall areas can therefore add up to more than the CUHP total.
    `link_flows` is accepted for older callers and not used.
    """
    local = defaultdict(float)
    for sc in subcatchments:
        if sc.target_node and sc.area_sq_mi:
            local[sc.target_node.lower()] += sc.area_sq_mi
    outgoing = defaultdict(list)
    for lk in model.links.values():
        outgoing[lk.from_node.lower()].append(lk.to_node.lower())
    total = defaultdict(float)
    for start, area in local.items():
        seen, todo = {start}, deque([start])
        while todo:
            node = todo.popleft()
            total[node] += area
            for nxt in outgoing.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
    return {name: total.get(name.lower(), 0.0) for name in model.nodes}


def read_drf_csv(path):
    """Read the MHFD 'Calculate Depth Reduction Factors' CSV output.

    Returns {node_name_lower: (contributing_area, drf_minor, drf_major)}.
    """
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            name = str(row.get("DesignPoint", "")).strip()
            if not name:
                continue
            def num(k):
                try:
                    return float(row.get(k))
                except (TypeError, ValueError):
                    return None
            out[name.lower()] = (num("ContributingArea"), num("DRF_Minor"), num("DRF_Major"))
    return out


MINOR_STORMS = {"WQ", "1", "2", "5", "10"}


# ---------------------------------------------------------------------------

def build(model, runs, reports, study_id, subcatchments=None, drf=None,
          node_filter=None, status_value=None):
    """Build all staged rows.

    model         SwmmModel
    runs          list[RunSpec]
    reports       {rpt_path: SwmmReport}
    study_id      int (MHFD study id)
    subcatchments list[cuhp.Subcatchment] or None
    drf           output of read_drf_csv() or None
    node_filter   optional callable(Node) -> bool for which nodes become design
                  points. Routing elements are only built between design points,
                  because both ends need a design point id in the template.
    status_value  optional text for the design point 'status' field
    """
    st = Staging()
    src = os.path.basename(model.path)

    # Design points ------------------------------------------------------
    dp_names = set()
    for n in model.nodes.values():
        if node_filter and not node_filter(n):
            continue
        if n.x is None:
            st.messages.append(f"Node {n.name} has no coordinates; skipped as a design point.")
            continue
        dp_names.add(n.name)
        st.design_points.append({
            "swmm_node_name": n.name,
            "swmm_node_type_id": C.NODE_TYPE_IDS[n.kind],
            "x": n.x, "y": n.y,
            "location": n.tag,
            "status": status_value,
            "comments": None,
            "study_id": study_id,
            "source_file_path": src,
        })

    # Routing elements ---------------------------------------------------
    for lk in model.links.values():
        a, b = model.canonical_node_name(lk.from_node), model.canonical_node_name(lk.to_node)
        if a not in dp_names or b not in dp_names:
            continue
        coords = model.link_geometry(lk)
        if coords is None:
            st.messages.append(f"Link {lk.name} has no geometry; skipped.")
            continue
        st.routing_elements.append({
            "routing_name": lk.name,
            "from_node_name": a, "to_node_name": b,
            "coords": coords,
            # A conduit is implied; name the other link types since the table has no type field.
            "comments": None if lk.kind == "CONDUIT" else f"SWMM {lk.kind.lower()}",
            "study_id": study_id,
        })

    # Contributing area / DRF -------------------------------------------
    # Group runs: (condition, rp) -> {correction_area: report}
    grouped = defaultdict(dict)
    for run in runs:
        rpt = reports[run.rpt_path]
        grouped[(run.condition, run.return_period)][float(run.correction_area_sq_mi or 0)] = (run, rpt)
    conditions = sorted({c for c, _ in grouped}, key=lambda c: C.CONDITION_IDS[c])

    areas = contributing_areas(model, subcatchments) if subcatchments else {}

    def pick(cond, rp, node_name):
        """Report to read for a node: the matching DRF run, else the base run."""
        options = grouped.get((cond, rp))
        if not options:
            return None, None
        drf_bin = 0.0
        if drf and node_name.lower() in drf:
            _, dmin, dmaj = drf[node_name.lower()]
            drf_bin = float((dmin if rp in MINOR_STORMS else dmaj) or 0)
        if drf_bin in options:
            return options[drf_bin]
        if 0.0 in options:
            return options[0.0]
        return None, None

    # Peak flow / runoff volume -----------------------------------------
    for cond in conditions:
        cid = C.CONDITION_IDS[cond]
        for name in sorted(dp_names, key=lambda s: s.lower()):
            pf = {"swmm_node_name": name, "study_id": study_id,
                  "hydro_modeled_condition_id": cid,
                  "contributing_drainage_area_sq_mi": None,
                  "is_major_drf_applied": None, "is_minor_drf_applied": None,
                  "comments": None, "source_file_path": None}
            rv = {"swmm_node_name": name, "study_id": study_id,
                  "hydro_modeled_condition_id": cid, "comments": None,
                  "source_file_path": None}
            if drf and name.lower() in drf:
                carea, dmin, dmaj = drf[name.lower()]
                pf["contributing_drainage_area_sq_mi"] = carea
                pf["is_minor_drf_applied"] = 1 if (dmin or 0) > 0 else 0
                pf["is_major_drf_applied"] = 1 if (dmaj or 0) > 0 else 0
            elif name in areas:
                pf["contributing_drainage_area_sq_mi"] = round(areas[name], 4)
            files, missing = set(), []
            for rp in C.RETURN_PERIODS:
                run, rpt = pick(cond, rp, name)
                if rpt is None:
                    continue
                res = rpt.inflow(name)
                if res is None:
                    missing.append(rp)
                    continue
                pf[C.PEAK_FLOW_FIELD[rp]] = res.max_total_cfs
                if rp in C.VOLUME_FIELD:
                    rv[C.VOLUME_FIELD[rp]] = round(res.total_volume_ac_ft, 3)
                files.add(os.path.basename(run.rpt_path))
            if missing:
                pf["comments"] = f"Not in report for: {', '.join(missing)}"
            src_files = "; ".join(sorted(files))[:255] or None
            pf["source_file_path"] = rv["source_file_path"] = src_files
            st.peak_flows.append(pf)
            st.runoff_volumes.append(rv)

    # Detention (storage nodes) -----------------------------------------
    storages = [n for n in model.nodes.values() if n.kind == "STORAGE"]
    for cond in conditions:
        cid = C.CONDITION_IDS[cond]
        for n in storages:
            row = {"node_name": n.name, "study_id": study_id,
                   "hydro_modeled_condition_id": cid, "comments": None}
            for rp in C.RETURN_PERIODS:
                if rp not in C.DETENTION_VOLUME_FIELD:
                    continue
                run, rpt = pick(cond, rp, n.name)
                if rpt is None:
                    continue
                s = rpt.storage.get(n.name)
                inflow = rpt.inflow(n.name)
                if s is not None:
                    row[C.DETENTION_VOLUME_FIELD[rp]] = round(s.max_volume_ac_ft, 3)
                    row[C.DETENTION_FLOW_FIELDS[rp][1]] = s.max_outflow_cfs
                if inflow is not None:
                    row[C.DETENTION_FLOW_FIELDS[rp][0]] = inflow.max_total_cfs
            st.detention_summary.append(row)

    for n in storages:
        table = stage_storage_table(model, n)
        if not table:
            st.messages.append(f"Storage {n.name}: shape not tabular/functional; no rating curve rows.")
            continue
        q_at, out_links, skipped = outlet_rating(model, n.name)
        note = None
        if skipped:
            note = ("Discharge excludes non-tabular outlet links: " + ", ".join(skipped))[:255]
        for i, r in enumerate(table):
            desc = "Bottom of storage" if i == 0 else ("Top of storage" if i == len(table) - 1 else None)
            st.rating_curves.append({
                "node_name": n.name, "study_id": study_id,
                "elevation_description": desc,
                "stage_elevation_ft": round(r["elevation_ft"], 2),
                "stage_area_sq_ft": round(r["area_sq_ft"], 1),
                "stage_volume_ac_ft": round(r["volume_ac_ft"], 3),
                "stage_discharge_cfs": round(q_at(r["depth_ft"]), 2) if q_at else None,
                "comments": note,
            })

    # Subcatchment attributes (joined to polygons by name in the loader) ---
    for sc in subcatchments or []:
        st.subcatchments.append(subcatchment_row(model, sc, study_id))
    return st


def _mi_ft(v):
    return None if v is None else round(v * 5280.0, 1)


# ---------------------------------------------------------------------------
# Detention placeholders and catchment roll-ups

def detention_placeholders(model, subcatchments, study_id, size_ft=100.0):
    """One placeholder detention boundary row per SWMM storage node.

    Rows carry the node's center (x, y in model units) for a square drawn by
    the loader, the nodes immediately up and downstream, and the full
    upstream CUHP area in acres.
    """
    areas = contributing_areas(model, subcatchments) if subcatchments else {}
    up, down = defaultdict(list), defaultdict(list)
    for lk in model.links.values():
        up[lk.to_node.lower()].append(model.canonical_node_name(lk.from_node) or lk.from_node)
        down[lk.from_node.lower()].append(model.canonical_node_name(lk.to_node) or lk.to_node)
    rows = []
    for n in model.nodes.values():
        if n.kind != "STORAGE" or n.x is None:
            continue
        area = areas.get(n.name)
        rows.append({
            "node_name": n.name,
            "node_name_upstream": ", ".join(sorted(set(up[n.name.lower()])))[:255] or None,
            "node_name_downstream": ", ".join(sorted(set(down[n.name.lower()])))[:255] or None,
            "upstream_drainage_area_ac": round(area * C.SQMI_TO_AC, 2) if area is not None else None,
            "ownership_type_id": C.OWNERSHIP_UNKNOWN,
            "study_id": study_id,
            "comments": f"PLACEHOLDER {size_ft:g} ft square at SWMM storage node; "
                        "edit to the facility outline and fill status/ownership/as-built fields.",
            "x": n.x, "y": n.y,
        })
    return rows


def imperviousness(sc, condition="Existing"):
    """(existing %, future %) for a CUHP subcatchment. The Multiple Runs sheet gives
    both; otherwise the single % impervious column belongs to the workbook's
    condition and the other is unknown (None)."""
    if sc.impervious_existing_pct is not None or sc.impervious_future_pct is not None:
        return sc.impervious_existing_pct, sc.impervious_future_pct
    if str(condition).lower().startswith("fut"):
        return None, sc.impervious_pct
    return sc.impervious_pct, None


def catchment_rollup(subcatchments, condition="Existing"):
    """hydro_catchment roll-up fields from the CUHP subcatchments in one basin."""
    areas = [(sc, sc.area_ac) for sc in subcatchments if sc.area_ac]
    total = sum(a for _, a in areas)

    def weighted(i):
        vals = [(imperviousness(sc, condition)[i], a) for sc, a in areas]
        vals = [(v, a) for v, a in vals if v is not None]
        if not vals or not total:
            return None
        return round(sum(v * a for v, a in vals) / sum(a for _, a in vals), 2)

    return {
        "drainage_area_total_ac": round(total, 2),
        "subbasin_count": len(subcatchments),
        "subbasin_area_average_ac": round(total / len(areas), 2) if areas else None,
        "impervious_existing_pct": weighted(0),
        "impervious_future_pct": weighted(1),
    }


def subcatchment_row(model, sc, study_id, condition="Existing"):
    """hydro_subcatchment attributes for one CUHP subcatchment."""
    ex, fut = imperviousness(sc, condition)
    return {
        "subcatchment_name": sc.name,
        "swmm_target_node": (model.canonical_node_name(sc.target_node) if model else None) or sc.target_node,
        "area_ac": round(sc.area_ac, 3) if sc.area_ac is not None else None,
        "length_to_centroid_ft": _mi_ft(sc.length_to_centroid_mi),
        "length_basin_ft": _mi_ft(sc.length_mi),
        "slope_ftft": sc.slope_ftft,
        "imperviousness_exisiting_pct": ex,
        "imperviousness_future_pct": fut,
        "depression_pervious_in": sc.depression_pervious_in,
        "depression_impervious_in": sc.depression_impervious_in,
        "infiltration_initialrate_inhr": sc.infil_initial_inhr,
        "infiltration_decaycoeff_1sec": sc.infil_decay_1sec,
        "infiltration_finalrate_inhr": sc.infil_final_inhr,
        "has_override_parameters": 1 if sc.has_override else 0,
        "study_id": study_id,
    }


def match_names(names, cuhp_names):
    """{name: cuhp_name} for names matching a CUHP name, ignoring case and spaces."""
    key = lambda v: re.sub(r"\s+", "", str(v)).lower()  # noqa: E731
    lookup = {key(c): c for c in cuhp_names}
    return {n: lookup[key(n)] for n in names if n is not None and key(n) in lookup}


# ---------------------------------------------------------------------------
# Plain-file export for review without ArcGIS

def write_outputs(st, out_dir, wkid=C.DEFAULT_WKID):
    os.makedirs(out_dir, exist_ok=True)

    def write_csv(name, rows):
        if not rows:
            return
        keys = []
        for r in rows:
            for k in r:
                if k not in keys and k != "coords":
                    keys.append(k)
        with open(os.path.join(out_dir, name), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    write_csv("hydro_design_point.csv", st.design_points)
    write_csv("hydro_routing_element.csv", st.routing_elements)
    write_csv("hydro_peak_flow.csv", st.peak_flows)
    write_csv("hydro_runoff_volume.csv", st.runoff_volumes)
    write_csv("hydro_detention_summary.csv", st.detention_summary)
    write_csv("hydro_detention_rating_curve.csv", st.rating_curves)
    write_csv("hydro_subcatchment_attributes.csv", st.subcatchments)

    crs = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{wkid}"}}
    pts = {"type": "FeatureCollection", "crs": crs, "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["x"], d["y"]]},
         "properties": {k: v for k, v in d.items() if k not in ("x", "y")}}
        for d in st.design_points]}
    lines = {"type": "FeatureCollection", "crs": crs, "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": r["coords"]},
         "properties": {k: v for k, v in r.items() if k != "coords"}}
        for r in st.routing_elements]}
    with open(os.path.join(out_dir, "hydro_design_point.geojson"), "w") as fh:
        json.dump(pts, fh)
    with open(os.path.join(out_dir, "hydro_routing_element.geojson"), "w") as fh:
        json.dump(lines, fh)
