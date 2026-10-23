"""Run EPA SWMM once per storm from a SWMM model and CUHP interface files.

CUHP writes one interface file per storm (Interface_100yr.txt etc.). The SWMM
model reads it through `USE INFLOWS "<file>"` in [FILES]. For each storm this
module writes a copy of the .inp pointing at that storm's interface file,
runs the SWMM engine on it and returns the .rpt path.

Engine, first found wins:
  1. a path the user gives (runswmm.exe / swmm5.exe, or swmm5.dll)
  2. the swmm-toolkit Python package, if installed
  3. EPA SWMM 5.x installed under Program Files (runswmm.exe / swmm5.exe)
  4. swmm5.dll from an EPA SWMM or PCSWMM install
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import constants as C

CONDITION_TOKENS = {"Existing": "Ex", "Future": "Fut", "Pre-Developed": "Pre",
                    "Master Planned": "MP", "FEMA": "FEMA", "Design": "Design"}

_EXE_NAMES = ("runswmm.exe", "swmm5.exe", "runswmm", "swmm5")
_DLL_RUNNER = (
    "import ctypes, sys\n"
    "lib = ctypes.CDLL(sys.argv[1])\n"
    "f = lib.swmm_run\n"
    "f.argtypes = [ctypes.c_char_p] * 3\n"
    "f.restype = ctypes.c_int\n"
    "args = [a.encode('mbcs' if sys.platform == 'win32' else 'utf-8') for a in sys.argv[2:5]]\n"
    "sys.exit(f(*args))\n"
)


class SwmmEngineNotFound(RuntimeError):
    pass


def _program_dirs():
    dirs = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        d = os.environ.get(var)
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def _version_key(path):
    nums = re.findall(r"\d+", path)
    return [int(n) for n in nums]


def find_engine(user_path=None):
    """Return (kind, path) with kind in 'exe', 'dll', 'toolkit'."""
    if user_path:
        if os.path.isdir(user_path):
            for name in _EXE_NAMES + ("swmm5.dll",):
                cand = os.path.join(user_path, name)
                if os.path.isfile(cand):
                    user_path = cand
                    break
        if not os.path.isfile(user_path):
            raise SwmmEngineNotFound(f"SWMM engine not found at {user_path}")
        return ("dll" if user_path.lower().endswith(".dll") else "exe"), user_path
    try:
        from swmm.toolkit import solver  # noqa: F401
        return "toolkit", None
    except ImportError:
        pass
    exes, dlls = [], []
    for base in _program_dirs():
        for d in glob.glob(os.path.join(base, "EPA SWMM*")):
            exes += [os.path.join(d, n) for n in _EXE_NAMES if os.path.isfile(os.path.join(d, n))]
            dlls += glob.glob(os.path.join(d, "swmm5.dll"))
        for pattern in ("CHI", "CHI*", "PCSWMM*"):
            dlls += glob.glob(os.path.join(base, pattern, "**", "swmm5.dll"), recursive=True)
    if exes:
        return "exe", sorted(exes, key=_version_key)[-1]
    if dlls:
        return "dll", sorted(dlls, key=_version_key)[-1]
    raise SwmmEngineNotFound(
        "No SWMM engine found. Install EPA SWMM 5.2 (it provides runswmm.exe), or give the "
        "path to runswmm.exe or swmm5.dll.")


def _python_exe():
    # Inside ArcGIS Pro sys.executable is ArcGISPro.exe, not python.
    for cand in (os.path.join(sys.exec_prefix, "python.exe"), os.path.join(sys.exec_prefix, "python"),
                 sys.executable):
        if os.path.isfile(cand) and "arcgispro" not in os.path.basename(cand).lower():
            return cand
    return sys.executable


def run_swmm(inp, rpt, out, engine, cwd=None):
    """Run one SWMM simulation. Relative paths in the model resolve from cwd."""
    kind, path = engine
    inp, rpt, out = (os.path.abspath(p) for p in (inp, rpt, out))
    if kind == "toolkit":
        from swmm.toolkit import solver
        old = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            solver.swmm_run(inp, rpt, out)
        except Exception as e:  # the toolkit raises on SWMM errors; the .rpt says why
            if not os.path.exists(rpt):
                raise RuntimeError(f"SWMM failed on {os.path.basename(inp)}: {e}")
        finally:
            os.chdir(old)
        return
    if kind == "exe":
        cmd = [path, inp, rpt, out]
    else:
        cmd = [_python_exe(), "-c", _DLL_RUNNER, path, inp, rpt, out]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, creationflags=flags)
    if not os.path.exists(rpt):
        raise RuntimeError(f"SWMM did not write {rpt} (exit {res.returncode}): "
                           f"{(res.stderr or res.stdout).strip()[-500:]}")


def point_inp_at_interface(text, interface_path):
    """Model text with [FILES] reading inflows from interface_path only.

    Existing USE INFLOWS lines are replaced and SAVE lines dropped, so a run
    never overwrites files the original model writes.
    """
    line = f'USE INFLOWS "{os.path.abspath(interface_path)}"'
    lines = text.splitlines()
    out, in_files, placed = [], False, False
    for ln in lines:
        s = ln.strip()
        if s.startswith("["):
            if in_files and not placed:
                out.append(line)
                placed = True
            in_files = s.upper() == "[FILES]"
            out.append(ln)
            continue
        if in_files:
            u = s.upper()
            if u.startswith("USE INFLOWS"):
                if not placed:
                    out.append(line)
                    placed = True
                continue
            if u.startswith("SAVE "):
                continue
        out.append(ln)
    if in_files and not placed:
        out.append(line)
        placed = True
    if not placed:
        idx = next((i for i, ln in enumerate(out) if ln.strip().upper() == "[OPTIONS]"), None)
        block = ["[FILES]", line, ""]
        if idx is None:
            out = block + out
        else:  # after the [OPTIONS] section
            j = idx + 1
            while j < len(out) and not out[j].strip().startswith("["):
                j += 1
            out[j:j] = block
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Engine build checks. Peak flows only reproduce on the same SWMM build.

_TINY_MODEL = """[OPTIONS]
FLOW_UNITS CFS
START_DATE 01/01/2000
START_TIME 00:00:00
END_DATE 01/01/2000
END_TIME 01:00:00
[JUNCTIONS]
J1 100 5
[OUTFALLS]
O1 99 FREE
[CONDUITS]
C1 J1 O1 100 0.013 0 0
[XSECTIONS]
C1 CIRCULAR 1 0 0 0
"""
_ENGINE_BUILDS = {}


def report_build(rpt):
    """Build string from a report header, e.g. '5.2.4', or None."""
    try:
        with open(rpt, encoding="latin-1", errors="replace") as fh:
            head = [next(fh, "") for _ in range(10)]
    except OSError:
        return None
    for ln in head:
        m = re.search(r"\(Build\s+([\d.]+)\)", ln, re.I)
        if m:
            return m.group(1)
    return None


def engine_build(engine):
    """Run a one-pipe model to read the engine's build. Cached per engine."""
    key = tuple(engine)
    if key not in _ENGINE_BUILDS:
        tmp = tempfile.mkdtemp(prefix="mhfd_swmm_")
        try:
            inp = os.path.join(tmp, "version.inp")
            with open(inp, "w") as fh:
                fh.write(_TINY_MODEL)
            try:
                run_swmm(inp, inp[:-4] + ".rpt", inp[:-4] + ".out", engine, cwd=tmp)
                _ENGINE_BUILDS[key] = report_build(inp[:-4] + ".rpt")
            except Exception:
                _ENGINE_BUILDS[key] = None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return _ENGINE_BUILDS[key]


