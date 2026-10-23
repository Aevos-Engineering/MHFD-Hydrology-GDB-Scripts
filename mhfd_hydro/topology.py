"""Topology checks on a study's design points and routing elements.

Plain coordinates in, findings out, so the checks run without arcpy. The
polygon checks (overlaps, gaps) need geometry operations and live in the
.pyt; ring_area() and interior_rings() here help them.
"""

import math
from collections import defaultdict


def _cell(x, y, size):
    return (math.floor(x / size), math.floor(y / size))


class _PointIndex:
    """Grid lookup of points within a tolerance."""

    def __init__(self, points, tol):
        self.tol = tol
        self.size = max(tol, 1e-9)
        self.grid = defaultdict(list)
        for pid, x, y in points:
            self.grid[_cell(x, y, self.size)].append((pid, x, y))

    def near(self, x, y):
        cx, cy = _cell(x, y, self.size)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for pid, px, py in self.grid.get((cx + dx, cy + dy), ()):
                    if math.hypot(px - x, py - y) <= self.tol:
                        out.append(pid)
        return out


def check_network(points, lines, tol=0.1):
    """Design point / routing element topology.

    points  [(design_point_id, name, x, y)]
    lines   [(routing_id, name, from_id, to_id, [(x, y), ...])]
    tol     snapping tolerance in map units

    Returns a list of (check, detail, x, y) errors:
      Dangle            a routing element end with no design point on it
      Wrong end point   the end touches a different design point than its from/to id
      Unconnected point a design point no routing element starts or ends at
      Stacked points    two design points in the same place
    """
    idx = _PointIndex([(pid, x, y) for pid, _, x, y in points], tol)
    names = {pid: name for pid, name, _, _ in points}
    used = set()
    errors = []
    for rid, rname, from_id, to_id, coords in lines:
        if not coords:
            continue
        for end, (x, y), want in (("start", coords[0], from_id), ("end", coords[-1], to_id)):
            hits = idx.near(x, y)
            used.update(hits)
            if not hits:
                errors.append(("Dangle", f"Routing element {rname} {end} is not on a design point "
                                         f"(tolerance {tol:g}).", x, y))
            elif want is not None and want not in hits:
                errors.append(("Wrong end point", f"Routing element {rname} {end} touches "
                                                  f"{', '.join(str(names.get(h, h)) for h in hits)} but is "
                                                  f"linked to {names.get(want, want)}.", x, y))
    for pid, name, x, y in points:
        if pid not in used:
            errors.append(("Unconnected point", f"Design point {name} is not at the end of any "
                                                "routing element.", x, y))
    seen = set()
    for pid, name, x, y in points:
        others = [h for h in idx.near(x, y) if h != pid and (h, pid) not in seen]
        for h in others:
            seen.add((pid, h))
            errors.append(("Stacked points", f"Design points {name} and {names.get(h, h)} are in the "
                                             "same place.", x, y))
    return errors


def ring_area(ring):
    """Area of a ring of (x, y) by the shoelace formula."""
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2.0


def interior_rings(parts):
    """Holes of a polygon given as arcpy iterates it: each part is a list of
    points (objects with X, Y) where None separates the outer ring from holes."""
    holes = []
    for part in parts:
        rings, ring = [], []
        for pt in part:
            if pt is None:
                rings.append(ring)
                ring = []
            else:
                ring.append((pt.X, pt.Y))
        rings.append(ring)
        holes += [r for r in rings[1:] if len(r) >= 3]
    return holes


def ring_center(ring):
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)
