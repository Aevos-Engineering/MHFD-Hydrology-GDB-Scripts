# MHFD SWMM to Hydrology GDB Converter

An ArcGIS Pro toolbox that takes a CUHP/SWMM model set and fills the MHFD Hydrology GDB (built on MHFD v4.2; use the latest MHFD template), with the table relationships intact, and runs the MHFD QA checks.

## Install
1. Copy this whole folder (`MHFD_SWMM_to_Hydrology_GDB_Converter_v1.0.pyt` plus the `mhfd_hydro` and `layer_files` subfolders) somewhere local.
2. In ArcGIS Pro (3.3 or later): Catalog → Toolboxes → Add Toolbox → `MHFD_SWMM_to_Hydrology_GDB_Converter_v1.0.pyt`.

No extra packages are needed; it uses openpyxl, which ships with Pro.

## Tools
The letters give the run order:
- **A** makes the SWMM reports (skip it if you already have reports).
- **B** loads the subcatchment polygons and builds the catchment (the study area).
- **C** loads the SWMM results, including placeholder detention boundaries, adds every hydro layer and table to the map, checks topology, and writes the one QA report for all three tools.

### A_Generate SWMM rpt files for all storms
For when only the SWMM model and the CUHP interface files exist. It writes no QA report of its own: after the runs it shows each run's status (errors, continuity, instability, flooding) in Pro's messages, and tool C's QA report checks the runs in full. For each interface file it makes a temporary copy of the `.inp` whose `[FILES]` section reads `USE INFLOWS` from that file (other `USE INFLOWS` and any `SAVE` lines are dropped), runs SWMM, and writes only `<model>_Ex_100yr_SWMM5.2.4.rpt` into the folder you pick, or next to the `.inp` when left blank. The names carry condition and storm, so tool C fills those in on its own. Interface file names don't say the condition, so pick it in the table. Relative paths in the model still resolve from the model's folder.

