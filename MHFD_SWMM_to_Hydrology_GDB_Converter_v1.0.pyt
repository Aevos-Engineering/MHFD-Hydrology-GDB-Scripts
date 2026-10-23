# -*- coding: utf-8 -*-
"""MHFD SWMM to Hydrology GDB Converter: ArcGIS Pro Python toolbox.

Tools, in the order a study is built
  A. Generate SWMM rpt files for all storms: runs SWMM once per CUHP
     interface file.
  B. Load Subcatchments and Catchments: the subcatchment polygons with CUHP
     attributes, and one catchment (the study area) merged from them.
  C. Load SWMM Results to Hydrology GDB: design points, routing elements, peak
     flow, runoff volume, detention boundaries (placeholder squares where none
     exist), summary and rating curves, and hour rainfall, then adds the
     styled layers to the map, checks topology (subcatchments without
     overlaps, gaps or holes; design points snapped to routing elements with no
     dangles) and writes the one QA report for all three tools.

Hydrology QA Report, Export SWMM Layers, Create Detention Boundary
Placeholders and Check Topology stay in this file for scripting and tests but
are not listed.

Parsing and QA live in the mhfd_hydro package next to this file, so they can
be tested outside ArcGIS (see run_staging.py and tests/).
"""

import importlib
import os
import sys
import tempfile

import arcpy

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import mhfd_hydro  # noqa: E402
import mhfd_hydro.constants as C  # noqa: E402
import mhfd_hydro.cuhp as cuhp  # noqa: E402
import mhfd_hydro.layers as layers  # noqa: E402
import mhfd_hydro.qa as qa  # noqa: E402
import mhfd_hydro.staging as staging  # noqa: E402
import mhfd_hydro.swmm_inp as swmm_inp  # noqa: E402
import mhfd_hydro.swmm_rpt as swmm_rpt  # noqa: E402
import mhfd_hydro.swmm_run as swmm_run  # noqa: E402
import mhfd_hydro.topology as topology  # noqa: E402

for _m in (mhfd_hydro, C, swmm_inp, swmm_rpt, swmm_run, cuhp, staging, qa, layers, topology):
    importlib.reload(_m)  # pick up edits without restarting Pro

CONDITIONS = list(C.CONDITION_IDS)
# The toolbox is published as one version (also in the file name); the tools
# carry no versions of their own. Bump this and the file name together.
TOOLBOX_VERSION = "1.0"
RUN_FILE_TYPES = (".rpt", ".txt")
RETURN_PERIOD_CHOICES = ["WQ", "1", "2", "5", "10", "25", "50", "100", "500", "1%+"]


class Toolbox(object):
    def __init__(self):
        self.label = f"MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION}"
        self.alias = "mhfdswmmgdb"
        # HydrologyQAReport and ExportSwmmLayers stay in this file for scripting
        # but are not listed: A and C write the QA report, and MHFD imports the
        # Hydrology GDB rather than separate SWMM layers.
        # DetentionPlaceholders is kept for scripting; the load tool makes the placeholders.
        # TopologyCheck too: the load tool runs the topology check at the end.
        self.tools = [RunSwmmStorms, LoadSubcatchments, LoadSwmmResults]


# ---------------------------------------------------------------------------
# Shared parameter helpers

def _p_inp():
    p = arcpy.Parameter(displayName="SWMM input file (.inp)", name="swmm_inp",
                        datatype="DEFile", parameterType="Required", direction="Input")
    p.filter.list = ["inp"]
    return p


def _p_runs(interface_only=False, reports_only=False):
    if interface_only:
        label, col = "CUHP interface files, one per condition and storm", "CUHP interface file (.txt)"
    elif reports_only:
        label = "SWMM report files (.rpt) from tool A, one per condition and storm"
        col = "SWMM report (.rpt)"
    else:
        label = ("SWMM report files (.rpt), or CUHP interface files (.txt) to run SWMM on, "
                 "one per condition and storm")
        col = "Report (.rpt) or CUHP interface file (.txt)"
    p = arcpy.Parameter(displayName=label, name="runs", datatype="GPValueTable",
                        parameterType="Required", direction="Input")
    p.columns = [["DEFile", col], ["GPString", "Modeled condition"],
                 ["GPString", "Return period (yr)"], ["GPDouble", "DRF correction area (sq mi, 0 = none)"]]
    # Pro's browser hides file types it doesn't know (.rpt) unless the column filters on them.
    # Pro's browser hides .rpt unless the column filters on it, and the filter
    # keeps other files (.xlsx, .out) out of the list. The type may be preset
    # and read-only, so set the list on its own.
    try:
        p.filters[0].type = "File"
    except Exception:
        pass
    p.filters[0].list = ["txt"] if interface_only else (["rpt"] if reports_only else ["rpt", "txt"])
    p.filters[1].type = "ValueList"
    p.filters[1].list = CONDITIONS
    p.filters[2].type = "ValueList"
    p.filters[2].list = RETURN_PERIOD_CHOICES
    return p


def _p_cuhp():
    p = arcpy.Parameter(displayName="CUHP workbook (.xlsm), for drainage areas and subcatchment attributes",
                        name="cuhp", datatype="DEFile", parameterType="Optional", direction="Input")
    p.filter.list = ["xlsm", "xlsx"]
    return p


def _p_drf():
    p = arcpy.Parameter(displayName="MHFD depth reduction factor CSV (optional)",
                        name="drf_csv", datatype="DEFile", parameterType="Optional", direction="Input")
    p.filter.list = ["csv"]
    return p


def _p_engine():
    p = arcpy.Parameter(displayName="SWMM engine (runswmm.exe or swmm5.dll; blank = find EPA SWMM)",
                        name="swmm_engine", datatype="DEFile", parameterType="Optional",
                        direction="Input")
    p.filter.list = ["exe", "dll"]
    return p


def _p_run_dir():
    p = arcpy.Parameter(displayName="Folder for SWMM runs (blank = .inp folder)",
                        name="run_dir", datatype="DEFolder", parameterType="Optional",
                        direction="Input")
    return p


def _p_builds():
    mb = arcpy.Parameter(displayName="SWMM build the model was made with (read from files beside the .inp)",
                         name="model_build", datatype="GPString", parameterType="Optional",
                         direction="Input")
    rb = arcpy.Parameter(displayName="SWMM build these runs use",
                         name="run_build", datatype="GPString", parameterType="Optional",
                         direction="Input")
    mb.enabled = rb.enabled = False
    return [mb, rb]


def _is_report(path):
    return str(path).lower().endswith(".rpt")


def _update_builds(inp_p, runs_p, engine_p, mb, rb):
    """Fill the two read-only build boxes when the model, runs or engine change."""
    if not any(p.altered and not p.hasBeenValidated for p in (inp_p, runs_p, engine_p)
               if p is not None):
        return
    if inp_p.value:
        b, src = swmm_run.model_build(inp_p.valueAsText)
        mb.value = f"{b} (from {os.path.basename(src)})" if b else "unknown (no .rpt, .chi or .ini beside the .inp)"
    entries = []
    rows = _rows(runs_p) if runs_p is not None else []
    if runs_p is None or any(r and r[0] and not _is_report(r[0]) for r in rows or []):
        try:
            engine = swmm_run.find_engine(engine_p.valueAsText if engine_p is not None else None)
            b = swmm_run.engine_build(engine)
            if engine_p is not None and not engine_p.value and engine[1]:
                engine_p.value = engine[1]  # show exactly which engine will run
            entries.append(f"{b or 'unknown'}: engine {engine[1] or 'swmm-toolkit'}")
        except swmm_run.SwmmEngineNotFound:
            entries.append("no SWMM engine found")
    for r in rows or []:
        if r and r[0] and _is_report(r[0]):
            entries.append(f"{swmm_run.report_build(str(r[0])) or 'unknown'}: {os.path.basename(str(r[0]))}")
    seen = []
    for e in entries:  # one line per distinct build/engine
        if e.split(":")[0] not in [x.split(":")[0] for x in seen] or e.startswith("no "):
            seen.append(e)
    rb.value = "; ".join(seen) if seen else None


def _build_messages(mb, rb, engine_p=None):
    if not rb.value:
        return
    text = rb.valueAsText
    if "no SWMM engine found" in text and engine_p is not None:
        engine_p.setErrorMessage("No SWMM engine found. Install EPA SWMM 5.2 or browse to runswmm.exe "
                                 "or swmm5.dll.")
        return
    model_b = mb.valueAsText.split(" ")[0] if mb.value else None
    run_bs = sorted({e.split(":")[0].strip() for e in text.split(";")} - {"unknown"})
    model_b = None if model_b == "unknown" else model_b
    problem = None
    if len(run_bs) > 1:
        problem = f"The runs use different SWMM builds ({', '.join(run_bs)})."
    elif run_bs and model_b and not swmm_run.same_build(run_bs[0], model_b):
        problem = f"The runs use SWMM {run_bs[0]} but the model was made with {model_b}."
    if problem:
        rb.setWarningMessage(problem + " Peak flows may not match the model of record. "
                             "Browse to the matching engine if this isn't intended.")


def _model_build(inp):
    return swmm_run.model_build(inp)


def _p_qa_out(name="qa_report", required="Optional"):
    p = arcpy.Parameter(displayName="QA report (.txt)", name=name, datatype="DEFile",
                        parameterType=required, direction="Output")
    p.filter.list = ["txt"]
    return p


def _cell(v):
    """Plain text of a value table cell. Pro hands DEFile cells back as value
    objects whose str() can be empty, so read .value first."""
    v = getattr(v, "value", v)
    return "" if v is None else str(v).strip()


def _rows(param, n=4):
    """Value table rows with the file column as a plain path string."""
    out = []
    for row in param.values or []:
        row = list(row) + [None] * (n - len(row))
        row[0] = _cell(row[0])
        row[1], row[2] = _cell(row[1]), _cell(row[2])
        out.append(row[:n])
    return out


def _show_full_path(param):
    """Pro can show a picked folder by its name only; writing the full path
    back makes the box show it."""
    if param.altered and not param.hasBeenValidated and param.value:
        full = param.valueAsText
        if full and os.path.isabs(full):
            param.value = full


def _fill_runs_from_names(param):
    """Fill blank condition / return period cells from the report file name."""
    if not param.altered or param.value is None:
        return
    rows, changed = [], False
    for path, cond, rp, area in _rows(param):
        if path and (not cond or not rp):
            try:
                spec = staging.RunSpec.from_filename(str(path), cond or None, rp or None)
                cond, rp = cond or spec.condition, rp or spec.return_period
                if area in (None, ""):
                    area = spec.correction_area_sq_mi
                changed = True
            except ValueError:
                # Interface_100yr.txt names the storm but not the condition.
                try:
                    spec = staging.RunSpec.from_filename(str(path), "Existing", rp or None)
                    if not rp:
                        rp, changed = spec.return_period, True
                except ValueError:
                    pass
        rows.append([path, cond, rp, area if area not in (None, "") else 0])
    if changed:
        param.values = rows


