"""Just enough of arcpy to run MHFD_SWMM_to_Hydrology_GDB_Converter.pyt outside ArcGIS Pro.

Tables live in memory. Primary keys named in AUTO_IDS are filled on insert to
mimic the template's attribute rules (NextSequenceValue('hydrology_id')),
and GlobalIDs are generated. Only 'study_id = N' where clauses are supported.
"""

import itertools
import os
import re
import types
import uuid

TABLES = {}        # path(lower) -> {"fields": [...], "rows": [dict], "sr": wkid}
NOT_NULL = {}      # (path(lower), field(lower)) -> field type, for fields that can't be blank
RELATIONSHIPS = []  # dicts with origin, destination, prim, fore
MESSAGES = []
_seq = itertools.count(1)

AUTO_IDS = {
    "hydro_design_point": "hydro_design_point_id",
    "hydro_routing_element": "hydro_routing_element_id",
    "hydro_peak_flow": "hydro_peak_flow_id",
    "hydro_runoff_volume": "hydro_runoff_volume_id",
    "hydro_detention_boundary": "hydro_detention_id",
    "hydro_detention_summary": "hydro_detention_summary_id",
    "hydro_detention_rating_curve": "hydro_detention_rating_curve_id",
    "hydro_subcatchment": "hydro_subcatchment_id",
    "hydro_catchment": "hydro_catchment_id",
    "hydro_point_precipitation": "hydro_point_precip_id",
    "hydro_hour_rainfall": "hydro_hour_rainfall_id",
}


def reset():
    global MAP
    MAP = None
    TABLES.clear()
    NOT_NULL.clear()
    RELATIONSHIPS.clear()
    MESSAGES.clear()
    EXTENTS_RECALCULATED.clear()


def add_table(path, fields, wkid=None, z=False):
    TABLES[path.lower()] = {"fields": ["OBJECTID", "GlobalID"] + [f for f in fields if f not in ("GlobalID",)],
                            "rows": [], "sr": wkid, "name": os.path.basename(path), "z": z}


# --- messages ---------------------------------------------------------------
def AddMessage(m):
    MESSAGES.append(("msg", m))


def AddWarning(m):
    MESSAGES.append(("warn", m))


def AddError(m):
    MESSAGES.append(("err", m))


# --- geometry ---------------------------------------------------------------
class SpatialReference:
    def __init__(self, code=None):
        self.factoryCode = code or 0
        self.name = f"EPSG {code}" if code else ""

    def exportToString(self):
        return str(self.factoryCode)

    def loadFromString(self, s):
        self.factoryCode = int(s)
        self.name = f"EPSG {s}"


class Point:
    def __init__(self, x, y, z=None):
        self.X, self.Y, self.Z = x, y, z


class Array(list):
    pass


class PointGeometry:
    def __init__(self, pt, sr=None):
        self.pt, self.spatialReference = pt, sr
        self.firstPoint = pt

    def projectAs(self, sr):
        return PointGeometry(self.pt, sr)


