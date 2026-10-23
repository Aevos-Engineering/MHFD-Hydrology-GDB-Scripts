# Maintaining and releasing

## Never commit client data

This repository holds code only. Before every commit, check that none of these are staged:

- Geodatabases (`*.gdb` folders), shapefiles, `.aprx` projects, rasters
- SWMM/CUHP model files: `.inp`, `.rpt`, `.out`, `.chi`, `.ini`, `.xlsm`, interface `.txt` files
- QA reports or results `.txt` from real studies
- Anything copied from the `P:` drive or a project folder

`.gitignore` blocks most of these, but it can't catch everything. Test data stays on the project drive; the tests find it through the `MHFD_TEST_DATA` and `MHFD_GDB_TEMPLATE` environment variables and skip themselves when it isn't there. If client data is ever pushed, deleting it in a new commit is not enough (it stays in history): tell the repo owner straight away.

## Branches

- `main` is always what's in the newest release or about to be. Protect it in Settings → Branches: require a pull request before merging.
- Do work on a short-lived branch (`fix-b-null-fields`, `c-hour-rainfall`), open a pull request, and merge when it's been tested in Pro.

## Versions

The toolbox is versioned as a whole: `v1.0`, then `v1.1` for fixes and small changes, `v2.0` for big changes. The tools have no versions of their own.

To make a new version:
1. Bump `TOOLBOX_VERSION` at the top of the `.pyt`.
2. Rename the `.pyt` to match (`MHFD_SWMM_to_Hydrology_GDB_Converter_v1.1.pyt`) and update the file name in `tests/test_loader.py` and `README.md`.
3. Add a line under "Versions" in `docs/TOOL_REFERENCE.md`.

The GitHub release tag is the same as the version (`v1.1`). For a test build before a release, use a tag with `beta` in it (`v1.1-beta1`); it is marked Pre-release automatically.

## Making a release

1. Make sure `main` has the code you tested in Pro, the version is bumped (above), and `python -m pytest tests -q` passes (with test data set).
2. On GitHub: **Releases** → **Draft a new release** → **Choose a tag** → type the new tag (e.g. `v1.1`) → **Create new tag on publish**, target `main`.
3. Title: `v1.1`. In the notes, say what changed in plain words and who should use it.
4. Click **Publish release**. The workflow in `.github/workflows/release.yml` builds `MHFD_SWMM_to_Hydrology_GDB_Converter_<tag>.zip` (toolbox, `mhfd_hydro`, `layer_files`, README, docs; no tests) and attaches it to the release within a minute or two. Tags containing `beta` are marked Pre-release.
5. Send testers the release link, not a copy of the files.