def _runs_messages(param, reports_only=False):
    """Error on rows whose condition or storm can't be worked out."""
    if param.value is None:
        return
    for path, cond, rp, _ in _rows(param):
        ext = os.path.splitext(path)[1].lower()
        if path and reports_only and ext != ".rpt":
            param.setErrorMessage(f"{os.path.basename(str(path))} is not a SWMM report (.rpt). "
                                  "Run tool A on the CUHP interface files first to make the reports.")
            return
        if path and ext not in RUN_FILE_TYPES:
            param.setErrorMessage(f"{os.path.basename(str(path))} is not a SWMM report (.rpt) or "
                                  "CUHP interface file (.txt). SWMM's binary .out file can't be read.")
            return
        if path and (not cond or not rp):
            param.setErrorMessage(f"Pick the modeled condition and return period for "
                                  f"{os.path.basename(str(path))}.")
            return


def _default_run_dir(inp):
    return os.path.dirname(os.path.abspath(inp))


def _run_qa(inp, rows, cuhp_path, drf_path, qa_path, tool_label="Hydrology QA Report",
            engine_path=None, run_dir=None):
    """Parse the model, reports and CUHP, run the QA checks and write the report."""
    model, runs, reports, subs, info, drf = _read_inputs(inp, rows, cuhp_path, drf_path,
                                                         engine_path, run_dir)
    st = staging.build(model, runs, reports, 0, subcatchments=subs, drf=drf)
    mb, msrc = _model_build(inp)
    findings = qa.run_all(model, runs, reports, st, subs, drf_supplied=bool(drf),
                          model_epsg=_pcswmm_epsg(inp), model_build=mb, model_source=msrc)
    _report_findings(findings, qa_path, inp, tool_label)


def _run_interfaces(inp, rows, engine_path, run_dir):
    """Run SWMM for rows whose file is a CUHP interface file; return rows with .rpt paths."""
    for r in rows:
        if os.path.splitext(str(r[0]))[1].lower() not in RUN_FILE_TYPES:
            raise ValueError(f"{r[0]} is not a SWMM report (.rpt) or CUHP interface file (.txt).")
    todo = [(i, r) for i, r in enumerate(rows) if not str(r[0]).lower().endswith(".rpt")]
    if not todo:
        return rows
    engine = swmm_run.find_engine(engine_path)
    arcpy.AddMessage(f"SWMM engine: {engine[1] or 'swmm-toolkit'}, build {swmm_run.engine_build(engine)}")
    run_dir = run_dir or _default_run_dir(inp)
    done = swmm_run.run_storms(inp, [r for _, r in todo], run_dir, engine,
                               log=arcpy.AddMessage)
    rows = list(rows)
    for (i, _), res in zip(todo, done):
        rows[i] = list(res)
        arcpy.AddMessage(f"  Wrote {res[0]}")
    return rows


def _read_inputs(inp, runs_vt, cuhp_path, drf_path, engine_path=None, run_dir=None):
    model = swmm_inp.read_inp(inp)
    rows = []
    for row in runs_vt:
        path, cond, rp, area = (list(row) + [None] * 4)[:4]
        rows.append([str(path), cond, rp, area])
    rows = _run_interfaces(inp, rows, engine_path, run_dir)
    runs = []
    for path, cond, rp, area in rows:
        cond_n, rp_n = C.normalize_condition(cond or ""), C.normalize_return_period(rp or "")
        if not cond_n or not rp_n:
            spec = staging.RunSpec.from_filename(path, cond or None, rp or None)
        else:
            spec = staging.RunSpec(path, cond_n, rp_n, float(area or 0))
        runs.append(spec)
    reports = {r.rpt_path: swmm_rpt.read_rpt(r.rpt_path) for r in runs}
    for path, rep in reports.items():
        if not rep.node_inflow:
            raise ValueError(f"{os.path.basename(path)} has no Node Inflow Summary. Use a completed "
                             "SWMM report (.rpt) with the node summary turned on.")
    subs, info = cuhp.read_cuhp(cuhp_path) if cuhp_path else (None, {})
    drf = staging.read_drf_csv(drf_path) if drf_path else None
    return model, runs, reports, subs, info, drf


def _pcswmm_epsg(inp):
    chi = os.path.splitext(inp)[0] + ".chi"
    if not os.path.exists(chi):
        return None
    import configparser
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(chi, encoding="latin-1")
        return cp.get("General", "Map EPSG", fallback=None)
    except configparser.Error:
        return None


def _report_findings(findings, txt_path, inp, tool_label):
    """Write the QA report, point to it from the first warning, and open it."""
    if txt_path:
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(qa.to_text(findings, os.path.basename(inp), tool_label))
    n_fail = sum(f.status == qa.FAIL for f in findings)
    n_warn = sum(f.status == qa.WARN for f in findings)
    where = f" Open the QA report for details: {txt_path}" if txt_path else ""
    if n_fail or n_warn:  # first warning, so it's the line Pro shows in the result banner
        arcpy.AddWarning(f"QA found {n_fail} FAIL and {n_warn} WARN.{where}")
    for f in findings:
        msg = f"[{f.status}] {f.check}: {f.detail}"
        if f.status == qa.FAIL:
            arcpy.AddWarning(msg)
        else:
            arcpy.AddMessage(msg)
    if txt_path:
        arcpy.AddMessage(f"QA report written to {txt_path}")
        _open_file(txt_path)


def _open_file(path):
    """Open a file in its default app (Notepad for .txt). Windows only; never fails the tool."""
    if not sys.platform.startswith("win"):
        return
    try:
        os.startfile(path)
    except OSError as e:
        arcpy.AddMessage(f"Could not open {path} automatically: {e}")


# ---------------------------------------------------------------------------
# GDB helpers

def _fields(table):
    return {f.name.lower(): f.name for f in arcpy.ListFields(table)}


def _relationship_keys(gdb, origin, destination):
    """(origin_primary_field, destination_foreign_field) for a relationship class."""
    for child in arcpy.Describe(gdb).children:
        if getattr(child, "dataType", "") != "RelationshipClass":
            continue
        if origin in [n.lower() for n in child.originClassNames] and \
                destination in [n.lower() for n in child.destinationClassNames]:
            prim = [k[0] for k in child.originClassKeys if k[1] == "OriginPrimary"]
            fore = [k[0] for k in child.originClassKeys if k[1] == "OriginForeign"]
            if prim and fore:
                return prim[0], fore[0]
    return None, None


def _delete_study_rows(table, study_id, where_extra=None):
    where = f"study_id = {int(study_id)}"
    if where_extra:
        where += f" AND {where_extra}"
    n = 0
    with arcpy.da.UpdateCursor(table, ["OID@"], where) as cur:
        for _ in cur:
            cur.deleteRow()
            n += 1
    return n


_FILLED = {}  # table.field -> value put into blanks by _insert since the last clear
_ZERO_TYPES = ("Integer", "SmallInteger", "BigInteger", "Double", "Single")


def _required_fills(table):
    """{field_lower: value} for fields that can't be blank: the field's default,
    else 0 for numbers or "" for text. Other types are left to the GDB."""
    out = {}
    for f in arcpy.ListFields(table):
        if getattr(f, "isNullable", True) or not getattr(f, "editable", True):
            continue
        ftype = getattr(f, "type", "String")
        default = getattr(f, "defaultValue", None)
        if default is not None:
            out[f.name.lower()] = default
        elif ftype in _ZERO_TYPES:
            out[f.name.lower()] = 0
        elif ftype == "String":
            out[f.name.lower()] = ""
    return out


def _fill_blanks(low, fills, filled, table):
    """Put fills into blank values of the row dict low; record table.field in filled."""
    for k, v in fills.items():
        if k in low and low[k] is None:
            low[k] = v
            if filled is not None:
                filled.setdefault(f"{os.path.basename(table)}.{k}", v)
    return low


# Each table's own id, which the template's attribute rule should fill on insert.
ID_FIELDS = {
    "hydro_design_point": "hydro_design_point_id",
    "hydro_routing_element": "hydro_routing_element_id",
    "hydro_peak_flow": "hydro_peak_flow_id",
    "hydro_runoff_volume": "hydro_runoff_volume_id",
    "hydro_detention_boundary": "hydro_detention_id",
    "hydro_detention_summary": "hydro_detention_summary_id",
    "hydro_detention_rating_curve": "hydro_detention_rating_curve_id",
    "hydro_point_precipitation": "hydro_point_precip_id",
    "hydro_hour_rainfall": "hydro_hour_rainfall_id",
    "hydro_catchment": "hydro_catchment_id",
    "hydro_subcatchment": "hydro_subcatchment_id",
}
_IDS_FILLED = {}  # table -> ids the tool filled because the attribute rule didn't


def _ensure_ids(table, study_id):
    """Give the study's rows a unique id where the attribute rule left it blank
    (next number after the largest id in the table). Existing ids are kept."""
    name = os.path.basename(table).lower()
    fld = ID_FIELDS.get(name)
    if not fld or fld.lower() not in _fields(table):
        return 0
    used = [r[0] for r in arcpy.da.SearchCursor(table, [fld]) if r[0] is not None]
    nxt, n = max(used or [0]) + 1, 0
    with arcpy.da.UpdateCursor(table, [fld], f"study_id = {int(study_id)}") as cur:
        for (val,) in cur:
            if val is None:
                cur.updateRow([nxt])
                nxt, n = nxt + 1, n + 1
    if n:
        _IDS_FILLED[name] = _IDS_FILLED.get(name, 0) + n
    return n


_EDIT_FIELDS = ("created_user", "created_date", "last_edited_user", "last_edited_date")
_USER = []


def _esri_user():
    """The ArcGIS account signed in to Pro, else the Windows login."""
    if not _USER:
        name = None
        try:
            name = (arcpy.GetPortalDescription().get("user") or {}).get("username")
        except Exception:
            pass
        if not name:
            import getpass
            name = getpass.getuser()
        _USER.append(name)
    return _USER[0]


def _tracking_values(table):
    """Created/edited user and date (UTC, like editor tracking) for a table whose
    editor tracking is off; {} when it's on, since the GDB fills them itself."""
    import datetime
    try:
        if getattr(arcpy.Describe(table), "editorTrackingEnabled", False):
            return {}
    except Exception:
        return {}
    have = _fields(table)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    vals = {"created_user": _esri_user(), "created_date": now,
            "last_edited_user": _esri_user(), "last_edited_date": now}
    return {k: v for k, v in vals.items() if k in have}