def model_build(inp):
    """(build, source file) the model was last run with, from files beside the .inp.

    Looks at the model's own .rpt, then PCSWMM's .chi, then EPA SWMM's .ini.
    """
    stem = os.path.splitext(inp)[0]
    b = report_build(stem + ".rpt")
    if b:
        return b, stem + ".rpt"
    for ext, pattern in ((".chi", r"(?im)^SWMM5 Version\s*=\s*([\d.]+)"),
                         (".ini", r"(?im)^Version\s*=\s*(\d{5})\s*$")):
        try:
            with open(stem + ext, encoding="latin-1", errors="replace") as fh:
                m = re.search(pattern, fh.read())
        except OSError:
            continue
        if m:
            v = m.group(1)
            if ext == ".ini":  # 52004 -> 5.2.4, 51015 -> 5.1.015
                v = f"{v[0]}.{v[1]}.{int(v[2:]):03d}" if v[1] == "1" else f"{v[0]}.{v[1]}.{int(v[2:])}"
            return v, stem + ext
    return None, None


def same_build(a, b):
    if not a or not b:
        return None
    return [int(x) for x in a.split(".")] == [int(x) for x in b.split(".")]


def run_name(inp, condition, return_period, correction_area=0, build=None):
    """<model>_Ex_100yr[_5mi^2][_SWMM5.2.4]: a name RunSpec.from_filename reads back."""
    stem = os.path.splitext(os.path.basename(inp))[0]
    rp = return_period if not str(return_period).isdigit() else f"{int(return_period):03d}yr"
    name = f"{stem}_{CONDITION_TOKENS.get(condition, condition)}_{rp}"
    if correction_area:
        name += f"_{correction_area:g}mi^2"
    if build:
        name += f"_SWMM{build}"
    return name


