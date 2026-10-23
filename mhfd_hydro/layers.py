"""MHFD layer files (layer_files/*.lyrx) pointed at a study's feature class.

The .lyrx files are MHFD's own symbology and labels with the SDE connection
removed. retarget_lyrx() rewrites the data connection to a file geodatabase
feature class and filters it to one study, so the loader can add the layer
with everything in the file (symbols, labels, scale ranges) intact.
"""

import copy
import json
import os

LAYER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "layer_files")

# feature class -> layer file
LAYER_FILES = {
    "hydro_design_point": "DesignPoints.lyrx",
    "hydro_routing_element": "RoutingElements.lyrx",
}


# Marker size factor per feature class (MHFD's design points crowd each other
# where nodes are close together).
MARKER_SCALE = {"hydro_design_point": 0.4}
_SIZED = ("CIMVectorMarker", "CIMCharacterMarker", "CIMPictureMarker", "CIMSimpleMarker",
          "CIMScaleDependentSizeVariation")


def scale_markers(obj, factor):
    """Multiply every marker size (and scale-dependent size) in layer JSON by factor.
    Label text sizes live in CIMTextSymbol 'height' and are left alone."""
    if isinstance(obj, dict):
        if obj.get("type") in _SIZED and isinstance(obj.get("size"), (int, float)):
            obj["size"] = round(obj["size"] * factor, 2)
        for v in obj.values():
            scale_markers(v, factor)
    elif isinstance(obj, list):
        for v in obj:
            scale_markers(v, factor)
    return obj


def retarget_lyrx(lyrx_path, gdb, feature_class, where=None):
    """Layer file JSON with every feature layer reading gdb/feature_class."""
    with open(lyrx_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc = copy.deepcopy(doc)
    for layer in doc.get("layerDefinitions", []):
        table = layer.get("featureTable")
        if not table:
            continue
        table["dataConnection"] = {
            "type": "CIMStandardDataConnection",
            "workspaceConnectionString": f"DATABASE={os.path.abspath(gdb)}",
            "workspaceFactory": "FileGDB",
            "dataset": feature_class,
            "datasetType": "esriDTFeatureClass",
        }
        if where:
            table["definitionExpression"] = where
            # Pro 3.x reads the active filter from definitionFilterChoices when present.
            table.pop("definitionFilterChoices", None)
            table.pop("definitionExpressionName", None)
    return doc


def write_retargeted(feature_class, gdb, where, out_dir):
    """Write the retargeted layer file for feature_class to out_dir; return its path."""
    src = os.path.join(LAYER_DIR, LAYER_FILES[feature_class])
    doc = retarget_lyrx(src, gdb, feature_class, where)
    if feature_class in MARKER_SCALE:
        scale_markers(doc, MARKER_SCALE[feature_class])
    out = os.path.join(out_dir, LAYER_FILES[feature_class])
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return out