def _check_relationships(gdb, study_id):
    """[(link, linked, total, problem)] for each relationship class between the
    study's rows: how many child rows find their parent."""
    out, seen = [], set()
    where = f"study_id = {int(study_id)}"
    for child in arcpy.Describe(gdb).children:
        if getattr(child, "dataType", "") != "RelationshipClass":
            continue
        prim = [k[0] for k in child.originClassKeys if k[1] == "OriginPrimary"]
        fore = [k[0] for k in child.originClassKeys if k[1] == "OriginForeign"]
        if not (child.originClassNames and child.destinationClassNames and prim and fore):
            continue
        o_name, d_name = child.originClassNames[0], child.destinationClassNames[0]
        o, d = os.path.join(gdb, o_name), os.path.join(gdb, d_name)
        key = (o_name.lower(), d_name.lower(), prim[0].lower(), fore[0].lower())
        if key in seen or not (arcpy.Exists(o) and arcpy.Exists(d)):
            continue
        seen.add(key)
        link = f"{o_name} -> {d_name} ({prim[0]} -> {fore[0]})"
        if prim[0].lower() not in _fields(o) or fore[0].lower() not in _fields(d):
            out.append((link, 0, 0, "the relationship uses a field the table doesn't have"))
            continue
        kids = [r[0] for r in arcpy.da.SearchCursor(d, [fore[0]], where)]
        if not kids:
            continue
        parents = {str(r[0]).upper() for r in arcpy.da.SearchCursor(o, [prim[0]], where)
                   if r[0] is not None}
        linked = sum(1 for k in kids if k is not None and str(k).upper() in parents)
        out.append((link, linked, len(kids), None))
    return out


def _insert(table, rows, extra_cols=(), filled=None):
    """Insert dict rows, writing only columns that exist in the table. Blank values
    in fields that can't be blank get _required_fills(); filled collects which.
    Created/edited user and date are filled when editor tracking is off."""
    if not rows:
        return 0
    track = _tracking_values(table)
    if track:
        rows = [dict(track, **{k: v for k, v in r.items() if v is not None or k not in track})
                for r in rows]
    have = _fields(table)
    fills = _required_fills(table)
    cols = []
    for r in rows:
        for k in r:
            if k.lower() in have and have[k.lower()] not in cols:
                cols.append(have[k.lower()])
    cols_lower = [c.lower() for c in cols]
    with arcpy.da.InsertCursor(table, list(extra_cols) + cols) as cur:
        for r in rows:
            low = {k.lower(): v for k, v in r.items()}
            for c in fills:
                if c in cols_lower:
                    low.setdefault(c, None)
            _fill_blanks(low, fills, _FILLED if filled is None else filled, table)
            extras = [r.get(c) for c in extra_cols]
            cur.insertRow(extras + [low.get(c) for c in cols_lower])
    return len(rows)


def _project_xy(x, y, from_sr, to_sr):
    pt = arcpy.PointGeometry(arcpy.Point(x, y), from_sr)
    if from_sr.factoryCode != to_sr.factoryCode:
        pt = pt.projectAs(to_sr)
    return pt


def _polyline(coords, from_sr, to_sr):
    arr = arcpy.Array([arcpy.Point(x, y) for x, y in coords])
    line = arcpy.Polyline(arr, from_sr)
    if from_sr.factoryCode != to_sr.factoryCode:
        line = line.projectAs(to_sr)
    return line


def _has_z(fc):
    return bool(getattr(arcpy.Describe(fc), "hasZ", False))


def _polygon(rings, from_sr, to_sr, has_z=False):
    """Polygon from rings of (x, y), projected to to_sr, with Z = 0 when the
    target feature class stores Z (the cursor rejects 2D shapes there)."""
    parts = arcpy.Array([arcpy.Array([arcpy.Point(x, y, 0) if has_z else arcpy.Point(x, y)
                                      for x, y in ring]) for ring in rings])
    poly = arcpy.Polygon(parts, from_sr, has_z)
    if from_sr.factoryCode != to_sr.factoryCode:
        poly = poly.projectAs(to_sr)
    return poly


def _rings(geom):
    """Rings of a polygon geometry as lists of (x, y); arcpy separates holes with None."""
    out = []
    for part in geom:
        ring = []
        for pt in part:
            if pt is None:
                if ring:
                    out.append(ring)
                ring = []
            else:
                ring.append((pt.X, pt.Y))
        if ring:
            out.append(ring)
    return out


def _to_target(geom, to_sr, has_z):
    """A user's polygon in the target coordinate system, rebuilt with Z = 0 if needed."""
    sr = geom.spatialReference
    if sr is not None and sr.name and sr.factoryCode != to_sr.factoryCode:
        geom = geom.projectAs(to_sr)
    if has_z and not getattr(geom, "hasZ", False):
        geom = _polygon(_rings(geom), to_sr, to_sr, True)
    return geom


def _square(x, y, size_ft, from_sr, to_sr, has_z=False):
    """size_ft square centered on (x, y) in model coordinates."""
    feet_per_unit = getattr(from_sr, "metersPerUnit", 0.3048006096012192) / 0.3048006096012192
    h = size_ft / 2.0 / (feet_per_unit or 1.0)
    ring = [(x - h, y - h), (x - h, y + h), (x + h, y + h), (x + h, y - h), (x - h, y - h)]
    return _polygon([ring], from_sr, to_sr, has_z)


def _model_sr(param):
    sr = arcpy.SpatialReference()
    if param.value:
        sr.loadFromString(param.valueAsText)
        return sr
    return arcpy.SpatialReference(C.DEFAULT_WKID)


def _recalc_extents(paths):
    """Reset each feature class's stored extent so zoom-to-layer fits the data."""
    for fc in paths:
        try:
            arcpy.management.RecalculateFeatureClassExtent(fc)
        except Exception as e:  # a schema lock (open attribute table) blocks it
            arcpy.AddWarning(f"Could not recalculate the extent of {os.path.basename(fc)}: {e}")


def _points_extent(points, sr, pad=0.05):
    xs = [p.firstPoint.X for p in points]
    ys = [p.firstPoint.Y for p in points]
    if not xs:
        return None
    dx, dy = (max(xs) - min(xs)) * pad or 500, (max(ys) - min(ys)) * pad or 500
    return arcpy.Extent(min(xs) - dx, min(ys) - dy, max(xs) + dx, max(ys) + dy, spatial_reference=sr)


_LABEL_FIELDS = {"hydro_subcatchment": "subcatchment_name", "hydro_catchment": "basin_name"}


def _style_polygons(lyr, fc):
    """Hollow outline and name labels for feature classes MHFD has no layer file for."""
    try:
        sym = lyr.symbology
        if hasattr(sym, "renderer") and hasattr(sym.renderer, "symbol"):
            sym.renderer.symbol.color = {"RGB": [0, 0, 0, 0]}
            sym.renderer.symbol.outlineColor = {"RGB": [0, 92, 230, 100]} if fc == "hydro_subcatchment" \
                else {"RGB": [230, 0, 0, 100]}
            sym.renderer.symbol.outlineWidth = 1 if fc == "hydro_subcatchment" else 2.5
            lyr.symbology = sym
    except Exception:
        pass
    field = _LABEL_FIELDS.get(fc)
    if field and fc == "hydro_subcatchment":
        try:
            lc = lyr.listLabelClasses()[0]
            lc.expression = f"$feature.{field}"
            lyr.showLabels = True
        except Exception:
            pass


# Everything tool C puts on the map, bottom to top, and the tables beside it.
ALL_LAYERS = ("hydro_catchment", "hydro_subcatchment", "hydro_detention_boundary",
              "hydro_routing_element", "hydro_design_point", "hydro_point_precipitation")
ALL_TABLES = ("hydro_peak_flow", "hydro_runoff_volume", "hydro_detention_summary",
              "hydro_detention_rating_curve", "hydro_hour_rainfall")
# Decimals shown for float fields (the template stores them single precision, so
# Pro would otherwise show digits like 5123.4502).
FIELD_DECIMALS = {"hydro_detention_rating_curve": {"stage_elevation_ft": 2, "stage_area_sq_ft": 1,
                                                   "stage_volume_ac_ft": 3, "stage_discharge_cfs": 2}}


def _set_decimals(item, decimals):
    """Show the named fields of a map layer or table rounded to a number of decimals."""
    try:
        cim = item.getDefinition("V3")
        table = getattr(cim, "featureTable", None) or cim
        descs = {d.fieldName.lower(): d for d in (table.fieldDescriptions or [])}
        for field, n in decimals.items():
            d = descs.get(field)
            if d is None:
                d = arcpy.cim.CreateCIMObjectFromClassName("CIMFieldDescription", "V3")
                d.fieldName, d.alias, d.visible = field, field, True
                table.fieldDescriptions = list(table.fieldDescriptions or []) + [d]
            fmt = arcpy.cim.CreateCIMObjectFromClassName("CIMNumericFormat", "V3")
            fmt.roundingOption = "esriRoundNumberOfDecimals"
            fmt.roundingValue = n
            fmt.zeroPad = True
            d.numberFormat = fmt
        item.setDefinition(cim)
    except Exception:
        pass  # display only; the data are right either way


def _same_source(item, src, where):
    """True for an earlier copy of this study's layer or table (or an unfiltered one)."""
    try:
        same = os.path.normcase(os.path.abspath(item.dataSource)) == src
    except Exception:
        return False
    return same and (getattr(item, "definitionQuery", "") or where).replace(" ", "") == \
        where.replace(" ", "")


def _add_study_layers(gdb, study_id, extent, fcs=("hydro_routing_element", "hydro_design_point"),
                      tables=()):
    """Add the study's layers (and tables) to the active map, filtered to the study,
    then zoom. Design points and routing elements use the MHFD layer files; the
    polygons get a simple outline style."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        arcpy.AddMessage("Not run from an open ArcGIS Pro project; layers not added to a map.")
        return
    m = aprx.activeMap
    if m is None:
        arcpy.AddWarning("No map is open, so the styled layers were not added. Open a map and "
                         "run the tool again to add them.")
        return
    where = f"study_id = {int(study_id)}"
    tmp = tempfile.mkdtemp(prefix="mhfd_lyrx_")
    for fc in fcs:
        if not arcpy.Exists(os.path.join(gdb, fc)):
            continue
        src = os.path.normcase(os.path.abspath(os.path.join(gdb, fc)))
        for lyr in m.listLayers():
            if lyr.supports("DATASOURCE") and _same_source(lyr, src, where):
                m.removeLayer(lyr)
        if fc in layers.LAYER_FILES:
            added = m.addLayer(arcpy.mp.LayerFile(layers.write_retargeted(fc, gdb, where, tmp)))
        else:
            added = [m.addDataFromPath(os.path.join(gdb, fc))]
            _style_polygons(added[0], fc)
        if added and added[0].supports("DEFINITIONQUERY"):
            added[0].definitionQuery = where  # also set through arcpy in case the CIM filter is ignored
        arcpy.AddMessage(f"Added '{added[0].name}' to map '{m.name}', filtered to study {study_id}.")
    for t in tables:
        if not arcpy.Exists(os.path.join(gdb, t)):
            continue
        src = os.path.normcase(os.path.abspath(os.path.join(gdb, t)))
        for old in m.listTables():
            if _same_source(old, src, where):
                m.removeTable(old)
        tbl = m.addDataFromPath(os.path.join(gdb, t))
        try:
            tbl.definitionQuery = where
        except Exception:
            pass
        if t in FIELD_DECIMALS:
            _set_decimals(tbl, FIELD_DECIMALS[t])
        arcpy.AddMessage(f"Added table '{t}' to map '{m.name}', filtered to study {study_id}.")
    view = aprx.activeView
    if extent is not None and view is not None and hasattr(view, "camera"):
        msr = m.spatialReference
        if msr is not None and msr.factoryCode != extent.spatialReference.factoryCode:
            extent = extent.projectAs(msr)
        view.camera.setExtent(extent)


def _clear_exists_warning(param):
    """Drop Pro's 'already exists' warning (000725) on an output file meant to be replaced."""
    try:
        if param.hasWarning() and "000725" in (param.message or ""):
            param.clearMessage()
    except Exception:
        pass


