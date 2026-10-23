"""Read the summary tables of an EPA SWMM 5.1/5.2 report (.rpt) file."""

import re
from dataclasses import dataclass, field

from .constants import KCF_TO_ACFT, MGAL_TO_ACFT


@dataclass
class NodeInflow:
    node: str
    kind: str
    max_lateral_cfs: float
    max_total_cfs: float
    time_of_max: str
    lateral_volume_mgal: float
    total_volume_mgal: float

    @property
    def total_volume_ac_ft(self):
        return None if self.total_volume_mgal is None else self.total_volume_mgal * MGAL_TO_ACFT


@dataclass
class StorageResult:
    node: str
    max_volume_kcf: float
    max_pct_full: float
    time_of_max: str
    max_outflow_cfs: float

    @property
    def max_volume_ac_ft(self):
        return None if self.max_volume_kcf is None else self.max_volume_kcf * KCF_TO_ACFT


@dataclass
class SwmmReport:
    path: str
    version: str = None
    build: str = None  # engine build from the header, e.g. "5.2.4"
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    runoff_continuity_pct: float = None
    routing_continuity_pct: float = None
    instability: list = field(default_factory=list)   # [(link, index)]
    node_inflow: dict = field(default_factory=dict)   # name -> NodeInflow
    flooding: dict = field(default_factory=dict)      # name -> hours flooded
    storage: dict = field(default_factory=dict)       # name -> StorageResult
    link_max_flow: dict = field(default_factory=dict)  # name -> max |flow| cfs

    def inflow(self, node):
        r = self.node_inflow.get(node)
        if r is None:
            low = {k.lower(): v for k, v in self.node_inflow.items()}
            r = low.get(str(node).lower())
        return r


def _f(tok):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def _section(lines, title):
    """Yield the data rows (token lists) of a summary table titled `title`."""
    for i, line in enumerate(lines):
        if line.strip() == title and i + 1 < len(lines) and set(lines[i + 1].strip()) == {"*"}:
            break
    else:
        return []
    j = i + 2
    # Skip to the dashed rule that closes the column header (second rule).
    rules = 0
    while j < len(lines):
        s = lines[j].strip()
        if s.startswith("---"):
            rules += 1
            if rules == 2:
                j += 1
                break
        elif rules == 0 and s and not s.startswith("---") and "No " in s and s.endswith("."):
            return []  # e.g. "No nodes were flooded."
        j += 1
    rows = []
    while j < len(lines):
        s = lines[j].strip()
        if not s or s.startswith("*"):
            break
        rows.append(s.split())
        j += 1
    return rows


def read_rpt(path):
    with open(path, encoding="latin-1") as fh:
        lines = fh.read().splitlines()
    rpt = SwmmReport(path=str(path))

    for line in lines[:10]:
        m = re.search(r"VERSION\s+([\d.]+)", line, re.I)
        if m:
            rpt.version = m.group(1)
        m = re.search(r"\(Build\s+([\d.]+)\)", line, re.I)
        if m:
            rpt.build = m.group(1)
    for line in lines:
        s = line.strip()
        if s.startswith("WARNING"):
            rpt.warnings.append(s)
        elif s.startswith("ERROR"):
            rpt.errors.append(s)

    block = None
    for line in lines:
        s = line.strip()
        if s.startswith("Runoff Quantity Continuity"):
            block = "runoff"
        elif s.startswith("Flow Routing Continuity"):
            block = "routing"
        elif s.startswith("Continuity Error (%)") and block:
            val = _f(s.split()[-1])
            if block == "runoff":
                rpt.runoff_continuity_pct = val
            else:
                rpt.routing_continuity_pct = val
            block = None

    for i, line in enumerate(lines):
        if line.strip() == "Highest Flow Instability Indexes":
            for s in lines[i + 2:i + 10]:
                m = re.match(r"\s*Link\s+(\S+)\s+\((\d+)\)", s)
                if m:
                    rpt.instability.append((m.group(1), int(m.group(2))))
            break

    for r in _section(lines, "Node Inflow Summary"):
        if len(r) < 8:
            continue
        rpt.node_inflow[r[0]] = NodeInflow(
            node=r[0], kind=r[1], max_lateral_cfs=_f(r[2]), max_total_cfs=_f(r[3]),
            time_of_max=f"{r[4]} {r[5]}", lateral_volume_mgal=_f(r[6]),
            total_volume_mgal=_f(r[7]))

    for r in _section(lines, "Node Flooding Summary"):
        if len(r) >= 2:
            rpt.flooding[r[0]] = _f(r[1])

    for r in _section(lines, "Storage Volume Summary"):
        if len(r) < 6:
            continue
        # Trailing columns are stable across 5.1/5.2: ... MaxVol MaxPct days hr:min MaxOutflow
        rpt.storage[r[0]] = StorageResult(
            node=r[0], max_volume_kcf=_f(r[-5]), max_pct_full=_f(r[-4]),
            time_of_max=f"{r[-3]} {r[-2]}", max_outflow_cfs=_f(r[-1]))

    for r in _section(lines, "Link Flow Summary"):
        if len(r) >= 3:
            rpt.link_max_flow[r[0]] = _f(r[2])

    return rpt