class Polygon:
    """Rings of Points; holes and multipart shapes are just more rings."""

    def __init__(self, arr, sr=None, has_z=False):
        arr = list(arr)
        self.rings = [list(r) for r in arr] if arr and isinstance(arr[0], (list, Array)) else [arr]
        self.spatialReference, self.hasZ = sr, has_z

    def __iter__(self):
        return iter(self.rings)

    @property
    def pointCount(self):
        return sum(len(r) for r in self.rings)

    @staticmethod
    def _ring_area(r):
        return abs(sum(a.X * b.Y - b.X * a.Y for a, b in zip(r, r[1:] + r[:1]))) / 2.0

    def getArea(self, method="PLANAR", units="ACRES"):
        return sum(self._ring_area(r) for r in self.rings) / (43560.0 if units == "ACRES" else 1.0)

    @property
    def labelPoint(self):
        r = self.rings[0]
        return Point(sum(p.X for p in r) / len(r), sum(p.Y for p in r) / len(r))

    def contains(self, geom):
        x, y = geom.pt.X, geom.pt.Y
        for r in self.rings:
            inside = False
            for a, b in zip(r, r[1:] + r[:1]):
                if (a.Y > y) != (b.Y > y) and x < (b.X - a.X) * (y - a.Y) / (b.Y - a.Y) + a.X:
                    inside = not inside
            if inside:
                return True
        return False

    def union(self, other):
        return Polygon(self.rings + other.rings, self.spatialReference, self.hasZ)

    # Rectangle-only overlay, enough for the topology tests.
    @property
    def extent(self):
        xs = [p.X for r in self.rings for p in r]
        ys = [p.Y for r in self.rings for p in r]
        return Extent(min(xs), min(ys), max(xs), max(ys))

    @property
    def partCount(self):
        return len(self.rings)

    @property
    def area(self):
        return self.getArea(units="SQUAREFEET")

    def disjoint(self, other):
        a, b = self.extent, other.extent
        return a.XMin >= b.XMax or b.XMin >= a.XMax or a.YMin >= b.YMax or b.YMin >= a.YMax

    def intersect(self, other, dim):
        a, b = self.extent, other.extent
        x0, y0, x1, y1 = max(a.XMin, b.XMin), max(a.YMin, b.YMin), min(a.XMax, b.XMax), min(a.YMax, b.YMax)
        return Polygon(Array([Point(x0, y0), Point(x0, y1), Point(x1, y1), Point(x1, y0)]),
                       self.spatialReference)

    def projectAs(self, sr):
        return Polygon(self.rings, sr, self.hasZ)


class Extent:
    def __init__(self, xmin, ymin, xmax, ymax, spatial_reference=None):
        self.XMin, self.YMin, self.XMax, self.YMax = xmin, ymin, xmax, ymax
        self.spatialReference = spatial_reference


class Polyline:
    def __init__(self, arr, sr=None):
        self.points, self.spatialReference = list(arr), sr
        self.firstPoint, self.lastPoint = self.points[0], self.points[-1]

    def projectAs(self, sr):
        return Polyline(self.points, sr)


# --- catalog ----------------------------------------------------------------
def Exists(path):
    return str(path).lower() in TABLES


class _Field:
    def __init__(self, name, table=""):
        self.name = name
        ftype = NOT_NULL.get((str(table).lower(), name.lower()))
        self.type = ftype or "String"
        self.isNullable = ftype is None
        self.editable = True
        self.defaultValue = None


def ListFields(table):
    return [_Field(f, table) for f in TABLES[str(table).lower()]["fields"]]


def Describe(path):
    path = str(path).lower()
    if path in TABLES:
        t = TABLES[path]
        return types.SimpleNamespace(spatialReference=SpatialReference(t["sr"]), dataType="Table",
                                     hasZ=t.get("z", False))
    children = []
    for r in RELATIONSHIPS:
        children.append(types.SimpleNamespace(
            dataType="RelationshipClass", originClassNames=[r["origin"]],
            destinationClassNames=[r["destination"]],
            originClassKeys=[(r["prim"], "OriginPrimary", ""), (r["fore"], "OriginForeign", "")]))
    return types.SimpleNamespace(children=children)


# --- cursors ----------------------------------------------------------------
def _where(clause):
    if not clause:
        return lambda r: True
    m = re.fullmatch(r"\s*study_id\s*=\s*(-?\d+)\s*", clause)
    if not m:
        raise NotImplementedError(clause)
    sid = int(m.group(1))
    return lambda r: r.get("study_id") == sid


def _canon(table, name):
    if name in ("OID@",):
        return "OBJECTID"
    if name.startswith("SHAPE@"):
        return "SHAPE"
    low = {f.lower(): f for f in table["fields"]}
    if name.lower() not in low:
        raise RuntimeError(f"Field {name} not in {table['name']}")
    return low[name.lower()]