def _gdb_subcatchments(fc, study_id):
    """[(name, polygon acres, existing %, future %)] for the study's subcatchments in the GDB."""
    if not arcpy.Exists(fc):
        return []
    have = _fields(fc)
    cols = [have.get(c, "OID@") for c in ("imperviousness_exisiting_pct", "imperviousness_future_pct")]
    return [(name, geom.getArea("PLANAR", "ACRES") if geom is not None else None,
             ex if cols[0] != "OID@" else None, fut if cols[1] != "OID@" else None)
            for name, geom, ex, fut in arcpy.da.SearchCursor(
                fc, ["subcatchment_name", "SHAPE@"] + cols, f"study_id = {int(study_id)}")]


# ---------------------------------------------------------------------------

class LoadSwmmResults(object):
    def __init__(self):
        self.label = "C_Load SWMM Results to Hydrology GDB"
        self.description = ("Writes SWMM design points, routing elements, peak flows, runoff "
                            "volumes, detention boundaries and results, and hour rainfall into the MHFD Hydrology "
                            "GDB, keeping the table relationships intact, then adds the styled "
                            "layers to the map.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        gdb = arcpy.Parameter(displayName="Hydrology geodatabase (Built on MHFD v4.2)",
                              name="gdb", datatype="DEWorkspace",
                              parameterType="Required", direction="Input")
        study = arcpy.Parameter(displayName="MHFD study ID", name="study_id",
                                datatype="GPLong", parameterType="Required", direction="Input")
        sr = arcpy.Parameter(displayName="SWMM model coordinate system", name="model_sr",
                             datatype="GPCoordinateSystem", parameterType="Optional",
                             direction="Input")
        sr.value = arcpy.SpatialReference(C.DEFAULT_WKID).exportToString()
        replace = arcpy.Parameter(displayName="Replace rows already loaded for this study ID",
                                  name="replace", datatype="GPBoolean",
                                  parameterType="Optional", direction="Input")
        replace.value = True
        det = arcpy.Parameter(displayName="Load detention boundaries, summary and rating curves",
                              name="load_detention", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        det.value = True
        size = arcpy.Parameter(displayName="Placeholder detention square side (ft) (for storage "
                                           "nodes without a boundary)",
                               name="placeholder_ft", datatype="GPDouble",
                               parameterType="Optional", direction="Input")
        size.value = 100
        datum = arcpy.Parameter(displayName="Vertical datum of the SWMM elevations (fills the "
                                            "rating curve table)",
                                name="vertical_datum", datatype="GPString",
                                parameterType="Optional", direction="Input")
        datum.value = "NAVD88"
        status = arcpy.Parameter(displayName="Design point status (e.g. Master Plan, LOMR)",
                                 name="status_value", datatype="GPString",
                                 parameterType="Optional", direction="Input")
        noaa = arcpy.Parameter(displayName="NOAA Atlas 14 point (point layer or a point you draw; "
                                           "needed for Hour Rainfall)",
                               name="noaa_point", datatype="GPFeatureLayer",
                               parameterType="Optional", direction="Input")
        noaa.filter.list = ["Point"]
        qa_out = _p_qa_out()
        qa_out.displayName = ("QA report for all tools (.txt) (blank = <model>_C_QA.txt next to "
                              "the model)")
        return [gdb, _p_inp(), _p_runs(reports_only=True), study, _p_cuhp(), _p_drf(), sr, replace,
                det, size, datum, status, noaa, qa_out] + _p_builds()

    def updateParameters(self, params):
        _fill_runs_from_names(params[2])
        if len(params) > 15:  # an old dialog left open in Pro has fewer parameters
            _update_builds(params[1], params[2], None, params[14], params[15])
        if params[1].altered and not params[1].hasBeenValidated and params[1].value:
            epsg = _pcswmm_epsg(params[1].valueAsText)
            if epsg and epsg.isdigit():
                params[6].value = arcpy.SpatialReference(int(epsg)).exportToString()
        if len(params) > 13 and params[1].value and not params[13].altered:
            params[13].value = os.path.splitext(params[1].valueAsText)[0] + "_C_QA.txt"

    def updateMessages(self, params):
        _runs_messages(params[2], reports_only=True)
        if len(params) > 15:
            _build_messages(params[14], params[15])
        if len(params) > 13:  # rerunning C is meant to replace the old report
            _clear_exists_warning(params[13])
        if len(params) > 9 and params[9].value is not None and params[9].value <= 0:
            params[9].setErrorMessage("Give a size greater than 0.")
        if params[0].value:
            gdb = params[0].valueAsText
            missing = [t for t in ("hydro_design_point", "hydro_routing_element",
                                   "hydro_peak_flow", "hydro_runoff_volume")
                       if not arcpy.Exists(os.path.join(gdb, t))]
            if missing:
                params[0].setErrorMessage("Not an MHFD Hydrology GDB; missing " + ", ".join(missing))

    def execute(self, params, messages):
        gdb = params[0].valueAsText
        inp = params[1].valueAsText
        study_id = int(params[3].value)
        cuhp_path = params[4].valueAsText
        drf_path = params[5].valueAsText
        model_sr = _model_sr(params[6])
        replace = bool(params[7].value)
        load_det = bool(params[8].value)
        size_ft = float(params[9].value or 100)
        datum = params[10].valueAsText or None
        status_value = params[11].valueAsText
        noaa_layer = params[12].valueAsText if params[12].value else None
        qa_path = params[13].valueAsText or os.path.splitext(inp)[0] + "_C_QA.txt"

        arcpy.AddMessage(f"{self.label} (MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION})")
        arcpy.AddMessage("Reading SWMM, reports and CUHP...")
        _FILLED.clear()
        _IDS_FILLED.clear()
        rows = _rows(params[2])
        bad = [r[0] for r in rows if r[0] and not _is_report(r[0])]
        if bad:
            raise ValueError("Give SWMM reports (.rpt); run tool A on the CUHP interface files first. "
                             "Not a report: " + ", ".join(os.path.basename(b) for b in bad))
        model, runs, reports, subs, info, drf = _read_inputs(inp, rows, cuhp_path, drf_path)
        st = staging.build(model, runs, reports, study_id, subcatchments=subs, drf=drf,
                           status_value=status_value)
        for m in st.messages:
            arcpy.AddMessage("  " + m)

        T = {name: os.path.join(gdb, name) for name in (
            "hydro_design_point", "hydro_routing_element", "hydro_peak_flow",
            "hydro_runoff_volume", "hydro_detention_boundary", "hydro_detention_summary",
            "hydro_detention_rating_curve", "hydro_subcatchment", "hydro_point_precipitation",
            "hydro_hour_rainfall", "hydro_catchment")}
        target_sr = arcpy.Describe(T["hydro_design_point"]).spatialReference
        if model_sr.factoryCode != target_sr.factoryCode:
            arcpy.AddMessage(f"Projecting from {model_sr.name} to {target_sr.name}.")
        depths = (info or {}).get("rainfall") or []
        noaa_geom = None
        if noaa_layer:
            pts = [r[0] for r in arcpy.da.SearchCursor(noaa_layer, ["SHAPE@"]) if r[0] is not None]
            if len(pts) > 1:
                arcpy.AddWarning(f"The NOAA Atlas 14 input has {len(pts)} points; using the first.")
            if pts:
                noaa_geom = pts[0]
                if noaa_geom.spatialReference and noaa_geom.spatialReference.name and \
                        noaa_geom.spatialReference.factoryCode != target_sr.factoryCode:
                    noaa_geom = noaa_geom.projectAs(target_sr)
        rain_tables = arcpy.Exists(T["hydro_hour_rainfall"])

        edit = arcpy.da.Editor(gdb)
        edit.startEditing(False, False)
        edit.startOperation()
        try:
            if replace:
                for t in ("hydro_peak_flow", "hydro_runoff_volume", "hydro_routing_element",
                          "hydro_design_point"):
                    n = _delete_study_rows(T[t], study_id)
                    if n:
                        arcpy.AddMessage(f"  Removed {n} existing rows from {t}.")
                if load_det:
                    for t in ("hydro_detention_summary", "hydro_detention_rating_curve"):
                        if arcpy.Exists(T[t]):
                            n = _delete_study_rows(T[t], study_id)
                            if n:
                                arcpy.AddMessage(f"  Removed {n} existing rows from {t}.")

            # 1. Design points, then read back the ids the attribute rule assigned.
            dp_rows, dp_geoms = [], []
            for d in st.design_points:
                r = dict(d)
                r["SHAPE@"] = _project_xy(d["x"], d["y"], model_sr, target_sr)
                dp_geoms.append(r["SHAPE@"])
                dp_rows.append(r)
            _insert(T["hydro_design_point"], dp_rows, extra_cols=("SHAPE@",))
            _ensure_ids(T["hydro_design_point"], study_id)
            ids = {}
            with arcpy.da.SearchCursor(T["hydro_design_point"],
                                       ["swmm_node_name", "hydro_design_point_id", "GlobalID"],
                                       f"study_id = {study_id}") as cur:
                for name, dpid, gid in cur:
                    ids[str(name).lower()] = (dpid, gid)
            arcpy.AddMessage(f"Loaded {len(dp_rows)} design points.")

            # 2. Routing elements
            re_rows = []
            for r in st.routing_elements:
                row = {k: v for k, v in r.items() if k != "coords"}
                row["SHAPE@"] = _polyline(r["coords"], model_sr, target_sr)
                row["from_design_point_id"] = ids[r["from_node_name"].lower()][0]
                row["to_design_point_id"] = ids[r["to_node_name"].lower()][0]
                re_rows.append(row)
            _insert(T["hydro_routing_element"], re_rows, extra_cols=("SHAPE@",))
            _ensure_ids(T["hydro_routing_element"], study_id)
            arcpy.AddMessage(f"Loaded {len(re_rows)} routing elements.")

            # 3. Peak flow / runoff volume, keyed the way each relationship class says.
            for table, rows in (("hydro_peak_flow", st.peak_flows),
                                ("hydro_runoff_volume", st.runoff_volumes)):
                prim, fore = _relationship_keys(gdb, "hydro_design_point", table)
                have = _fields(T[table])
                out = []
                for r in rows:
                    dpid, gid = ids[r["swmm_node_name"].lower()]
                    row = {k: v for k, v in r.items() if k != "swmm_node_name"}
                    row["hydro_design_point_id"] = dpid
                    if "rel_globalid" in have:
                        row["rel_globalid"] = gid
                    if fore and prim:
                        row[fore] = gid if prim.lower() == "globalid" else dpid
                    out.append(row)
                _insert(T[table], out)
                _ensure_ids(T[table], study_id)
                arcpy.AddMessage(f"Loaded {len(out)} rows into {table}"
                                 + (f" (related on {prim} -> {fore})." if prim else "."))

            # 4. Detention: a placeholder square for storage nodes without a boundary
            #    (edited outlines are kept), then the summary and rating curves.
            if load_det and arcpy.Exists(T["hydro_detention_boundary"]):
                fc = T["hydro_detention_boundary"]
                have_bnd = {str(r[0]).lower() for r in arcpy.da.SearchCursor(
                    fc, ["node_name"], f"study_id = {study_id}") if r[0]}
                new = [r for r in staging.detention_placeholders(model, subs, study_id, size_ft)
                       if r["node_name"].lower() not in have_bnd]
                has_z = _has_z(fc)
                for r in new:
                    r["SHAPE@"] = _square(r.pop("x"), r.pop("y"), size_ft, model_sr,
                                          arcpy.Describe(fc).spatialReference, has_z)
                _insert(fc, new, extra_cols=("SHAPE@",))
                _ensure_ids(fc, study_id)  # boundaries drawn by hand may lack an id too
                if new:
                    arcpy.AddMessage(f"Added {len(new)} placeholder detention boundaries "
                                     f"({size_ft:g} ft squares) at storage nodes without one.")
                det, placeholders = {}, 0
                with arcpy.da.UpdateCursor(T["hydro_detention_boundary"],
                                           ["node_name", "hydro_detention_id", "hydro_design_point_id",
                                            "comments"],
                                           f"study_id = {study_id}") as cur:
                    for name, did, dpid, note in cur:
                        if not name:
                            continue
                        det[str(name).lower()] = did
                        placeholders += bool(note and str(note).startswith("PLACEHOLDER"))
                        if str(name).lower() in ids:  # design point ids change on every reload
                            cur.updateRow([name, did, ids[str(name).lower()][0], note])
                summ = [dict(r, hydro_detention_id=det[r["node_name"].lower()])
                        for r in st.detention_summary if r["node_name"].lower() in det]
                curves = [dict(r, hydro_detention_id=det[r["node_name"].lower()], vertical_datum=datum)
                          for r in st.rating_curves if r["node_name"].lower() in det]
                _insert(T["hydro_detention_summary"], summ)
                _insert(T["hydro_detention_rating_curve"], curves)
                _ensure_ids(T["hydro_detention_summary"], study_id)
                _ensure_ids(T["hydro_detention_rating_curve"], study_id)
                storage_names = sorted({r["node_name"] for r in st.detention_summary})
                no_poly = [n for n in storage_names if n.lower() not in det]
                arcpy.AddMessage(f"Loaded detention results for {len(set(r['node_name'] for r in summ))} "
                                 f"facilities ({len(curves)} rating curve rows).")
                if placeholders:
                    arcpy.AddMessage(f"  {placeholders} detention boundaries are still placeholders; "
                                     "edit them to the facility outlines and fill lifecycle "
                                     "status, ownership and as-built fields.")
                if no_poly:
                    arcpy.AddWarning(f"{len(no_poly)} SWMM storage nodes have no detention boundary "
                                     "for this study and were skipped: "
                                     + ", ".join(no_poly[:40]) + (" ..." if len(no_poly) > 40 else ""))

            # 5. Hour rainfall: CUHP storm depths, always loaded; linked to the NOAA
            #    Atlas 14 point when there is one (the QA report fails without it).
            rain_loaded, point_id = False, None
            if rain_tables and depths:
                pp = T["hydro_point_precipitation"]
                if noaa_geom is not None and arcpy.Exists(pp):
                    if replace:
                        _delete_study_rows(pp, study_id)
                    _insert(pp, [{
                        "SHAPE@": noaa_geom, "study_id": study_id,
                        "comments": "NOAA Atlas 14 point; depths from CUHP "
                                    + os.path.basename(cuhp_path or "")}], extra_cols=("SHAPE@",))
                    _ensure_ids(pp, study_id)
                if arcpy.Exists(pp):
                    rows_ = [r for r in arcpy.da.SearchCursor(pp, ["OID@", "hydro_point_precip_id"],
                                                              f"study_id = {study_id}")]
                    if rows_:
                        point_id = max(rows_)[1]  # the newest point for this study
                        if noaa_geom is None:
                            arcpy.AddMessage("Using the NOAA Atlas 14 point already in the GDB "
                                             "for this study.")
                _delete_study_rows(T["hydro_hour_rainfall"], study_id)
                _insert(T["hydro_hour_rainfall"], [{
                    "hydro_point_precip_id": point_id, "study_id": study_id,
                    "return_period_year_type_id": C.RETURN_PERIOD_TYPE_IDS[d["return_period"]],
                    "one_hour_precip_in": d["one_hour_in"], "six_hour_precip_in": d["six_hour_in"],
                    "comments": f"CUHP sheet {d['sheet']}"}
                    for d in depths if d["return_period"] in C.RETURN_PERIOD_TYPE_IDS])
                _ensure_ids(T["hydro_hour_rainfall"], study_id)
                rain_loaded = True
                arcpy.AddMessage(f"Loaded hour rainfall for {len(depths)} return periods.")
                if point_id is None:
                    arcpy.AddWarning("Hour Rainfall loaded without a NOAA Atlas 14 point (QA FAIL). "
                                     "Give the point (a point layer or a point you draw) and run "
                                     "C again to link it.")
            elif not depths:
                arcpy.AddWarning("Hour Rainfall not loaded: no 1-hr/6-hr depths were found (give "
                                 "the CUHP workbook).")

            if arcpy.Exists(T["hydro_subcatchment"]) and not any(
                    True for _ in arcpy.da.SearchCursor(T["hydro_subcatchment"], ["OID@"],
                                                        f"study_id = {study_id}")):
                arcpy.AddMessage("No subcatchment polygons for this study yet; run tool B to load them.")

            edit.stopOperation()
            edit.stopEditing(True)
        except Exception:
            try:
                edit.abortOperation()
                edit.stopEditing(False)
            except Exception:
                pass
            raise

        _recalc_extents([T["hydro_design_point"], T["hydro_routing_element"]]
                        + ([T["hydro_detention_boundary"]] if load_det and arcpy.Exists(
                            T["hydro_detention_boundary"]) else [])
                        + ([T["hydro_point_precipitation"]] if noaa_geom is not None else []))
        try:
            _add_study_layers(gdb, study_id, _points_extent(dp_geoms, target_sr), fcs=ALL_LAYERS,
                              tables=ALL_TABLES)
        except Exception as e:  # the load itself succeeded; don't fail on map trouble
            arcpy.AddWarning(f"Loaded, but the layers could not be added to the map: {e}")

        topo = None
        try:  # the old separate topology tool, run here so users have one less step
            arcpy.AddMessage("Checking topology...")
            topo = _check_topology(gdb, study_id)
        except Exception as e:
            arcpy.AddWarning(f"Loaded, but the topology check could not run: {e}")

        mb, msrc = _model_build(inp)
        findings = qa.run_all(model, runs, reports, st, subs, drf_supplied=bool(drf),
                              model_epsg=_pcswmm_epsg(inp), model_build=mb, model_source=msrc,
                              rainfall={"point": point_id is not None, "depths": depths,
                                        "loaded": rain_loaded})
        if topo is not None:  # one orphan node line covering SWMM and the GDB
            orphans = next((f for f in findings if f.check == qa.ORPHAN_CHECK), None)
            findings = [f for f in findings if f.check != qa.ORPHAN_CHECK]
            findings += qa.topology_findings(topo, TOPOLOGY_CHECKS, orphans)
        findings += qa.subcatchment_load_findings(_gdb_subcatchments(T["hydro_subcatchment"], study_id),
                                                  subs)
        findings.append(qa.vertical_datum_finding(datum) if load_det else None)
        try:
            findings.append(qa.related_records_finding(_check_relationships(gdb, study_id),
                                                       _IDS_FILLED))
        except Exception as e:
            arcpy.AddWarning(f"Loaded, but the related records check could not run: {e}")
        findings.append(qa.filled_fields_finding(_FILLED))
        _report_findings([f for f in findings if f is not None], qa_path, inp,
                          f"{self.label} (MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION})")


class DetentionPlaceholders(object):
    """Square placeholder detention boundaries at the SWMM storage nodes."""

    def __init__(self):
        self.label = "Create Detention Boundary Placeholders"
        self.description = ("Adds a square polygon to hydro_detention_boundary for each SWMM "
                            "storage node that doesn't have one yet, with the node names and "
                            "upstream drainage area filled. Edit the squares to the facility "
                            "outlines before submitting.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        gdb = arcpy.Parameter(displayName="Hydrology geodatabase (Built on MHFD v4.2)",
                              name="gdb", datatype="DEWorkspace",
                              parameterType="Required", direction="Input")
        study = arcpy.Parameter(displayName="MHFD study ID", name="study_id",
                                datatype="GPLong", parameterType="Required", direction="Input")
        size = arcpy.Parameter(displayName="Placeholder square size (ft)", name="size_ft",
                               datatype="GPDouble", parameterType="Required", direction="Input")
        size.value = 100
        sr = arcpy.Parameter(displayName="SWMM model coordinate system", name="model_sr",
                             datatype="GPCoordinateSystem", parameterType="Optional",
                             direction="Input")
        sr.value = arcpy.SpatialReference(C.DEFAULT_WKID).exportToString()
        cuhp_p = _p_cuhp()
        cuhp_p.displayName = "CUHP workbook (.xlsm), for upstream drainage area"
        return [gdb, _p_inp(), study, cuhp_p, size, sr]

    def updateParameters(self, params):
        if params[1].altered and not params[1].hasBeenValidated and params[1].value:
            epsg = _pcswmm_epsg(params[1].valueAsText)
            if epsg and epsg.isdigit():
                params[5].value = arcpy.SpatialReference(int(epsg)).exportToString()

    def updateMessages(self, params):
        if params[0].value and not arcpy.Exists(os.path.join(params[0].valueAsText,
                                                             "hydro_detention_boundary")):
            params[0].setErrorMessage("Not an MHFD Hydrology GDB; missing hydro_detention_boundary")
        if params[4].value is not None and params[4].value <= 0:
            params[4].setErrorMessage("Give a size greater than 0.")

    def execute(self, params, messages):
        gdb, inp, study_id = params[0].valueAsText, params[1].valueAsText, int(params[2].value)
        size_ft = float(params[4].value or 100)
        model_sr = _model_sr(params[5])
        fc = os.path.join(gdb, "hydro_detention_boundary")
        arcpy.AddMessage(f"{self.label} (MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION})")
        model = swmm_inp.read_inp(inp)
        subs = cuhp.read_cuhp(params[3].valueAsText)[0] if params[3].value else None
        if not subs:
            arcpy.AddWarning("No CUHP workbook given; upstream drainage area left blank.")
        rows = staging.detention_placeholders(model, subs, study_id, size_ft)
        existing = {str(r[0]).lower() for r in arcpy.da.SearchCursor(fc, ["node_name"],
                                                                     f"study_id = {study_id}") if r[0]}
        todo = [r for r in rows if r["node_name"].lower() not in existing]
        target_sr = arcpy.Describe(fc).spatialReference
        has_z = _has_z(fc)
        for r in todo:
            r["SHAPE@"] = _square(r.pop("x"), r.pop("y"), size_ft, model_sr, target_sr, has_z)
        edit = arcpy.da.Editor(gdb)
        edit.startEditing(False, False)
        edit.startOperation()
        try:
            _insert(fc, todo, extra_cols=("SHAPE@",))
            edit.stopOperation()
            edit.stopEditing(True)
        except Exception:
            try:
                edit.abortOperation()
                edit.stopEditing(False)
            except Exception:
                pass
            raise
        _recalc_extents([fc])
        arcpy.AddMessage(f"Added {len(todo)} placeholder detention boundaries ({size_ft:g} ft squares) "
                         f"for {len(rows)} SWMM storage nodes.")
        if len(todo) < len(rows):
            arcpy.AddMessage(f"  {len(rows) - len(todo)} storage nodes already had a boundary for this "
                             "study and were left as they are.")
        arcpy.AddMessage("Edit the squares to the facility outlines and fill lifecycle status, "
                         "ownership and as-built fields; the load tool (C) loads the detention results.")


class _ResultsLog(object):
    """Echoes tool messages to Pro and keeps them for a results .txt."""

    def __init__(self, title, inputs):
        import datetime
        self.title, self.inputs = title, inputs
        self.started = datetime.datetime.now()
        self.lines, self.warnings = [], []
        arcpy.AddMessage(title)

    def msg(self, text):
        arcpy.AddMessage(text)
        self.lines.append(text)

    def warn(self, text, items=None, cap=25, where="the results file"):
        """A QC note. Pro shows the first `cap` items; the results file lists them all."""
        items = list(items or [])
        shown = items[:cap]
        arcpy.AddWarning(text + (": " + "; ".join(shown) if shown else "")
                         + (f" ... (all {len(items)} in {where})" if len(items) > cap else ""))
        self.lines.append("QC: " + text + " (see QC notes)")
        self.warnings.append((text, items))

    def finish(self, note):
        """End without a file: a status line in Pro pointing to where the QC notes are."""
        if self.warnings:
            arcpy.AddWarning(f"COMPLETED WITH {len(self.warnings)} QC NOTE(S). {note}")
        else:
            arcpy.AddMessage(f"COMPLETED: everything loaded as intended. {note}")

    def write(self, path, failed=None):
        if failed is not None:
            status = f"FAILED: {failed}. Nothing was saved (the edit was rolled back)."
        elif self.warnings:
            status = (f"COMPLETED WITH {len(self.warnings)} QC NOTE(S): the load finished; review "
                      "the QC notes above before submitting.")
        else:
            status = "COMPLETED: everything loaded as intended, no QC notes."
        out = [self.title, f"Run: {self.started:%Y-%m-%d %H:%M}", "", "Inputs"]
        out += [f"  {k}: {v}" for k, v in self.inputs.items()]
        out += ["", "Results"] + [f"  {ln}" for ln in self.lines]
        out += ["", "QC notes"]
        for text, items in self.warnings:
            out.append(f"  - {text}")
            out += [f"      {i}" for i in items]
        if not self.warnings:
            out.append("  None")
        out += ["", "Status: " + status, ""]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(out))
        except OSError as e:
            arcpy.AddWarning(f"Could not write the results file {path}: {e}")
            return
        if self.warnings:
            arcpy.AddWarning(f"{len(self.warnings)} QC note(s). Open the results file for the full "
                             f"lists: {path}")
        arcpy.AddMessage(f"Results written to {path}")
        _open_file(path)


