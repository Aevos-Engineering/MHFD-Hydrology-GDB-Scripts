"""End-to-end tests on the Brighton South Outfall model.

Run:  python -m pytest tests -q   (from the mhfd_hydro folder)

Needs the South Outfall files; set MHFD_TEST_DATA to the folder holding
'USE THIS ONE/*.inp', the CUHP workbook and the per-storm .rpt files, or the
tests are skipped. The GDB writer is exercised against tests/fake_arcpy.py,
which mimics the template's attribute rules, so this checks the loader's
logic, not ArcGIS itself.
"""

import glob
import importlib.machinery
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

DATA = os.environ.get("MHFD_TEST_DATA", "")
INP = os.path.join(DATA, "USE THIS ONE", "Brighton_South_Outfall_Existing_Conditions.inp")
PCSWMM_RPT = os.path.join(DATA, "USE THIS ONE", "Brighton_South_Outfall_Existing_Conditions.rpt")
CUHP = os.path.join(DATA, "CUHP", "BSO_CUHP_v201_DRCOG_working", "BSO_CUHP_v1_NH.xlsm")
RPTS = sorted(glob.glob(os.path.join(DATA, "runs", "BSO_Ex_*yr.rpt")))
TEMPLATE = os.environ.get("MHFD_GDB_TEMPLATE", "")

pytestmark = pytest.mark.skipif(not os.path.exists(INP), reason="South Outfall data not available")

from mhfd_hydro import constants as C, qa, staging  # noqa: E402
from mhfd_hydro.cuhp import read_cuhp  # noqa: E402
from mhfd_hydro.swmm_inp import read_inp  # noqa: E402
from mhfd_hydro.swmm_rpt import read_rpt  # noqa: E402


@pytest.fixture(scope="module")
def model():
    return read_inp(INP)


def test_inp_counts(model):
    kinds = [n.kind for n in model.nodes.values()]
    assert len(model.nodes) == 474
    assert kinds.count("JUNCTION") == 354 and kinds.count("STORAGE") == 50
    assert len(model.links) == 514
    assert all(n.x is not None for n in model.nodes.values())
    assert model.node("BASIN094").name == "Basin094"  # case-insensitive like SWMM


def test_rpt_parse():
    r = read_rpt(PCSWMM_RPT)
    assert r.version == "5.2"
    assert r.routing_continuity_pct == pytest.approx(-0.637)
    assert len(r.node_inflow) == 474
    assert r.inflow("144th").max_total_cfs == pytest.approx(161.42)
    assert r.inflow("144th").total_volume_ac_ft == pytest.approx(44.6 * 3.0689, rel=1e-3)
    assert r.storage["27J"].max_volume_ac_ft == pytest.approx(121.778 * 1000 / 43560)


def test_storage_volume_matches_swmm(model):
    from mhfd_hydro.swmm_inp import stage_storage_table
    # SWMM: 27J max 121.778 kcf at 52.4% full -> full = 232.4 kcf = 5.335 ac-ft
    full = stage_storage_table(model, model.nodes["27J"])[-1]["volume_ac_ft"]
    assert full == pytest.approx(121.778 / 0.524 * 1000 / 43560, rel=0.005)


def test_runspec_names():
    s = staging.RunSpec.from_filename("1_Ex_100yr_5mi^2_Gregory Canyon_17.rpt")
    assert (s.condition, s.return_period, s.correction_area_sq_mi) == ("Existing", "100", 5.0)
    s = staging.RunSpec.from_filename("BSO_Fut_010yr.rpt")
    assert (s.condition, s.return_period) == ("Future", "10")
    with pytest.raises(ValueError):
        staging.RunSpec.from_filename("model.rpt")
    assert C.normalize_return_period("Q100") == "100"
    assert C.normalize_return_period("1% plus") == "1%+"


@pytest.fixture(scope="module")
def staged(model):
    if not RPTS:
        pytest.skip("per-storm runs not available")
    runs = [staging.RunSpec.from_filename(p) for p in RPTS]
    reports = {r.rpt_path: read_rpt(r.rpt_path) for r in runs}
    subs, _ = read_cuhp(CUHP)
    st = staging.build(model, runs, reports, 99, subcatchments=subs)
    return model, runs, reports, subs, st