class InsertCursor:
    def __init__(self, path, fields):
        self.path = str(path).lower()
        self.t = TABLES[self.path]
        self.cols = [_canon(self.t, f) for f in fields]

    def insertRow(self, values):
        row = {f: None for f in self.t["fields"]}
        row.update(dict(zip(self.cols, values)))
        for (path, field), _ in NOT_NULL.items():
            if path == self.path and row.get(_canon(self.t, field)) is None:
                raise RuntimeError(f"The field is not nullable. [{field}]")
        row["OBJECTID"] = max([r["OBJECTID"] for r in self.t["rows"]] or [0]) + 1
        row["GlobalID"] = "{" + str(uuid.uuid4()).upper() + "}"
        auto = AUTO_IDS.get(self.t["name"])
        if auto and row.get(auto) is None:
            row[auto] = next(_seq)
        self.t["rows"].append(row)
        return row["OBJECTID"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SearchCursor:
    def __init__(self, path, fields, where=None):
        self.t = TABLES[str(path).lower()]
        self.cols = [_canon(self.t, f) for f in fields]
        self.pred = _where(where)

    def __iter__(self):
        for r in list(self.t["rows"]):
            if self.pred(r):
                yield tuple(r.get(c) for c in self.cols)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UpdateCursor(SearchCursor):
    def __iter__(self):
        self._rows = [r for r in self.t["rows"] if self.pred(r)]
        for r in self._rows:
            self._cur = r
            yield [r.get(c) for c in self.cols]

    def updateRow(self, values):
        self._cur.update(dict(zip(self.cols, values)))

    def deleteRow(self):
        self.t["rows"].remove(self._cur)


class Editor:
    def __init__(self, ws):
        self.ws = ws

    def startEditing(self, *a):
        pass

    def startOperation(self):
        pass

    def stopOperation(self):
        pass

    def stopEditing(self, save):
        pass

    def abortOperation(self):
        pass


da = types.SimpleNamespace(InsertCursor=InsertCursor, SearchCursor=SearchCursor,
                           UpdateCursor=UpdateCursor, Editor=Editor)
EXTENTS_RECALCULATED = []


def _create_fc(folder, name, geom, spatial_reference=None):
    add_table(f"{folder}/{name}", [], getattr(spatial_reference, "factoryCode", None))


def _add_field(path, name, *a, **k):
    TABLES[str(path).lower()]["fields"].append(name)


management = types.SimpleNamespace(RecalculateFeatureClassExtent=EXTENTS_RECALCULATED.append,
                                   CreateFeatureclass=_create_fc, AddField=_add_field,
                                   Delete=lambda p: TABLES.pop(str(p).lower(), None))
env = types.SimpleNamespace(scratchGDB="C:/fake/scratch.gdb")


def _no_project(name):
    raise OSError("No ArcGIS Pro project is open")


class _Layer:
    def __init__(self, path):
        self.dataSource, self.name = path, os.path.basename(path)
        self.definitionQuery, self.showLabels = "", False

    def supports(self, what):
        return True


class _Map:
    name = "Map"
    spatialReference = None

    def __init__(self):
        self.layers, self.tables = [], []

    def listLayers(self):
        return list(self.layers)

    def removeLayer(self, lyr):
        self.layers.remove(lyr)

    def addDataFromPath(self, path):
        lyr = _Layer(path)
        if TABLES.get(str(path).lower(), {}).get("sr") is None:  # no geometry: a standalone table
            self.tables.append(lyr)
        else:
            self.layers.append(lyr)
        return lyr

    def addLayer(self, layer_file):
        import json
        doc = json.load(open(layer_file.path, encoding="utf-8"))
        conn = doc["layerDefinitions"][0]["featureTable"]["dataConnection"]
        lyr = _Layer(conn["workspaceConnectionString"].split("=", 1)[1] + "/" + conn["dataset"])
        lyr.doc = doc
        self.layers.append(lyr)
        return [lyr]

    def listTables(self):
        return list(self.tables)

    def removeTable(self, tbl):
        self.tables.remove(tbl)


MAP = None  # set to _Map() in a test to stand in for an open Pro map


def _project(name):
    if MAP is None:
        raise OSError("No ArcGIS Pro project is open")
    return types.SimpleNamespace(activeMap=MAP, activeView=None)


mp = types.SimpleNamespace(ArcGISProject=_project,
                           LayerFile=lambda path: types.SimpleNamespace(path=path))


class Parameter:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.value = None
        self.values = None
        self.altered = False
        self.hasBeenValidated = False
        self.filter = types.SimpleNamespace(list=[])
        self.filters = [types.SimpleNamespace(type=None, list=[]) for _ in range(4)]
        self.columns = []

    @property
    def valueAsText(self):
        return None if self.value is None else str(self.value)

    def setErrorMessage(self, m):
        self.error = m

    def setWarningMessage(self, m):
        self.warning = m