class LoadSubcatchments(object):
    """Subcatchment polygons with CUHP attributes, and the major-basin catchments."""

    def __init__(self):
        self.label = "B_Load Subcatchments and Catchments"
        self.description = ("Copies the subcatchment polygons into hydro_subcatchment with the "
                            "CUHP attributes, builds hydro_catchment (all subcatchments merged "
                            "into one study area polygon) with area and imperviousness rolled up "
                            "from CUHP, and adds both to the map.")
        self.canRunInBackground = False
        self._cuhp_cache = {}

    def getParameterInfo(self):
        gdb = arcpy.Parameter(displayName="Hydrology geodatabase (Built on MHFD v4.2)",
                              name="gdb", datatype="DEWorkspace",
                              parameterType="Required", direction="Input")
        study = arcpy.Parameter(displayName="MHFD study ID", name="study_id",
                                datatype="GPLong", parameterType="Required", direction="Input")
        sub_fc = arcpy.Parameter(displayName="Subcatchment polygons",
                                 name="subcatchments", datatype="GPFeatureLayer",
                                 parameterType="Required", direction="Input")
        sub_fc.filter.list = ["Polygon"]
        sub_name = arcpy.Parameter(displayName="Subcatchment name field (matches CUHP names)",
                                   name="name_field", datatype="Field",
                                   parameterType="Required", direction="Input")
        sub_name.parameterDependencies = [sub_fc.name]
        sub_name.filter.list = ["Text"]
        cuhp_p = _p_cuhp()
        cuhp_p.displayName = "CUHP workbook (.xlsm)"
        cuhp_p.parameterType = "Required"
        cond = arcpy.Parameter(displayName="CUHP workbook condition (only used when the Multiple "
                                           "Runs tab is blank)",
                               name="cuhp_condition", datatype="GPString",
                               parameterType="Required", direction="Input")
        cond.filter.type = "ValueList"
        cond.filter.list = ["Existing", "Future"]
        cond.value = "Existing"
        basin = arcpy.Parameter(displayName="Catchment name (i.e. Study Area name)",
                                name="catchment_name", datatype="GPString",
                                parameterType="Required", direction="Input")
        replace = arcpy.Parameter(displayName="Replace subcatchments and catchments already loaded "
                                              "for this study ID",
                                  name="replace", datatype="GPBoolean",
                                  parameterType="Optional", direction="Input")
        replace.value = True
        return [gdb, study, sub_fc, sub_name, cuhp_p, cond, basin, replace]

    def _cuhp_names(self, path):
        if path not in self._cuhp_cache:
            try:
                self._cuhp_cache[path] = [s.name for s in cuhp.read_cuhp(path)[0]]
            except Exception:
                self._cuhp_cache[path] = []
        return self._cuhp_cache[path]

    def _guess_name_field(self, layer, cuhp_path):
        fields = [f.name for f in arcpy.ListFields(layer) if getattr(f, "type", "String") == "String"]
        if not fields:
            return None
        names = self._cuhp_names(cuhp_path) if cuhp_path else []
        best, best_n = None, 0
        if names:
            for f in fields:
                vals = [r[0] for r in arcpy.da.SearchCursor(layer, [f])]
                n = len(staging.match_names(vals, names))
                if n > best_n:
                    best, best_n = f, n
        if best:
            return best
        return next((f for f in fields if "name" in f.lower()), fields[0])

    def updateParameters(self, params):
        changed = lambda p: p.altered and not p.hasBeenValidated  # noqa: E731
        if params[2].value and (changed(params[2]) or (changed(params[4]) and not params[3].altered)):
            guess = self._guess_name_field(params[2].valueAsText, params[4].valueAsText)
            if guess:
                params[3].value = guess

    def updateMessages(self, params):
        if params[0].value and not arcpy.Exists(os.path.join(params[0].valueAsText, "hydro_subcatchment")):
            params[0].setErrorMessage("Not an MHFD Hydrology GDB; missing hydro_subcatchment")

    def execute(self, params, messages):
        gdb, study_id = params[0].valueAsText, int(params[1].value)
        sub_layer, name_field = params[2].valueAsText, params[3].valueAsText
        cuhp_path = params[4].valueAsText
        condition = params[5].valueAsText or "Existing"
        cat_name = params[6].valueAsText
        replace = bool(params[7].value)
        T = {t: os.path.join(gdb, t) for t in ("hydro_subcatchment", "hydro_catchment")}
        log = _ResultsLog(f"{self.label} (MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION})", {})
        self._load(log, gdb, study_id, sub_layer, name_field, cuhp_path, condition, cat_name,
                   replace, T)
        log.finish("Tool C's QA report rechecks the subcatchments, so all QC notes end up in "
                   "that one report.")

    def _load(self, log, gdb, study_id, sub_layer, name_field, cuhp_path, condition, cat_name,
              replace, T):
        subs, _ = cuhp.read_cuhp(cuhp_path)
        by_name = {s.name: s for s in subs}
        target_sr = arcpy.Describe(T["hydro_subcatchment"]).spatialReference
        sub_z = _has_z(T["hydro_subcatchment"])
        polys = [(r[0], r[1]) for r in arcpy.da.SearchCursor(sub_layer, [name_field, "SHAPE@"])
                 if r[1] is not None]
        match = staging.match_names([n for n, _ in polys], list(by_name))
        no_cuhp = sorted({str(n) for n, _ in polys if n not in match})
        no_poly = sorted(set(by_name) - set(match.values()))
        if no_cuhp:
            log.warn(f"{len(no_cuhp)} polygons have no matching CUHP subcatchment and were "
                     "skipped", no_cuhp)
        if no_poly:
            log.warn(f"{len(no_poly)} CUHP subcatchments have no polygon", no_poly)
        sub_rows, area_off, seen = [], [], set()
        for name, geom in polys:
            if name not in match or match[name] in seen:
                continue
            sc = by_name[match[name]]
            seen.add(sc.name)
            geom = _to_target(geom, target_sr, sub_z)
            ac = geom.getArea("PLANAR", "ACRES")
            if sc.area_ac and abs(ac - sc.area_ac) / sc.area_ac > 0.01:
                area_off.append(f"{sc.name}: polygon {ac:.2f} ac, CUHP {sc.area_ac:.2f} ac")
            row = staging.subcatchment_row(None, sc, study_id, condition)
            row["SHAPE@"] = geom
            sub_rows.append((sc, row))
        if area_off:
            log.warn(f"{len(area_off)} polygons differ from the CUHP area by more than 1% "
                     "(check the polygons and CUHP workbook are the same delineation)", area_off)

        # Catchment: always one study area polygon, all subcatchments merged.
        cat_z = _has_z(T["hydro_catchment"])
        cats = []  # [catchment name, geometry, source]
        if sub_rows:
            merged = sub_rows[0][1]["SHAPE@"]
            for _, row in sub_rows[1:]:
                merged = merged.union(row["SHAPE@"])
            cats.append([cat_name, _to_target(merged, target_sr, cat_z),
                         "Merged from subcatchment polygons"])
        members = {0: list(sub_rows)} if cats else {}
        filled = {}

        edit = arcpy.da.Editor(gdb)
        edit.startEditing(False, False)
        edit.startOperation()
        try:
            if replace:
                for t in ("hydro_subcatchment", "hydro_catchment"):
                    n = _delete_study_rows(T[t], study_id)
                    if n:
                        log.msg(f"  Removed {n} existing rows from {t}.")
            cat_ids, have = {}, _fields(T["hydro_catchment"])
            fills = _required_fills(T["hydro_catchment"])
            for i, (name, geom, src) in enumerate(cats):
                roll = (staging.catchment_rollup([sc for sc, _ in members[i]], condition)
                        if members[i] else {})
                vals = dict(_tracking_values(T["hydro_catchment"]), **roll, study_id=study_id,
                            basin_name=name, source_file_path=src)
                cols = [have[k.lower()] for k in vals if k.lower() in have]
                low = _fill_blanks({k.lower(): v for k, v in vals.items()}, fills, filled,
                                   T["hydro_catchment"])
                with arcpy.da.InsertCursor(T["hydro_catchment"], ["SHAPE@"] + cols) as cur:
                    cat_ids[i] = cur.insertRow([geom] + [low[c.lower()] for c in cols])
                log.msg(f"Catchment '{name}': {roll.get('subbasin_count', 0)} subcatchments, "
                                 f"{roll.get('drainage_area_total_ac', 0)} ac.")
            _ensure_ids(T["hydro_catchment"], study_id)
            id_by_oid = {oid: cid for oid, cid in arcpy.da.SearchCursor(
                T["hydro_catchment"], ["OID@", "hydro_catchment_id"], f"study_id = {study_id}")}
            for i, mem in members.items():
                for _, row in mem:
                    row["hydro_catchment_id"] = id_by_oid.get(cat_ids[i])
            _insert(T["hydro_subcatchment"], [row for _, row in sub_rows], extra_cols=("SHAPE@",),
                    filled=filled)
            _ensure_ids(T["hydro_subcatchment"], study_id)
            edit.stopOperation()
            edit.stopEditing(True)
        except Exception:
            try:
                edit.abortOperation()
                edit.stopEditing(False)
            except Exception:
                pass
            raise
        log.msg(f"CUHP % impervious written as {condition} imperviousness"
                + (" (the Multiple Runs sheet gave both conditions where filled)." if any(
                    sc.impervious_existing_pct is not None or sc.impervious_future_pct is not None
                    for sc in subs) else "."))
        if filled:
            log.warn("These fields can't be blank in the geodatabase but CUHP has no value for "
                     "them, so they were filled with the field default (0 or empty when there is "
                     "none). Check them before submitting",
                     [f"{k} = {v!r}" for k, v in sorted(filled.items())])
        _recalc_extents([T["hydro_subcatchment"], T["hydro_catchment"]])
        try:
            _add_study_layers(gdb, study_id, cats[0][1].extent if cats else None,
                              fcs=("hydro_catchment", "hydro_subcatchment"))
        except Exception as e:  # the load itself succeeded; don't fail on map trouble
            log.warn(f"Loaded, but the layers could not be added to the map: {e}")
        log.msg(f"Loaded {len(sub_rows)} of {len(subs)} CUHP subcatchments and "
                f"{len(cats)} catchment(s).")
        written = sum(1 for _ in arcpy.da.SearchCursor(T["hydro_subcatchment"], ["OID@"],
                                                       f"study_id = {study_id}"))
        if written != len(sub_rows):
            log.warn(f"Read-back mismatch: {written} subcatchment rows in the GDB for this study, "
                     f"expected {len(sub_rows)}.")
        else:
            log.msg(f"Confirmed {written} subcatchment rows in the GDB for study {study_id}.")