def test_staging_shapes(staged):
    model, runs, reports, subs, st = staged
    assert len(st.design_points) == 474
    assert len(st.routing_elements) == 514
    assert len(st.peak_flows) == 474  # one condition
    out = next(p for p in st.peak_flows if p["swmm_node_name"] == "Outfall")
    for rp in ("2", "5", "10", "50", "100"):
        assert out[C.PEAK_FLOW_FIELD[rp]] > 0


def test_contributing_area_full_upstream(staged):
    model, runs, reports, subs, st = staged
    total = sum(s.area_sq_mi for s in subs)
    missing = sum(s.area_sq_mi for s in subs if model.node(s.target_node) is None)
    area = {p["swmm_node_name"]: p["contributing_drainage_area_sq_mi"] for p in st.peak_flows}
    # Split branches carry the full upstream area, so the lower-flow side of a divider isn't 0.
    assert area["O10"] > 0 and area["46-350"] == area["J080"]
    assert max(area.values()) <= total - missing + 1e-3
    zero = sorted(n for n, a in area.items() if not a)
    # No CUHP area upstream: direct inflows only, or below the empty PALIZZI pond.
    assert zero == ["73-130", "74-176P", "BF_Interceptor", "Brine", "DetPondY", "PALIZZI"]
    f = {x.check: x for x in qa.run_all(model, runs, reports, st, subs)}
    assert f["Drainage area balance"].status == qa.WARN  # Basin098A target node missing


def test_qa_flags_known_issues(staged):
    model, runs, reports, subs, st = staged
    f = {x.check: x for x in qa.run_all(model, runs, reports, st, subs)}
    assert f["CUHP target nodes in SWMM"].status == qa.FAIL
    assert any("Basin098A" in i for i in f["CUHP target nodes in SWMM"].items)
    assert f["Subcatchment size"].status == qa.PASS and len(f["Subcatchment size"].items) == 8  # over 130 ac: listed, not a fail
    assert f["SWMM georeferenced"].status == qa.PASS


# --- loader against fake arcpy -------------------------------------------

