"""Read an EPA SWMM 5 input (.inp) file into plain Python structures.

Only the sections the MHFD deliverables need are interpreted: nodes, links,
coordinates/vertices, tags, storage and rating curves, and [FILES].
Every other section is kept as raw token rows in SwmmModel.sections.
"""

import re
import shlex
from collections import OrderedDict
from dataclasses import dataclass, field

NODE_SECTIONS = {
    "JUNCTIONS": "JUNCTION",
    "OUTFALLS": "OUTFALL",
    "DIVIDERS": "DIVIDER",
    "STORAGE": "STORAGE",
}
LINK_SECTIONS = {
    "CONDUITS": "CONDUIT",
    "PUMPS": "PUMP",
    "ORIFICES": "ORIFICE",
    "WEIRS": "WEIR",
    "OUTLETS": "OUTLET",
}


@dataclass
class Node:
    name: str
    kind: str              # JUNCTION / OUTFALL / DIVIDER / STORAGE
    invert: float
    max_depth: float = None
    x: float = None
    y: float = None
    tag: str = None
    params: list = field(default_factory=list)  # remaining raw tokens


@dataclass
class Link:
    name: str
    kind: str              # CONDUIT / PUMP / ORIFICE / WEIR / OUTLET
    from_node: str
    to_node: str
    vertices: list = field(default_factory=list)  # [(x, y), ...] interior points
    tag: str = None
    params: list = field(default_factory=list)


@dataclass
class Curve:
    name: str
    kind: str              # STORAGE, RATING, ...
    points: list = field(default_factory=list)   # [(x, y), ...]


@dataclass
class SwmmModel:
    path: str
    nodes: "OrderedDict[str, Node]"
    links: "OrderedDict[str, Link]"
    curves: dict
    options: dict
    files: list
    sections: dict

    def node(self, name):
        """Case-insensitive lookup (SWMM object names are not case sensitive)."""
        n = self.nodes.get(name)
        if n is None:
            n = self._lower_nodes().get(str(name).lower())
        return n

    def _lower_nodes(self):
        if not hasattr(self, "_lnodes"):
            self._lnodes = {k.lower(): v for k, v in self.nodes.items()}
        return self._lnodes

    def canonical_node_name(self, name):
        n = self.node(name)
        return n.name if n else None

    @property
    def interface_file(self):
        for row in self.files:
            if len(row) >= 3 and row[0].upper() == "USE" and row[1].upper() == "INFLOWS":
                return row[2]
        return None

    def link_geometry(self, link):
        """Polyline coordinates from-node -> vertices -> to-node, or None."""
        a, b = self.node(link.from_node), self.node(link.to_node)
        if a is None or b is None or a.x is None or b.x is None:
            return None
        return [(a.x, a.y)] + list(link.vertices) + [(b.x, b.y)]


