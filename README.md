# MHFD SWMM to Hydrology GDB Converter

An ArcGIS Pro toolbox from Aevos Engineering that turns a finished CUHP/SWMM model into the **MHFD Hydrology Geodatabase** deliverable (03 Hydrology), and writes a plain-language QA report that tells you what to fix before you submit.

## What it's for

MHFD master plans and LOMR-type studies have to hand in a Hydrology GDB (built on MHFD template v4.2) with subcatchments, design points, routing elements, peak flows, runoff volumes, detention and rainfall, all linked to each other. Building that by hand from SWMM and CUHP takes days and is easy to get wrong. This toolbox does it in three steps and checks the model against MHFD's rules along the way.

It does **not** build or calibrate your model. You still do the hydrology in CUHP and SWMM (or PCSWMM); this toolbox packages the results.

## What you need

- **ArcGIS Pro 3.3 or later** (Windows). No extra Python packages.
- **EPA SWMM 5.x** installed, or PCSWMM, only if you use tool A to run the storms.
- A copy of **MHFD's latest Hydrology GDB template**. Never load into the only copy; work on a copy for each study.
- Your study's files: the SWMM `.inp`, the CUHP workbook (`.xlsm`) and interface files, and the subcatchment polygons.

## Install (one time, about 5 minutes)

1. Go to this repository's **Releases** page (right-hand side of the GitHub page) and download the `.zip` from the newest release (marked **Latest**).
2. Right-click the zip, choose **Properties**, and if you see an **Unblock** checkbox at the bottom, tick it and click OK. (Windows sometimes blocks scripts downloaded from the internet.)
3. Extract the zip to a folder on your computer, for example `C:\GIS\Tools\`. Keep the files together: the `.pyt` file needs the `mhfd_hydro` and `layer_files` folders sitting next to it.
4. Open ArcGIS Pro and your project. In the **Catalog** pane, right-click **Toolboxes** and choose **Add Toolbox**. Browse to the extracted folder and pick `MHFD_SWMM_to_Hydrology_GDB_Converter_v1.0.pyt` (the number matches the version you downloaded).
5. Expand the toolbox in Catalog. You should see tools **A**, **B** and **C**.

To update later, download the new release, extract it to a new folder, remove the old toolbox from Catalog and add the new `.pyt`. The version is in the toolbox's name, so you can always tell which one you're running.

## How to use it

Run the tools in letter order. Double-click a tool to open it; hover over any box to see what it wants.

| Step | Tool | What you give it | What you get |
|---|---|---|---|
| 1 | **A_Generate SWMM rpt files for all storms** | The SWMM `.inp` and the CUHP interface file for each storm | One SWMM report (`.rpt`) per storm. Skip A if you already have reports from your own runs. |
| 2 | **B_Load Subcatchments and Catchments** | The Hydrology GDB copy, the subcatchment polygons, the CUHP workbook, and the study area name | Subcatchments with all CUHP attributes, plus one catchment (the study area), added to the map |
| 3 | **C_Load SWMM Results to Hydrology GDB** | The same GDB, the `.inp`, the `.rpt` files, the MHFD study ID, and optionally the CUHP workbook, MHFD DRF CSV and NOAA Atlas 14 point | Design points, routing elements, peak flows, volumes, detention, rainfall, styled map layers, a topology check, and the QA report |

**Read the QA report.** When tool C finishes, its `.txt` QA report (covering all three tools) opens in Notepad. It starts with a one-line summary of every check, then lists problems as **FAIL** (fix before submitting), **WARN** (review), **PASS** and **INFO**. Each check explains what it looked at, why it matters, every item it found and what to do. Pro's own message window only shows part of this, so always use the `.txt`.

**Re-running is safe.** Tool C replaces the study's earlier rows instead of duplicating them, and never overwrites detention boundaries you've already drawn.

**Things you still do by hand:** draw the real detention facility outlines (C makes 1,000 ft placeholder squares, marked `PLACEHOLDER`), fill lifecycle status and as-built fields, and fix anything the QA report flags.

Full details on every input, check and table: [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md).

## Getting help and reporting problems

Open an **Issue** on this repository (Issues tab, **New issue**). Include the toolbox version, the tool letter, what you expected, what happened, and attach the QA report `.txt`. Don't attach client model files or geodatabases to an issue; say where they are on the project drive instead.

## What's in this repository

| Path | What it is |
|---|---|
| `MHFD_SWMM_to_Hydrology_GDB_Converter_v1.0.pyt` | The toolbox you add to ArcGIS Pro |
| `mhfd_hydro/` | The Python code behind the tools (SWMM/CUHP readers, QA checks, topology) |
| `layer_files/` | MHFD symbology for the Design Points and Routing Elements layers |
| `run_staging.py` | Preview what the tools would write, without ArcGIS (for developers) |
| `tests/` | Automated tests (for developers) |
| `docs/` | Tool reference and the release process |

## Versions

The toolbox is versioned as a whole (v1.0, v1.1, v2.0…); the tools inside it have no versions of their own. The version is in the `.pyt` file name, the toolbox name in Pro, and the QA report header. Each version is a GitHub release with the same tag (`v1.0`). The change history is in [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md#versions).

## For maintainers

See [docs/RELEASING.md](docs/RELEASING.md) for how to make a release, and what must never be committed (client data).

Maintained by Aevos Engineering. Internal use only; see [LICENSE](LICENSE).