SWMM engine: leave blank to use EPA SWMM 5.x from Program Files (`runswmm.exe`), or browse to `runswmm.exe` / `swmm5.dll` (PCSWMM's `swmm5.dll` works too).

### B_Load Subcatchments and Catchments
| Input | Notes |
|---|---|
| Subcatchment polygons | Shapefile or feature class, one polygon per CUHP subcatchment |
| Subcatchment name field | Picked automatically: the text field whose values match the most CUHP names (case and spaces ignored) |
| CUHP workbook | Fills every CUHP parameter: area, lengths, slope, imperviousness, depression storage, infiltration, override flag |
| CUHP workbook condition (only used when the Multiple Runs tab is blank) | Existing (default) or Future. When the Multiple Runs tab has Existing and Future % imperviousness, both are loaded from it and this box is ignored. Otherwise the Subcatchments tab's single % impervious column goes to the imperviousness field for this condition, and the other condition's field is left blank (or 0 where the GDB won't allow blank) |
| Catchment name | The study area name, e.g. South Outfall. The catchment is always all the subcatchments merged into one polygon |

**hydro_subcatchment** gets one row per matched polygon, named with the CUHP spelling. **hydro_catchment** gets one row: all the subcatchments merged, named with the catchment name, with total area, subbasin count, average subbasin area and area-weighted Ex/Fut imperviousness from CUHP; every subcatchment's `hydro_catchment_id` points at it. Fields the geodatabase won't allow blank get the field's default, or 0. Created/edited user and date are filled (signed-in ArcGIS user, else the Windows login; UTC) where editor tracking is off, and blank ids the template's attribute rules miss get the next unused number. It adds **hydro_catchment** (red outline) and **hydro_subcatchment** (blue outline, labeled by name) to the open map filtered to the study. B writes no file: its notes show in Pro's messages, and tool C's QA report rechecks the subcatchments from the GDB (missing polygons, area vs CUHP within 1%, imperviousness).

### C_Load SWMM Results to Hydrology GDB
| Input | Notes |
|---|---|
| Hydrology geodatabase | A copy of MHFD's latest Hydrology GDB template (the tools were built on v4.2) |
| SWMM input file | The `.inp` (PCSWMM's `.chi` next to it sets the coordinate system automatically) |
| SWMM report files | One `.rpt` per condition and storm, from tool A or your own runs. Condition and return period fill in from the file name when it contains them (`_Ex_100yr`, `_Fut_010yr`, MHFD/CUHP's `1_Ex_100yr_5mi^2_…`); otherwise pick them in the table |
| MHFD study ID | Written to every row |
| CUHP workbook | Optional. Gives contributing drainage areas and the 1-hr/6-hr rainfall depths |
| MHFD DRF CSV | Optional. Output of MHFD's *Calculate Depth Reduction Factors*. With area-corrected runs listed in the report table (DRF area column), each design point reads its peak from the matching run, as MHFD's *Compile SWMM Results* does |
| Replace rows | On by default: re-running replaces this study's rows instead of duplicating them |
| Load detention / placeholder side | On by default, 100 ft x 100 ft squares (see step 4) |
| Vertical datum | Datum of the SWMM elevations, NAVD88 by default; fills `vertical_datum` in the rating curve table |
| Design point status | Optional text, e.g. Master Plan, LOMR |
| NOAA Atlas 14 point | The point the rainfall depths are for: pick a point layer, or draw one. The tool never invents it; without one the hour rainfall still loads, unlinked, and the QA report FAILs |
| QA report | Blank = `<model>_C_QA.txt` next to the model. The one QA report for tools A, B and C; rerunning C replaces it |

What it writes, in this order:
1. **hydro_design_point**: every SWMM node (the routing table needs a design point at both ends of every link). SWMM tags go into `location`.
2. **hydro_routing_element**: every link, with node vertices, and from/to design point IDs. Comments are blank for conduits and name the link type for weirs, orifices, outlets and pumps.
3. **hydro_peak_flow** and **hydro_runoff_volume**: one row per design point per condition, all storms across. Volumes are converted from 10⁶ gal to ac-ft. The tool reads the relationship classes in the GDB and fills whichever key they use (`hydro_design_point_id`, or `GlobalID`→`rel_globalid`).
4. **hydro_detention_boundary**, **hydro_detention_summary** and **hydro_detention_rating_curve**. MHFD's guidance doesn't say what the boundary should trace, so every storage node without a boundary for the study gets a placeholder square (100 ft by default) centered on the node, with `node_name`, the nodes immediately up and downstream, the upstream drainage area, ownership "Unknown", and a comment starting `PLACEHOLDER`. Boundaries that already exist are never touched, so edited outlines survive re-runs; the tool reports how many are still placeholders. Lifecycle status and as-built fields are left for the user. Then the summary and rating curves load for every storage node. The rating curve comes from the storage curve (stage, area, volume) and any TABULAR outlet curves (discharge). Weirs and orifices are not in the discharge column; the row comment says so. The boundaries' design point IDs are refreshed. A boundary with no `hydro_detention_id` (the template's attribute rule didn't fill it) gets the next unused number, and the summary and rating curve rows carry the same ID. `vertical_datum` comes from the datum box.
5. **hydro_point_precipitation** and **hydro_hour_rainfall**: the NOAA Atlas 14 point and one row per storm with the 1-hr and 6-hr depths from the CUHP storm sheets. The hour rainfall always loads. With no point given, a point already loaded for the study is reused; with none at all, the rows load unlinked and the QA report FAILs until a point is given and C is rerun.

Then it recalculates the feature class extents and adds every hydro layer to the open map, filtered to the study: catchment, subcatchments, detention boundaries, routing elements, design points and the NOAA point. Design points and routing elements use MHFD's symbology and labels (`layer_files/`), with design point markers at 40% of MHFD's size so close nodes stay readable. It also adds the peak flow, runoff volume, detention summary, rating curve and hour rainfall tables, with the rating curve shown to 2/1/3/2 decimals (the template stores those fields single precision, so Pro would otherwise show digits like 5123.4502). Earlier copies for the study are replaced, and it zooms to the study. With no map open it skips this and says so. Then it runs the topology check (below) and writes the QA report (checks below).

IDs come from the template's attribute rules, so the tool inserts design points first and reads their IDs back before writing anything that refers to them. Where a rule leaves an ID blank, the tool gives it the next unused number. Peak flow and runoff volume rows get both keys the template's relationship classes use (`hydro_design_point_id` and `rel_globalid`), so a design point's pop-up shows its results. Created/edited user and date are filled (signed-in ArcGIS user, else Windows login; UTC, like editor tracking) on tables where editor tracking is off. It runs in one edit operation and rolls back on error.

### Topology check (end of tool C)
After loading, tool C checks the whole study in the Hydrology GDB, so there's no separate tool to run:
- **Subcatchments** (`hydro_subcatchment`, from tool B): no overlaps between polygons, no gaps or holes (it merges all the subcatchments and reports every hole in the result), and no multipart polygons. Together these mean neighbouring boundaries are coincident. Overlaps and gaps smaller than 1 sq ft are ignored.
- **Design points and routing elements** (from tool C): every routing element starts and ends on a design point within 0.1 ft, and on the design points its from/to IDs name. It also flags design points that no routing element starts or ends at, and two design points stacked in the same place.

Each problem becomes a point in `MHFD_topology_errors_<study>` in the scratch geodatabase, with `check_type` and `detail`, added to the open map. The QA report gets a pass/fail line per check listing every problem. Fix them (subcatchments: re-run B) and run C again. Tool C builds routing elements from the SWMM node coordinates, so they start out snapped; the network check catches later edits and anything loaded by other means.

### QA report (written by C, covering A, B and C)
One plain text file (`<model>_C_QA.txt`) that opens in Notepad when C finishes. It starts with a summary, then SUMMARY OF QC CHECKS (every check by topic with its status and a one-sentence description, topology last), then the checks grouped FAIL (fix before submitting), WARN (review), PASS and INFO. Each check says what it checks, why it matters, what was found (every flagged item, not just the first few) and, for FAIL and WARN, what to do. If C had to fill blank fields the geodatabase requires, a "Blank required fields" WARN lists them.

Checks:
- SWMM georeferenced in Colorado Central StatePlane
- Topology check: orphan nodes (SWMM, and in C the GDB design points too), plus C's subcatchment overlaps, gaps and multipart polygons, conduit dangles and end points, and stacked design points, each reported as e.g. "No orphan nodes" or "Conduit dangles found: 3"
- Per run: errors, continuity error (warn above 1%, fail above 5%), instability, flooding, warnings
- Per run: results populated (fails if a run gives no peaks or design points are missing from the report)
- SWMM engine build matches across runs and with the model
- Storms and conditions supplied vs the scope (Existing and Future at 2, 5, 10, 25, 50, 100, 500-yr)
- CUHP target nodes exist in SWMM, including names that differ only by case
- Subcatchment size: average ≤ 100 ac (WARN above); any over 130 ac are listed for information only, never a fail
- DRFs considered where design points drain 2 sq mi or more
- Subcatchments (tool B, read back from the GDB): every CUHP subcatchment has a polygon, areas within 1% of CUHP, Existing and Future imperviousness filled
- Related records: every peak flow, runoff volume, detention and rainfall row links to its parent through the GDB relationship classes (lists any ids the tool had to fill)
- Vertical datum given for the rating curves
- All CUHP area reaches the SWMM network (none drains to a missing target node)
- Peak flows rise with return period
- Future vs existing 100-yr increase above 30%
- Storage nodes without a usable outlet rating
- Hour rainfall: depths loaded from CUHP; FAIL when there's no NOAA Atlas 14 point

### SWMM build check (tools A and C)
Peak flows only reproduce exactly on the same SWMM engine build, so the tools make the engine and build visible:
- In tool A, when the engine box is blank, the tool fills it with the engine it found, so you see exactly which `runswmm.exe` / `swmm5.dll` will run. Browse to a different one to override.
- Two greyed-out boxes show the build the model was made with (from the model's own `.rpt` header, else PCSWMM's `.chi`, else EPA SWMM's `.ini`) and the build the runs use (each listed `.rpt` header, and the engine for interface files).
- If they differ, the build box shows a warning. The QA report has a "SWMM engine build" line.
- Reports the tool writes carry the build in the name: `<model>_Ex_100yr_SWMM5.2.4.rpt`.

## How contributing drainage area is computed
Each CUHP subcatchment's area is carried to every node downstream of its target node. Where flow splits (dividers, ponds with overflow weirs), both branches carry the full upstream area, so a node's area is everything that can drain to it. Outfall areas can therefore add up to more than the CUHP total (about 18.8 sq mi vs 9.6 sq mi in the South Outfall model). If you supply MHFD's DRF CSV, its areas are used instead.

## Without ArcGIS
`python run_staging.py --inp model.inp --rpt run1.rpt --rpt run2.rpt --cuhp model.xlsm --out folder` writes the same tables as CSV/GeoJSON plus `QA_report.txt`, for checking before a load.

Tests: `python -m pytest tests -q` with `MHFD_TEST_DATA` and `MHFD_GDB_TEMPLATE` set (see `tests/test_loader.py`). The GDB writer is tested against a stand-in for arcpy, so the first real run in Pro is still the true test.

## Known limits
- Detention boundaries made by the load tool are placeholders; the facility outlines, lifecycle status and as-built fields are still the user's to fill.
- The map step needs the tool run from ArcGIS Pro with a map open.
- Ids the tool fills (when an attribute rule leaves one blank) continue from the largest id in the table. If the rule starts working again later, its sequence may hand out a number the tool already used; the Related records check would show the clash.

## Versions
The toolbox is versioned as a whole; the tools carry no versions of their own. The version is in the `.pyt` file name, the toolbox name in Pro, the top of each tool's messages and the QA report header. To release a new version, bump `TOOLBOX_VERSION` at the top of the `.pyt`, rename the file to match, and add a line here.

- **v1.0** (published 2026-10-23): first release. A_Generate SWMM rpt files for all storms, B_Load Subcatchments and Catchments, and C_Load SWMM Results to Hydrology GDB, with one QA report written by C for all three tools. Built and tested on the MHFD Hydrology GDB v4.2 template. Earlier betas (2026-10-21 to 2026-10-23) carried per-tool versions (A v01-v02, B v000-v003, C v001-v006).