def _tokens(line):
    line = line.split(";", 1)[0] if '"' not in line else _strip_comment_quoted(line)
    if not line.strip():
        return []
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _strip_comment_quoted(line):
    out, quoted = [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            break
        out.append(ch)
    return "".join(out)


def _f(tok):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def read_inp(path):
    sections = OrderedDict()
    current = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            s = raw.strip()
            m = re.match(r"^\[(.+?)\]", s)
            if m:
                current = m.group(1).upper()
                sections.setdefault(current, [])
                continue
            if current is None:
                continue
            toks = _tokens(raw)
            if toks:
                sections[current].append(toks)

    nodes = OrderedDict()
    for sec, kind in NODE_SECTIONS.items():
        for row in sections.get(sec, []):
            # Divider rows are Name Elev DivertedLink Type ..., so no depth at [2].
            max_depth = _f(row[2]) if kind in ("JUNCTION", "STORAGE") and len(row) > 2 else None
            nodes[row[0]] = Node(name=row[0], kind=kind, invert=_f(row[1]),
                                 max_depth=max_depth, params=row[2:])

    links = OrderedDict()
    for sec, kind in LINK_SECTIONS.items():
        for row in sections.get(sec, []):
            links[row[0]] = Link(name=row[0], kind=kind, from_node=row[1],
                                 to_node=row[2], params=row[3:])

    lower_nodes = {k.lower(): v for k, v in nodes.items()}
    for row in sections.get("COORDINATES", []):
        n = nodes.get(row[0]) or lower_nodes.get(row[0].lower())
        if n is not None and len(row) >= 3:
            n.x, n.y = _f(row[1]), _f(row[2])

    lower_links = {k.lower(): v for k, v in links.items()}
    for row in sections.get("VERTICES", []):
        lk = links.get(row[0]) or lower_links.get(row[0].lower())
        if lk is not None and len(row) >= 3:
            lk.vertices.append((_f(row[1]), _f(row[2])))

    for row in sections.get("TAGS", []):
        if len(row) < 3:
            continue
        target = nodes if row[0].upper() == "NODE" else links if row[0].upper() == "LINK" else None
        if target is not None:
            obj = target.get(row[1]) or {k.lower(): v for k, v in target.items()}.get(row[1].lower())
            if obj is not None:
                obj.tag = " ".join(row[2:])

    curves = {}
    for row in sections.get("CURVES", []):
        name = row[0]
        rest = row[1:]
        if name not in curves:
            kind = ""
            if rest and _f(rest[0]) is None:
                kind, rest = rest[0].upper(), rest[1:]
            curves[name] = Curve(name=name, kind=kind)
        vals = [_f(t) for t in rest]
        vals = [v for v in vals if v is not None]
        curves[name].points.extend(zip(vals[0::2], vals[1::2]))

    options = {r[0].upper(): " ".join(r[1:]) for r in sections.get("OPTIONS", []) if r}

    return SwmmModel(path=str(path), nodes=nodes, links=links, curves=curves,
                     options=options, files=sections.get("FILES", []), sections=sections)


# ---------------------------------------------------------------------------
# Storage and outlet rating helpers

def storage_shape(node):
    """Return (shape, curve_name_or_coeffs) for a STORAGE node."""
    p = node.params  # [MaxDepth, InitDepth, Shape, ...]
    if len(p) < 3:
        return None, None
    shape = p[2].upper()
    if shape == "TABULAR":
        return shape, p[3] if len(p) > 3 else None
    if shape == "FUNCTIONAL":
        return shape, [_f(t) for t in p[3:6]]
    return shape, [_f(t) for t in p[3:6]]


def stage_storage_table(model, node, step_count=None):
    """Depth/area/volume table for a storage node.

    Returns a list of dicts with depth_ft, elevation_ft, area_sq_ft, volume_ac_ft,
    or [] when the storage shape is not tabular/functional.
    Volume is integrated with the trapezoidal rule, which is what SWMM uses
    for tabular storage curves.
    """
    shape, spec = storage_shape(node)
    depths_areas = []
    if shape == "TABULAR" and spec in model.curves:
        depths_areas = list(model.curves[spec].points)
    elif shape == "FUNCTIONAL" and spec and spec[0] is not None:
        a, b, c = (spec + [0, 0, 0])[:3]
        maxd = node.max_depth or 0
        n = step_count or 10
        depths_areas = [(maxd * i / n, a * (maxd * i / n) ** b + c) for i in range(n + 1)]
    if not depths_areas:
        return []

    rows, vol = [], 0.0
    prev_d, prev_a = None, None
    for d, a in depths_areas:
        if prev_d is not None:
            vol += (d - prev_d) * (a + prev_a) / 2.0
        rows.append({
            "depth_ft": d,
            "elevation_ft": (node.invert or 0.0) + d,
            "area_sq_ft": a,
            "volume_ac_ft": vol / 43560.0,
        })
        prev_d, prev_a = d, a
    return rows


def outlet_rating(model, storage_name):
    """Combined depth->discharge function for the links leaving a storage node.

    Only TABULAR/DEPTH and TABULAR/HEAD outlets are interpreted exactly.
    Returns (callable or None, list of link names, list of uninterpreted links).
    """
    out_links = [lk for lk in model.links.values()
                 if lk.from_node.lower() == storage_name.lower()]
    curves, skipped = [], []
    for lk in out_links:
        if lk.kind == "OUTLET" and len(lk.params) >= 3 and lk.params[1].upper().startswith("TABULAR"):
            cname = lk.params[2]
            offset = _f(lk.params[0]) or 0.0
            if cname in model.curves:
                curves.append((offset, model.curves[cname].points))
                continue
        skipped.append(lk.name)

    if not curves:
        return None, [lk.name for lk in out_links], skipped

    def q_at(depth):
        total = 0.0
        for offset, pts in curves:
            h = depth - offset
            if h <= 0:
                continue
            total += _interp(pts, h)
        return total

    return q_at, [lk.name for lk in out_links], skipped


def _interp(pts, x):
    if not pts:
        return 0.0
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]