class TopologyCheck(object):
    """Subcatchment and network topology checks on a study already in the GDB."""

    def __init__(self):
        self.label = "Check Topology"
        self.description = ("Checks a study in the Hydrology GDB: subcatchments with no overlaps, "
                            "gaps or holes (boundaries coincident), and design points snapped to "
                            "routing elements with no dangles. Writes the problems as points to "
                            "review on the map.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        gdb = arcpy.Parameter(displayName="Hydrology geodatabase (Built on MHFD v4.2)",
                              name="gdb", datatype="DEWorkspace",
                              parameterType="Required", direction="Input")
        study = arcpy.Parameter(displayName="MHFD study ID", name="study_id",
                                datatype="GPLong", parameterType="Required", direction="Input")
        tol = arcpy.Parameter(displayName="Snapping tolerance (ft)", name="tolerance_ft",
                              datatype="GPDouble", parameterType="Required", direction="Input")
        tol.value = 0.1
        sliver = arcpy.Parameter(displayName="Ignore overlaps and gaps smaller than (sq ft)",
                                 name="min_area_sqft", datatype="GPDouble",
                                 parameterType="Required", direction="Input")
        sliver.value = 1.0
        errs = arcpy.Parameter(displayName="Error points (blank = MHFD_topology_errors in the "
                                           "scratch geodatabase)",
                               name="error_points", datatype="DEFeatureClass",
                               parameterType="Optional", direction="Output")
        report = arcpy.Parameter(displayName="Topology report (.md) (blank = messages only)",
                                 name="report", datatype="DEFile", parameterType="Optional",
                                 direction="Output")
        report.filter.list = ["md", "txt"]
        return [gdb, study, tol, sliver, errs, report]

    def updateMessages(self, params):
        if params[0].value:
            missing = [t for t in ("hydro_subcatchment", "hydro_design_point", "hydro_routing_element")
                       if not arcpy.Exists(os.path.join(params[0].valueAsText, t))]
            if missing:
                params[0].setErrorMessage("Not an MHFD Hydrology GDB; missing " + ", ".join(missing))

    def execute(self, params, messages):
        gdb, study_id = params[0].valueAsText, int(params[1].value)
        tol_ft, min_sqft = float(params[2].value), float(params[3].value)
        arcpy.AddMessage(f"{self.label} (MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION})")
        errors = _check_topology(gdb, study_id, tol_ft, min_sqft, params[4].valueAsText)
        counts = {}
        for check, *_ in errors:
            counts[check] = counts.get(check, 0) + 1
        all_checks = TOPOLOGY_CHECKS
        if params[5].value:
            with open(params[5].valueAsText, "w", encoding="utf-8") as fh:
                fh.write(f"# Topology check: study {study_id} ({self.label})\n\n"
                         "| Check | Result | Count |\n|---|---|---|\n")
                for check in all_checks:
                    n = counts.get(check, 0)
                    fh.write(f"| {check} | {'❌ FAIL' if n else '✅ PASS'} | {n} |\n")
                for check in all_checks:
                    items = [d for c, d, _, _ in errors if c == check]
                    if items:
                        fh.write(f"\n## {check}\n\n" + "".join(f"- {d}\n" for d in items[:200]))
            arcpy.AddMessage(f"Topology report written to {params[5].valueAsText}")