def run_storms(inp, storms, out_dir, engine=None, keep_out=False, log=print):
    """Run SWMM for each storm.

    storms: iterable of (interface_path, condition, return_period, correction_area).
    Returns a list of (rpt_path, condition, return_period, correction_area).
    """
    engine = engine or find_engine()
    build = engine_build(engine)
    os.makedirs(out_dir, exist_ok=True)
    with open(inp, encoding="latin-1") as fh:
        text = fh.read()
    model_dir = os.path.dirname(os.path.abspath(inp))
    tmp = tempfile.mkdtemp(prefix="mhfd_swmm_")  # per-storm .inp copies live here only
    results = []
    try:
        results = _run_each(inp, text, storms, out_dir, engine, build, keep_out, log, model_dir, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def _run_each(inp, text, storms, out_dir, engine, build, keep_out, log, model_dir, tmp):
    results = []
    for iface, cond, rp, area in storms:
        cond = C.normalize_condition(cond) or cond
        rp = C.normalize_return_period(rp) or rp
        area = float(area or 0)
        if not os.path.isfile(iface):
            raise FileNotFoundError(f"Interface file not found: {iface}")
        name = run_name(inp, cond, rp, area, build)
        run_inp = os.path.join(tmp, name + ".inp")
        rpt = os.path.join(out_dir, name + ".rpt")
        out = os.path.join(out_dir, name + ".out")
        with open(run_inp, "w", encoding="latin-1") as fh:
            fh.write(point_inp_at_interface(text, iface))
        log(f"Running SWMM {build or '(build unknown)'}: {cond} {rp}-yr from {os.path.basename(iface)}")
        run_swmm(run_inp, rpt, out, engine, cwd=model_dir)
        if not keep_out and os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        errors = _rpt_errors(rpt)
        if errors:
            raise RuntimeError(f"SWMM reported errors for {name}:\n  " + "\n  ".join(errors[:10]))
        results.append((rpt, cond, rp, area))
    return results


def _rpt_errors(rpt):
    with open(rpt, encoding="latin-1", errors="replace") as fh:
        return [ln.strip() for ln in fh if ln.strip().upper().startswith("ERROR")]