def _load_pyt():
    import fake_arcpy
    sys.modules["arcpy"] = fake_arcpy
    path = os.path.join(ROOT, "MHFD_SWMM_to_Hydrology_GDB_Converter_v1.0.pyt")
    loader = importlib.machinery.SourceFileLoader("mhfd_loader", path)
    spec = importlib.util.spec_from_loader("mhfd_loader", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod, fake_arcpy


def _template_fields():
    import pyogrio
    out = {}
    for name, geom in pyogrio.list_layers(TEMPLATE):
        out[name] = (list(pyogrio.read_info(TEMPLATE, layer=name)["fields"]), geom)
    return out


@pytest.mark.skipif(not os.path.exists(TEMPLATE), reason="GDB template not available")
@pytest.mark.parametrize("peak_key", ["hydro_design_point_id", "GlobalID"])
def test_loader_end_to_end(staged, peak_key, tmp_path, monkeypatch):
    model, runs, reports, subs, st = staged
    mod, fa = _load_pyt()
    fa.reset()
    gdb = "C:/fake/Hydrology.gdb"
    for name, (fields, geom) in _template_fields().items():
        fa.add_table(f"{gdb}/{name}", fields, 6428 if geom else None, z="Z" in (geom or ""))
    T = lambda n: fa.TABLES[f"{gdb}/{n}".lower()]["rows"]  # noqa: E731
    fore = "hydro_design_point_id" if peak_key == "hydro_design_point_id" else "rel_globalid"
    if fore == "rel_globalid":  # simulate a GlobalID-keyed relationship
        fa.TABLES[f"{gdb}/hydro_peak_flow".lower()]["fields"].append("rel_globalid")
    fa.RELATIONSHIPS += [
        dict(origin="hydro_design_point", destination="hydro_peak_flow", prim=peak_key, fore=fore),
        dict(origin="hydro_design_point", destination="hydro_runoff_volume",
             prim="hydro_design_point_id", fore="hydro_design_point_id")]
    # One boundary the user already drew; the load adds placeholders for the rest.
    with fa.da.InsertCursor(f"{gdb}/hydro_detention_boundary", ["study_id", "node_name", "comments"]) as c:
        c.insertRow([99, "27J", "drawn by user"])

    # A NOAA Atlas 14 point picked as a layer.
    noaa = "C:/fake/noaa_point"
    fa.add_table(noaa, [], 6428)
    with fa.da.InsertCursor(noaa, ["SHAPE@"]) as c:
        c.insertRow([fa.PointGeometry(fa.Point(3170000, 1720000), fa.SpatialReference(6428))])

    # Like Jeremy's GDB: the attribute rule on detention boundaries doesn't fire.
    monkeypatch.delitem(fa.AUTO_IDS, "hydro_detention_boundary")
    tool = mod.LoadSwmmResults()
    assert tool.label == "C_Load SWMM Results to Hydrology GDB"
    p = tool.getParameterInfo()
    assert len(p) == 16 and "Master Plan, LOMR" in p[11].displayName and p[9].value == 100
    assert p[10].value == "NAVD88"
    p[0].value = gdb
    p[1].value = INP
    p[2].values = [[r.rpt_path, r.condition, r.return_period, 0] for r in runs]
    p[3].value = 99
    p[4].value = CUHP
    p[6].value = "6428"
    p[7].value = True
    p[8].value = True
    p[12].value = noaa
    qa_txt = tmp_path / "C_QA.txt"
    p[13].value = str(qa_txt)
    fa.MAP = fa._Map()
    for _ in range(2):  # second run must replace, not duplicate
        tool.execute(p, None)

    bounds = T("hydro_detention_boundary")  # second run kept them instead of duplicating
    assert len(bounds) == 50 and sum(r["comments"] == "drawn by user" for r in bounds) == 1
    ph = next(r for r in bounds if r["node_name"] == "DetBasinK")
    assert ph["comments"].startswith("PLACEHOLDER") and ph["ownership_type_id"] == 2
    assert ph["upstream_drainage_area_ac"] > 0 and ph["node_name_downstream"]
    assert ph["SHAPE"].getArea("PLANAR", "SQUAREFEET") == pytest.approx(100 * 100)
    det_ids = [r["hydro_detention_id"] for r in bounds]
    assert all(det_ids) and len(set(det_ids)) == 50  # filled by the tool, unique
    assert ph["SHAPE"].hasZ  # the template's detention boundary stores Z
    dps = T("hydro_design_point")
    assert len(dps) == 474 and all(r["hydro_design_point_id"] for r in dps)
    ids = {r["hydro_design_point_id"] for r in dps}
    gids = {r["GlobalID"] for r in dps}
    res = T("hydro_routing_element")
    assert len(res) == 514
    assert all(r["from_design_point_id"] in ids and r["to_design_point_id"] in ids for r in res)
    conduits = {lk.name for lk in model.links.values() if lk.kind == "CONDUIT"}
    assert all(r["comments"] is None for r in res if r["routing_name"] in conduits)
    assert any(r["comments"] == "SWMM weir" for r in res)
    pf = T("hydro_peak_flow")
    assert len(pf) == 474 and all(r["hydro_design_point_id"] in ids for r in pf)
    if fore == "rel_globalid":
        assert all(r["rel_globalid"] in gids for r in pf)
    rv = T("hydro_runoff_volume")
    assert len(rv) == 474 and all(r["rel_globalid"] in gids for r in rv)
    summ = T("hydro_detention_summary")
    assert len(summ) == 50 and all(r["volume_max_100yr_ac_ft"] is not None for r in summ)
    curves = T("hydro_detention_rating_curve")
    assert {r["hydro_detention_id"] for r in curves} <= {r["hydro_detention_id"] for r in summ}
    assert len({r["hydro_detention_id"] for r in curves}) == 50
    assert all(r["vertical_datum"] == "NAVD88" for r in curves)
    assert {r["hydro_detention_id"] for r in summ} <= set(det_ids)
    assert all(r["hydro_design_point_id"] in ids for r in T("hydro_detention_boundary"))
    pts = T("hydro_point_precipitation")
    rain = T("hydro_hour_rainfall")
    assert len(pts) == 1 and len(rain) == 5
    r100 = next(r for r in rain if r["return_period_year_type_id"] == 8)
    assert (r100["one_hour_precip_in"], r100["six_hour_precip_in"]) == (2.56, 3.63)
    assert r100["hydro_point_precip_id"] == pts[0]["hydro_point_precip_id"]
    assert f"{gdb}/hydro_design_point" in fa.EXTENTS_RECALCULATED
    msgs = [m for _, m in fa.MESSAGES]
    # Every hydro layer and table on the map once (reruns replace them), filtered to the study.
    names = sorted(os.path.basename(l.dataSource) for l in fa.MAP.layers)
    assert names == sorted(mod.ALL_LAYERS)
    assert sorted(t.name for t in fa.MAP.tables) == sorted(mod.ALL_TABLES)
    assert all(x.definitionQuery == "study_id = 99" for x in fa.MAP.layers + fa.MAP.tables)
    dp_layer = next(l for l in fa.MAP.layers if l.dataSource.endswith("hydro_design_point"))
    import json
    assert '"size": 7.2' in json.dumps(dp_layer.doc)  # 18 pt markers shrunk to 40%
    assert any("still placeholders" in m for m in msgs)
    assert any("No topology problems found." in m for m in msgs)  # topology runs at the end of the load
    assert not any("no detention boundary" in m for k, m in fa.MESSAGES if k == "warn")

    report = qa_txt.read_text(encoding="utf-8")
    assert "[PASS] Related records" in report and "[PASS] Hour rainfall" in report

    # Without a point, an existing point for the study is reused; with none, the
    # hour rainfall still loads and the QA report fails it.
    p[12].value = None
    tool.execute(p, None)
    assert len(T("hydro_hour_rainfall")) == 5
    fa.TABLES[f"{gdb}/hydro_point_precipitation".lower()]["rows"].clear()
    fa.MESSAGES.clear()
    tool.execute(p, None)
    rain = T("hydro_hour_rainfall")
    assert len(rain) == 5 and all(r["hydro_point_precip_id"] is None for r in rain)
    assert "[FAIL] Hour rainfall" in qa_txt.read_text(encoding="utf-8")


@pytest.mark.skipif(not os.path.exists(TEMPLATE), reason="GDB template not available")
def test_subcatchments_and_catchments(tmp_path):
    mod, fa = _load_pyt()
    fa.reset()
    gdb = "C:/fake/Hydrology.gdb"
    for name, (fields, geom) in _template_fields().items():
        fa.add_table(f"{gdb}/{name}", fields, 6428 if geom else None, z="Z" in (geom or ""))
    subs, _ = read_cuhp(CUHP)
    # Consultant shapefile: a square per CUHP subcatchment sized to its CUHP area, names in
    # upper case to test matching, one extra polygon and one subcatchment left out.
    shp = "C:/fake/consultant_subcatchments"
    fa.add_table(shp, ["BASIN_ID", "NOTE"], 6428)
    with fa.da.InsertCursor(shp, ["BASIN_ID", "NOTE", "SHAPE@"]) as c:
        for i, sc in enumerate(subs[1:]):
            side = (sc.area_ac * 43560) ** 0.5 * (1.05 if i == 0 else 1.0)
            x0 = 3_100_000 + (i % 20) * 6000
            y0 = 1_700_000 + (i // 20) * 6000
            ring = [fa.Point(x0, y0), fa.Point(x0, y0 + side), fa.Point(x0 + side, y0 + side),
                    fa.Point(x0 + side, y0)]
            c.insertRow([sc.name.upper(), "x", fa.Polygon(fa.Array(ring), fa.SpatialReference(6428))])
        c.insertRow(["NOT_IN_CUHP", "x", fa.Polygon(fa.Array([fa.Point(0, 0), fa.Point(0, 1), fa.Point(1, 1)]),
                                                     fa.SpatialReference(6428))])
    # Like Jeremy's GDB: future imperviousness can't be blank, and this CUHP has none.
    fa.NOT_NULL[(f"{gdb}/hydro_catchment".lower(), "impervious_future_pct")] = "Double"
    fa.NOT_NULL[(f"{gdb}/hydro_subcatchment".lower(), "imperviousness_future_pct")] = "Double"
    tool = mod.LoadSubcatchments()
    assert tool.label == "B_Load Subcatchments and Catchments"
    p = tool.getParameterInfo()
    p[0].value, p[1].value = gdb, 99
    p[2].value, p[2].altered = shp, True
    p[4].value, p[4].altered = CUHP, True
    tool.updateParameters(p)
    assert p[3].value == "BASIN_ID"  # found by matching CUHP names
    assert p[5].value == "Existing" and p[5].filter.list == ["Existing", "Future"]
    assert "Catchment name" in p[6].displayName and p[6].parameterType == "Required"
    p[6].value = "South Outfall"
    fa.MAP = fa._Map()
    for _ in range(2):
        tool.execute(p, None)
    # Both layers on the map once (the rerun replaces them), filtered to the study.
    assert sorted(l.name for l in fa.MAP.layers) == ["hydro_catchment", "hydro_subcatchment"]
    assert all(l.definitionQuery == "study_id = 99" for l in fa.MAP.layers)
    T = lambda n: fa.TABLES[f"{gdb}/{n}".lower()]["rows"]  # noqa: E731
    sc_rows, cats = T("hydro_subcatchment"), T("hydro_catchment")
    assert len(sc_rows) == len(subs) - 1 and len(cats) == 1
    cat = cats[0]
    assert cat["basin_name"] == "South Outfall" and cat["subbasin_count"] == len(subs) - 1
    assert cat["drainage_area_total_ac"] == pytest.approx(sum(s.area_ac for s in subs[1:]), abs=0.05)
    assert all(r["hydro_catchment_id"] == cat["hydro_catchment_id"] for r in sc_rows)
    assert {r["subcatchment_name"] for r in sc_rows} == {s.name for s in subs[1:]}  # CUHP spelling
    one = next(r for r in sc_rows if r["subcatchment_name"] == subs[2].name)
    assert one["area_ac"] == pytest.approx(subs[2].area_ac, abs=1e-3) and one["slope_ftft"] == subs[2].slope_ftft
    warns = " ".join(m for k, m in fa.MESSAGES if k == "warn")
    assert "NOT_IN_CUHP" in warns and subs[0].name in warns and subs[1].name in warns  # area 1% off
    assert "vertices" not in warns and len(p) == 8  # no simplification checks in this tool
    assert cat["impervious_future_pct"] == 0 and all(r["imperviousness_future_pct"] == 0 for r in sc_rows)
    assert "hydro_catchment.impervious_future_pct = 0" in warns
    msgs = " ".join(m for _, m in fa.MESSAGES)
    assert f"Confirmed {len(subs) - 1} subcatchment rows" in msgs
    assert "COMPLETED WITH" in warns and not list(tmp_path.iterdir())  # no separate results file
    assert cats[0]["SHAPE"].hasZ  # hydro_catchment stores Z
    assert cat["created_user"] and cat["created_date"]  # editor tracking off: the tool fills them

    # A Future-condition workbook: the CUHP % impervious goes to the future fields.
    p[5].value = "Future"
    tool.execute(p, None)
    sc_rows, cat = T("hydro_subcatchment"), T("hydro_catchment")[0]
    one = next(r for r in sc_rows if r["subcatchment_name"] == subs[2].name)
    assert one["imperviousness_future_pct"] == subs[2].impervious_pct
    assert one["imperviousness_exisiting_pct"] is None
    assert cat["impervious_future_pct"] > 0 and cat["impervious_existing_pct"] is None


# --- running SWMM from CUHP interface files ---------------------------------

IFACE_DIR = os.path.join(DATA, "CUHP", "BSO_CUHP_v201_DRCOG_working")


def test_point_inp_at_interface():
    from mhfd_hydro.swmm_run import point_inp_at_interface
    src = ("[OPTIONS]\nFLOW_UNITS CFS\n\n[FILES]\n;;c\nUSE INFLOWS \"old.txt\"\n"
           "SAVE OUTFLOWS \"x.txt\"\n\n[JUNCTIONS]\n")
    t = point_inp_at_interface(src, "/data/Interface_010yr.txt")
    assert t.count("USE INFLOWS") == 1 and "Interface_010yr.txt" in t
    assert "SAVE" not in t and "old.txt" not in t
    t = point_inp_at_interface("[OPTIONS]\nFLOW_UNITS CFS\n\n[JUNCTIONS]\n", "/data/I.txt")
    assert t.index("[OPTIONS]") < t.index("[FILES]") < t.index("[JUNCTIONS]")


def test_run_name_round_trip():
    from mhfd_hydro.swmm_run import run_name
    for cond, rp, area in [("Existing", "100", 0), ("Future", "2", 0), ("Existing", "1%+", 0),
                           ("Master Planned", "WQ", 0), ("Existing", "100", 5)]:
        s = staging.RunSpec.from_filename(run_name("C:/m/Model 1.inp", cond, rp, area, "5.2.4") + ".rpt")
        assert (s.condition, s.return_period, s.correction_area_sq_mi) == (cond, rp, area)


def test_run_storms_matches_direct_run(tmp_path):
    pytest.importorskip("swmm.toolkit")
    from mhfd_hydro import swmm_run
    iface = os.path.join(IFACE_DIR, "Interface_010yr.txt")
    if not os.path.exists(iface) or not RPTS:
        pytest.skip("interface files not available")
    (rpt, cond, rp, area), = swmm_run.run_storms(INP, [(iface, "Ex", "10yr", 0)], str(tmp_path),
                                                  log=lambda m: None)
    assert os.path.basename(rpt) == "Brighton_South_Outfall_Existing_Conditions_Ex_010yr_SWMM5.2.4.rpt"
    assert (cond, rp) == ("Existing", "10") and not os.path.exists(rpt[:-4] + ".out")
    assert os.listdir(tmp_path) == [os.path.basename(rpt)]  # no per-storm .inp left behind
    ref = read_rpt(next(p for p in RPTS if p.endswith("010yr.rpt")))
    assert read_rpt(rpt).inflow("Outfall").max_total_cfs == pytest.approx(
        ref.inflow("Outfall").max_total_cfs, rel=1e-6)


def test_qa_tool_runs_interface_files(tmp_path):
    pytest.importorskip("swmm.toolkit")
    iface = os.path.join(IFACE_DIR, "Interface_002yr.txt")
    if not os.path.exists(iface):
        pytest.skip("interface files not available")
    mod, fa = _load_pyt()
    fa.reset()
    tool = mod.HydrologyQAReport()
    p = tool.getParameterInfo()
    p[0].value = INP
    p[1].values = [[iface, "", "", None]]
    p[1].value, p[1].altered = p[1].values, True
    tool.updateParameters(p)
    tool.updateMessages(p)
    assert "condition" in p[1].error
    assert p[1].values[0][2] == "2" and not p[1].values[0][1]  # storm filled, condition not
    p[1].values = [[iface, "Existing", "2", 0]]
    p[4].value = str(tmp_path / "qa.txt")
    p[6].value = str(tmp_path / "runs")
    tool.execute(p, None)
    assert os.path.exists(tmp_path / "runs" / "Brighton_South_Outfall_Existing_Conditions_Ex_002yr_SWMM5.2.4.rpt")
    assert os.path.exists(tmp_path / "qa.txt")


def test_swmm_builds(tmp_path):
    from mhfd_hydro import swmm_run
    b, src = swmm_run.model_build(INP)
    assert b == "5.2.4" and src.endswith(".rpt")
    assert read_rpt(PCSWMM_RPT).build == "5.2.4"
    # .ini only: EPA SWMM writes Version=52004
    m = tmp_path / "m.inp"
    m.write_text("")
    (tmp_path / "m.ini").write_text("[SWMM5]\nVersion=52004\n")
    assert swmm_run.model_build(str(m)) == ("5.2.4", str(tmp_path / "m.ini"))
    (tmp_path / "m.ini").write_text("[SWMM5]\nVersion=51015\n")
    assert swmm_run.model_build(str(m))[0] == "5.1.015"
    assert swmm_run.same_build("5.1.015", "5.1.15") and not swmm_run.same_build("5.2.3", "5.2.4")
    pytest.importorskip("swmm.toolkit")
    assert swmm_run.engine_build(swmm_run.find_engine()) == "5.2.4"


def test_build_mismatch_warns():
    if not RPTS:
        pytest.skip("per-storm runs not available")
    mod, fa = _load_pyt()
    tool = mod.HydrologyQAReport()
    p = tool.getParameterInfo()
    assert len(p) == 9
    p[0].value, p[0].altered = INP, True
    p[1].values = p[1].value = [[RPTS[0], "Existing", "2", 0]]
    tool.updateParameters(p)
    assert p[7].value.startswith("5.2.4 (from") and p[8].value.startswith("5.2.4:")
    tool.updateMessages(p)
    assert not hasattr(p[8], "warning")
    p[7].value = "5.2.3 (from model.chi)"  # pretend the model came from an older build
    tool.updateMessages(p)
    assert "5.2.3" in p[8].warning


def test_qa_fails_on_empty_results(model, tmp_path):
    # A file with no Node Inflow Summary (e.g. SWMM's binary .out picked by mistake)
    bad = tmp_path / "BSO_Ex_100yr.rpt"
    bad.write_bytes(b"\x00\x01binary junk\x00" * 50)
    good = next(p for p in RPTS if p.endswith("010yr.rpt")) if RPTS else None
    if not good:
        pytest.skip("per-storm runs not available")
    runs = [staging.RunSpec(good, "Existing", "10"), staging.RunSpec(str(bad), "Existing", "100")]
    reports = {r.rpt_path: read_rpt(r.rpt_path) for r in runs}
    st = staging.build(model, runs, reports, 1)
    f = {x.check: x for x in qa.run_all(model, runs, reports, st)}
    assert f["Results populated: Existing 100-yr"].status == qa.FAIL
    assert f["Results populated: Existing 10-yr"].status == qa.PASS
    assert f["Peak flows rise with return period"].status == qa.FAIL
    # A single storm can't show a rise; say so instead of passing.
    st1 = staging.build(model, runs[:1], {good: reports[good]}, 1)
    assert qa.check_flows_increase(st1, runs[:1])[0].status == qa.INFO


def test_value_table_cells_as_objects(tmp_path):
    """Pro returns DEFile cells as value objects whose str() is empty."""
    if not RPTS:
        pytest.skip("per-storm runs not available")
    mod, fa = _load_pyt()

    class Cell:
        def __init__(self, v):
            self.value = v

        def __str__(self):
            return ""

    tool = mod.HydrologyQAReport()
    p = tool.getParameterInfo()
    p[0].value = INP
    p[1].values = p[1].value = [[Cell(RPTS[0]), "", "", None]]
    p[1].altered = True
    tool.updateParameters(p)
    assert p[1].values[0][:3] == [RPTS[0], "Existing", "2"]
    tool.updateMessages(p)
    assert not hasattr(p[1], "error")


def test_tool_a_writes_reports(tmp_path):
    pytest.importorskip("swmm.toolkit")
    ifaces = [os.path.join(IFACE_DIR, f"Interface_{rp}yr.txt") for rp in ("002", "005")]
    if not all(os.path.exists(i) for i in ifaces):
        pytest.skip("interface files not available")
    mod, fa = _load_pyt()
    fa.reset()
    tool = mod.RunSwmmStorms()
    p = tool.getParameterInfo()
    assert tool.label == "A_Generate SWMM rpt files for all storms" and len(p) == 7
    p[0].value = INP
    p[1].values = [[i, "Existing", rp, 0] for i, rp in zip(ifaces, ("2", "5"))]
    p[2].value = str(tmp_path)
    tool.execute(p, None)
    files = sorted(os.listdir(tmp_path))  # reports only; the QA report comes from tool C
    assert files == ["Brighton_South_Outfall_Existing_Conditions_Ex_002yr_SWMM5.2.4.rpt",
                     "Brighton_South_Outfall_Existing_Conditions_Ex_005yr_SWMM5.2.4.rpt"]
    msgs = [m for _, m in fa.MESSAGES]
    assert any(m.startswith("[WARN] SWMM run Existing 2-yr") for m in msgs)  # quick run check
    assert any("Tool C's QA report" in m for m in msgs)


def test_qa_report_layout(staged):
    model, runs, reports, subs, st = staged
    findings = qa.run_all(model, runs, reports, st, subs)
    orphans = next(f for f in findings if f.check == qa.ORPHAN_CHECK)
    findings = [f for f in findings if f.check != qa.ORPHAN_CHECK]  # as tool C merges them
    findings += qa.topology_findings([], ["Overlap", "Gap", "Dangle", "Unconnected point"], orphans)
    md = qa.to_text(findings, "model.inp", "C_Load SWMM Results to Hydrology GDB")
    assert md.startswith("HYDROLOGY QA REPORT")
    # Checks summarized up front, then FAIL before WARN before PASS; per-storm checks share one block.
    assert md.index("SUMMARY OF QC CHECKS") < md.index("FAIL: fix before submitting") \
        < md.index("WARN: review") < md.index("\nPASS (")
    assert md.count("Results populated ") == 1 and "Existing 5-yr" in md
    assert "Checks: Every CUHP subcatchment's outlet (target node) exists" in md
    assert "TOPOLOGY CHECK" in md and "No gaps in subcatchments." in md
    assert "CUHP geometry" not in md


def test_layer_files_point_at_study():
    import json
    from mhfd_hydro import layers
    for fc in layers.LAYER_FILES:
        doc = layers.retarget_lyrx(os.path.join(layers.LAYER_DIR, layers.LAYER_FILES[fc]),
                                   "C:/Data/Hydrology.gdb", fc, "study_id = 405")
        (layer,) = doc["layerDefinitions"]
        con = layer["featureTable"]["dataConnection"]
        assert con["workspaceFactory"] == "FileGDB" and con["dataset"] == fc
        assert con["workspaceConnectionString"].startswith("DATABASE=") and "Hydrology.gdb" in con["workspaceConnectionString"]
        assert layer["featureTable"]["definitionExpression"] == "study_id = 405"
        assert "renderer" in layer and "svc_prpubuser" not in json.dumps(doc)  # no MHFD login


def test_network_topology():
    from mhfd_hydro.topology import check_network
    pts = [(1, "J1", 0, 0), (2, "J2", 100, 0), (3, "O1", 200, 0), (4, "Lonely", 500, 500),
           (5, "Twin", 200.05, 0)]
    lines = [(10, "C1", 1, 2, [(0, 0), (100, 0)]),
             (11, "C2", 2, 3, [(100.05, 0), (150, 0)]),   # ends short of O1: dangle
             (12, "C3", 1, 2, [(0, 0), (200, 0)])]         # ends at O1/Twin, linked to J2
    errs = check_network(pts, lines, tol=0.1)
    kinds = sorted(e[0] for e in errs)
    assert kinds == ["Dangle", "Stacked points", "Unconnected point", "Wrong end point"]
    assert any("Lonely" in e[1] for e in errs if e[0] == "Unconnected point")
    assert check_network(pts[:3], lines[:1] + [(11, "C2", 2, 3, [(100, 0), (200, 0)])], 0.1) == []


def test_interior_rings_are_gaps():
    from mhfd_hydro.topology import interior_rings, ring_area
    import fake_arcpy as fa
    P = fa.Point
    outer = [P(0, 0), P(0, 10), P(10, 10), P(10, 0)]
    hole = [P(4, 4), P(4, 6), P(6, 6), P(6, 4)]
    (ring,) = interior_rings([outer + [None] + hole])
    assert ring_area(ring) == 4
    assert interior_rings([outer]) == []


def test_topology_tool_flags_overlaps_and_writes_points():
    mod, fa = _load_pyt()
    fa.reset()
    gdb = "C:/fake/Hydrology.gdb"
    fa.add_table(f"{gdb}/hydro_subcatchment", ["study_id", "subcatchment_name"], 6428)
    fa.add_table(f"{gdb}/hydro_design_point", ["study_id", "hydro_design_point_id", "swmm_node_name"], 6428)
    fa.add_table(f"{gdb}/hydro_routing_element", ["study_id", "hydro_routing_element_id", "routing_name",
                                                  "from_design_point_id", "to_design_point_id"], 6428)
    sq = lambda x0, s: fa.Polygon(fa.Array([fa.Point(x0, 0), fa.Point(x0, s), fa.Point(x0 + s, s),  # noqa: E731
                                            fa.Point(x0 + s, 0)]))
    with fa.da.InsertCursor(f"{gdb}/hydro_subcatchment", ["study_id", "subcatchment_name", "SHAPE@"]) as c:
        c.insertRow([7, "A", sq(0, 100)])
        c.insertRow([7, "B", sq(100, 100)])   # shares an edge with A: fine
        c.insertRow([7, "C", sq(190, 100)])   # overlaps B by 10 x 100 ft
    with fa.da.InsertCursor(f"{gdb}/hydro_design_point",
                            ["study_id", "hydro_design_point_id", "swmm_node_name", "SHAPE@"]) as c:
        for i, x in ((1, 0), (2, 100)):
            c.insertRow([7, i, f"J{i}", fa.PointGeometry(fa.Point(x, 0))])
    with fa.da.InsertCursor(f"{gdb}/hydro_routing_element",
                            ["study_id", "hydro_routing_element_id", "routing_name", "from_design_point_id",
                             "to_design_point_id", "SHAPE@"]) as c:
        c.insertRow([7, 1, "C1", 1, 2, fa.Polyline([fa.Point(0, 0), fa.Point(100, 0)])])
    tool = mod.TopologyCheck()
    assert tool.label == "Check Topology"  # unlisted; the load tool runs it
    p = tool.getParameterInfo()
    p[0].value, p[1].value, p[2].value, p[3].value = gdb, 7, 0.1, 1.0
    tool.execute(p, None)
    errs = fa.TABLES["c:/fake/scratch.gdb/mhfd_topology_errors_7"]["rows"]
    assert [r["check_type"] for r in errs] == ["Overlap"]
    assert "B and C overlap by 1,000.0 sq ft" in errs[0]["detail"]