TOPOLOGY_CHECKS = ["Overlap", "Gap", "Multipart subcatchment", "Dangle", "Wrong end point",
                   "Unconnected point", "Stacked points"]


def _check_topology(gdb, study_id, tol_ft=0.1, min_sqft=1.0, out_points=None):
    """Subcatchment and design point / routing element topology for one study.

    Returns [(check, detail, x, y)]; writes them as error points (added to the
    open map) when there are any.
    """
    where = f"study_id = {study_id}"
    sr = arcpy.Describe(os.path.join(gdb, "hydro_design_point")).spatialReference
    ft = getattr(sr, "metersPerUnit", 0.3048006096012192) / 0.3048006096012192 or 1.0
    tol, min_area = tol_ft / ft, min_sqft / ft ** 2  # in map units
    errors = []  # (check, detail, x, y)

    # Subcatchments: overlaps between neighbours, gaps (holes in their union), multipart.
    sub_fc = os.path.join(gdb, "hydro_subcatchment")
    subs = [(r[0], r[1]) for r in arcpy.da.SearchCursor(sub_fc, ["subcatchment_name", "SHAPE@"], where)
            if r[1] is not None] if arcpy.Exists(sub_fc) else []
    arcpy.AddMessage(f"Checking {len(subs)} subcatchment polygons...")
    for name, g in subs:
        if getattr(g, "partCount", 1) > 1:
            pt = g.labelPoint
            errors.append(("Multipart subcatchment", f"{name} has {g.partCount} separate pieces.",
                           pt.X, pt.Y))
    ext = [(g.extent.XMin, g.extent.YMin, g.extent.XMax, g.extent.YMax) for _, g in subs]
    for i in range(len(subs)):
        for j in range(i + 1, len(subs)):
            a, b = ext[i], ext[j]
            if a[0] > b[2] or b[0] > a[2] or a[1] > b[3] or b[1] > a[3]:
                continue
            if subs[i][1].disjoint(subs[j][1]):
                continue
            inter = subs[i][1].intersect(subs[j][1], 4)
            if inter is not None and inter.area > min_area:
                pt = inter.labelPoint
                errors.append(("Overlap", f"{subs[i][0]} and {subs[j][0]} overlap by "
                                          f"{inter.area * ft ** 2:,.1f} sq ft.", pt.X, pt.Y))
    if subs:
        merged = subs[0][1]
        for _, g in subs[1:]:
            merged = merged.union(g)
        for ring in topology.interior_rings(merged):
            area = topology.ring_area(ring)
            if area > min_area:
                x, y = topology.ring_center(ring)
                errors.append(("Gap", f"Gap or hole of {area * ft ** 2:,.1f} sq ft between or "
                                      "inside subcatchments.", x, y))
    else:
        arcpy.AddMessage("No subcatchment polygons for this study; run tool B to load them.")

    # Design points and routing elements.
    points = [(r[0], r[1], r[2].firstPoint.X, r[2].firstPoint.Y) for r in arcpy.da.SearchCursor(
        os.path.join(gdb, "hydro_design_point"), ["hydro_design_point_id", "swmm_node_name", "SHAPE@"],
        where) if r[2] is not None]
    lines = []
    for rid, name, a, b, g in arcpy.da.SearchCursor(
            os.path.join(gdb, "hydro_routing_element"),
            ["hydro_routing_element_id", "routing_name", "from_design_point_id",
             "to_design_point_id", "SHAPE@"], where):
        if g is not None:
            lines.append((rid, name, a, b, [(g.firstPoint.X, g.firstPoint.Y),
                                            (g.lastPoint.X, g.lastPoint.Y)]))
    arcpy.AddMessage(f"Checking {len(points)} design points and {len(lines)} routing elements...")
    errors += topology.check_network(points, lines, tol)

    counts = {}
    for check, *_ in errors:
        counts[check] = counts.get(check, 0) + 1
    for check in TOPOLOGY_CHECKS:
        n = counts.get(check, 0)
        (arcpy.AddWarning if n else arcpy.AddMessage)(f"[{'FAIL' if n else 'PASS'}] {check}: {n}")
    for check, detail, _, _ in errors[:50]:
        arcpy.AddMessage(f"  {check}: {detail}")
    if len(errors) > 50:
        arcpy.AddMessage(f"  ... and {len(errors) - 50} more (see the error points).")

    if errors:
        out = out_points or os.path.join(arcpy.env.scratchGDB,
                                                    f"MHFD_topology_errors_{study_id}")
        _write_error_points(out, sr, errors)
        arcpy.AddMessage(f"Wrote {len(errors)} error points to {out}")
        try:
            m = arcpy.mp.ArcGISProject("CURRENT").activeMap
            if m is not None:
                m.addDataFromPath(out)
        except Exception:
            pass
    else:
        arcpy.AddMessage("No topology problems found.")
    return errors


