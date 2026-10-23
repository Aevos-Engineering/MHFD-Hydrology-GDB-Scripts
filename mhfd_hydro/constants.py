"""Schema constants from the MHFD Hydrology data dictionary v4.2."""

# hydro_swmm_node_type_long domain
NODE_TYPE_IDS = {"DIVIDER": 1, "JUNCTION": 2, "OUTFALL": 3, "STORAGE": 4}

# modeled_condition_type domain
CONDITION_IDS = {
    "Existing": 1,
    "Future": 2,
    "Pre-Developed": 3,
    "Master Planned": 4,
    "FEMA": 5,
    "Design": 6,
}

# Aliases accepted in file names and tool inputs, mapped to the domain label.
CONDITION_ALIASES = {
    "ex": "Existing", "exist": "Existing", "existing": "Existing",
    "fut": "Future", "future": "Future",
    "pre": "Pre-Developed", "predev": "Pre-Developed", "pre-developed": "Pre-Developed",
    "hist": "Pre-Developed", "historic": "Pre-Developed", "natural": "Pre-Developed",
    "mp": "Master Planned", "master": "Master Planned", "master planned": "Master Planned",
    "fema": "FEMA",
    "design": "Design",
}

# Return period key -> field suffix used in the peak flow / volume tables.
# Keys are what tool users type; "1%+" has a flow field but no volume field.
RETURN_PERIODS = ["WQ", "1", "2", "5", "10", "25", "50", "100", "500", "1%+"]

PEAK_FLOW_FIELD = {
    "WQ": "flow_wq_cfs", "1": "flow_1yr_cfs", "2": "flow_2yr_cfs",
    "5": "flow_5yr_cfs", "10": "flow_10yr_cfs", "25": "flow_25yr_cfs",
    "50": "flow_50yr_cfs", "100": "flow_100yr_cfs", "500": "flow_500yr_cfs",
    "1%+": "flow_1percentplus_cfs",
}
VOLUME_FIELD = {
    "WQ": "volume_wq_ac_ft", "1": "volume_1yr_ac_ft", "2": "volume_2yr_ac_ft",
    "5": "volume_5yr_ac_ft", "10": "volume_10yr_ac_ft", "25": "volume_25yr_ac_ft",
    "50": "volume_50yr_ac_ft", "100": "volume_100yr_ac_ft", "500": "volume_500yr_ac_ft",
}
DETENTION_VOLUME_FIELD = {k: "volume_max_" + v.split("_", 1)[1] for k, v in VOLUME_FIELD.items()}
# Detention summary flow fields are irregular in the template (WQ lacks "_cfs").
DETENTION_FLOW_FIELDS = {
    "WQ": ("flow_wq_in", "flow_wq_out"),
    **{rp: (f"flow_{rp}yr_cfs_in", f"flow_{rp}yr_cfs_out")
       for rp in ["1", "2", "5", "10", "25", "50", "100", "500"]},
}

# return_period domain (hydro_hour_rainfall.return_period_year_type_id)
RETURN_PERIOD_TYPE_IDS = {"WQ": 1, "1": 2, "2": 3, "5": 4, "10": 5, "25": 6, "50": 7,
                          "100": 8, "500": 9}

# ownership_type_id domain: 1 Private, 2 Unknown, 3 Public LG, 4 Public Other
OWNERSHIP_UNKNOWN = 2

# Unit conversions
MGAL_TO_ACFT = 1.0e6 / 325851.429  # 10^6 US gal -> acre-feet (3.0689)
KCF_TO_ACFT = 1000.0 / 43560.0     # 1000 ft3 -> acre-feet
SQMI_TO_AC = 640.0

DEFAULT_WKID = 6428  # NAD83(2011) StatePlane Colorado Central, US ft


def normalize_return_period(value):
    """'100yr', '100-yr', '100', 'Q100', '1%+', 'wq' -> canonical key or None."""
    s = str(value).strip().lower().replace(" ", "")
    if s in ("wq", "wqcv"):
        return "WQ"
    if s in ("1%+", "1%plus", "1percentplus", "onepercentplus", "1pctplus"):
        return "1%+"
    s = s.lstrip("q").replace("-", "").replace("_", "")
    for suffix in ("years", "year", "yrs", "yr", "y"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    s = s.lstrip("0") or "0"
    return s if s in RETURN_PERIODS else None


def normalize_condition(value):
    s = str(value).strip().lower()
    if s in CONDITION_ALIASES:
        return CONDITION_ALIASES[s]
    for label in CONDITION_IDS:
        if s == label.lower():
            return label
    return None