def _write_error_points(path, sr, errors):
    folder, name = os.path.split(path)
    if arcpy.Exists(path):
        arcpy.management.Delete(path)
    arcpy.management.CreateFeatureclass(folder, name, "POINT", spatial_reference=sr)
    arcpy.management.AddField(path, "check_type", "TEXT", field_length=40)
    arcpy.management.AddField(path, "detail", "TEXT", field_length=255)
    with arcpy.da.InsertCursor(path, ["SHAPE@XY", "check_type", "detail"]) as cur:
        for check, detail, x, y in errors:
            cur.insertRow([(x, y), check, detail[:255]])


class HydrologyQAReport(object):
    def __init__(self):
        self.label = "Hydrology QA Report"
        self.description = "Runs the MHFD hydrology QA checks on a CUHP/SWMM model set without writing to a GDB."
        self.canRunInBackground = False

    def getParameterInfo(self):
        return [_p_inp(), _p_runs(), _p_cuhp(), _p_drf(), _p_qa_out(required="Required"),
                _p_engine(), _p_run_dir()] + _p_builds()

    def updateParameters(self, params):
        _fill_runs_from_names(params[1])
        if len(params) > 6:
            _show_full_path(params[6])
        if len(params) > 8:
            _update_builds(params[0], params[1], params[5], params[7], params[8])
        if params[0].value and not params[4].altered:
            params[4].value = os.path.splitext(params[0].valueAsText)[0] + "_QA.txt"

    def updateMessages(self, params):
        _runs_messages(params[1])
        if len(params) > 8:
            _build_messages(params[7], params[8], params[5])

    def execute(self, params, messages):
        _run_qa(params[0].valueAsText, _rows(params[1]), params[2].valueAsText,
                params[3].valueAsText, params[4].valueAsText, tool_label=self.label,
                engine_path=params[5].valueAsText, run_dir=params[6].valueAsText)


class ExportSwmmLayers(object):
    """SWMM node and link feature classes (Dividers, Junctions, Outfalls,
    Storage, Conduits, Orifices, Outlets, Weirs) required next to the GDB."""

    LAYERS = [("Junctions", "JUNCTION", "POINT"), ("Dividers", "DIVIDER", "POINT"),
              ("Outfalls", "OUTFALL", "POINT"), ("Storage", "STORAGE", "POINT"),
              ("Conduits", "CONDUIT", "POLYLINE"), ("Orifices", "ORIFICE", "POLYLINE"),
              ("Outlets", "OUTLET", "POLYLINE"), ("Weirs", "WEIR", "POLYLINE"),
              ("Pumps", "PUMP", "POLYLINE")]

    def __init__(self):
        self.label = "Export SWMM Layers"
        self.description = "Writes the SWMM node and link layers MHFD requires to a file geodatabase."
        self.canRunInBackground = False

    def getParameterInfo(self):
        out = arcpy.Parameter(displayName="Output geodatabase", name="out_gdb", datatype="DEWorkspace",
                              parameterType="Required", direction="Input")
        sr = arcpy.Parameter(displayName="SWMM model coordinate system", name="model_sr",
                             datatype="GPCoordinateSystem", parameterType="Optional", direction="Input")
        sr.value = arcpy.SpatialReference(C.DEFAULT_WKID).exportToString()
        return [_p_inp(), out, sr]

    def updateParameters(self, params):
        if params[0].altered and not params[0].hasBeenValidated and params[0].value:
            epsg = _pcswmm_epsg(params[0].valueAsText)
            if epsg and epsg.isdigit():
                params[2].value = arcpy.SpatialReference(int(epsg)).exportToString()

    def execute(self, params, messages):
        inp, gdb = params[0].valueAsText, params[1].valueAsText
        sr = arcpy.SpatialReference()
        sr.loadFromString(params[2].valueAsText) if params[2].value else None
        if not sr.name:
            sr = arcpy.SpatialReference(C.DEFAULT_WKID)
        model = swmm_inp.read_inp(inp)
        for fc_name, kind, geom in self.LAYERS:
            if geom == "POINT":
                items = [n for n in model.nodes.values() if n.kind == kind]
            else:
                items = [lk for lk in model.links.values() if lk.kind == kind]
            if not items:
                continue
            fc = os.path.join(gdb, fc_name)
            if arcpy.Exists(fc):
                arcpy.management.Delete(fc)
            arcpy.management.CreateFeatureclass(gdb, fc_name, geom, spatial_reference=sr)
            arcpy.management.AddField(fc, "name", "TEXT", field_length=64)
            arcpy.management.AddField(fc, "tag", "TEXT", field_length=255)
            if geom == "POINT":
                arcpy.management.AddField(fc, "invert_elev_ft", "DOUBLE")
                arcpy.management.AddField(fc, "max_depth_ft", "DOUBLE")
                arcpy.management.AddField(fc, "parameters", "TEXT", field_length=255)
                with arcpy.da.InsertCursor(fc, ["SHAPE@XY", "name", "tag", "invert_elev_ft",
                                                "max_depth_ft", "parameters"]) as cur:
                    for n in items:
                        cur.insertRow([(n.x, n.y), n.name, n.tag, n.invert, n.max_depth,
                                       " ".join(n.params)[:255]])
            else:
                arcpy.management.AddField(fc, "from_node", "TEXT", field_length=64)
                arcpy.management.AddField(fc, "to_node", "TEXT", field_length=64)
                arcpy.management.AddField(fc, "parameters", "TEXT", field_length=255)
                with arcpy.da.InsertCursor(fc, ["SHAPE@", "name", "tag", "from_node", "to_node",
                                                "parameters"]) as cur:
                    for lk in items:
                        coords = model.link_geometry(lk)
                        if coords is None:
                            continue
                        line = arcpy.Polyline(arcpy.Array([arcpy.Point(x, y) for x, y in coords]), sr)
                        cur.insertRow([line, lk.name, lk.tag, lk.from_node, lk.to_node,
                                       " ".join(lk.params)[:255]])
            arcpy.AddMessage(f"{fc_name}: {len(items)} features")


class RunSwmmStorms(object):
    """One SWMM run per CUHP interface file, for when the .rpt files don't exist yet."""

    def __init__(self):
        self.label = "A_Generate SWMM rpt files for all storms"
        self.description = ("Runs the SWMM model once per CUHP interface file and writes one "
                            ".rpt per condition and storm, named so the other tools read the "
                            "condition and storm from the file name. Tool C's QA report checks "
                            "the runs.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        keep = arcpy.Parameter(displayName="Keep SWMM binary output (.out) files",
                               name="keep_out", datatype="GPBoolean",
                               parameterType="Optional", direction="Input")
        keep.value = False
        return [_p_inp(), _p_runs(interface_only=True), _p_run_dir(), _p_engine(), keep] + _p_builds()

    def updateParameters(self, params):
        _fill_runs_from_names(params[1])
        _show_full_path(params[2])
        if len(params) > 6:
            _update_builds(params[0], None, params[3], params[5], params[6])

    def updateMessages(self, params):
        _runs_messages(params[1])
        if len(params) > 6:
            _build_messages(params[5], params[6], params[3])

    def execute(self, params, messages):
        inp = params[0].valueAsText
        storms = _rows(params[1])
        arcpy.AddMessage(f"{self.label} (MHFD SWMM to Hydrology GDB Converter v{TOOLBOX_VERSION})")
        run_dir = params[2].valueAsText or _default_run_dir(inp)
        engine = swmm_run.find_engine(params[3].valueAsText)
        arcpy.AddMessage(f"SWMM engine: {engine[1] or 'swmm-toolkit'}, build {swmm_run.engine_build(engine)}")
        mb, msrc = _model_build(inp)
        arcpy.AddMessage(f"Model was made with SWMM {mb or 'unknown'}"
                         + (f" (from {os.path.basename(msrc)})" if msrc else ""))
        done = swmm_run.run_storms(inp, storms, run_dir, engine, keep_out=bool(params[4].value),
                                   log=arcpy.AddMessage)
        for rpt, cond, rp, area in done:
            arcpy.AddMessage(f"  Wrote {rpt}")
        # A quick look at each run here; the full checks go in tool C's QA report.
        model, runs, reports, _, _, _ = _read_inputs(inp, [list(d) for d in done], None, None)
        checks = qa.check_reports(runs, reports)
        for f in checks:
            (arcpy.AddWarning if f.status in (qa.FAIL, qa.WARN) else arcpy.AddMessage)(
                f"[{f.status}] {f.check}: {f.detail}")
        arcpy.AddMessage("Next: run B and C. Tool C's QA report covers these runs in full.")
