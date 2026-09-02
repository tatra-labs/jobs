"""
Shared helpers for the labor-market data pipeline.

Every fetch_*.py script uses these so downloads are cached on disk under raw/
and normalized outputs land in data/ with a consistent shape.
"""

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
DATA = ROOT / "data"
SITE_DATA = ROOT / "site" / "explore"

for _d in (RAW, DATA, SITE_DATA):
    _d.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) jobs-pipeline/1.0"


def fetch(url, dest, force=False, timeout=300, headers=None):
    """Download url to dest (a Path under raw/), caching on disk. Returns dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, headers=h) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def get_json(url, params=None, tries=4, timeout=90, headers=None):
    """GET a JSON endpoint with simple backoff."""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last = None
    for i in range(tries):
        try:
            r = httpx.get(url, params=params, timeout=timeout, follow_redirects=True, headers=h)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - retry any transport/status error
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def write_json(obj, path, compact=True):
    """Write JSON and report the size, so pipeline runs are self-documenting."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, separators=(",", ":"))
        else:
            json.dump(obj, f, indent=1)
    kb = path.stat().st_size / 1024
    print(f"wrote {path.relative_to(ROOT)}  ({kb:,.0f} KB)")
    return path


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Postal code -> (FIPS, name). 50 states + DC + PR, matching OEWS and ACS geographies.
STATES = {
    "AL": ("01", "Alabama"), "AK": ("02", "Alaska"), "AZ": ("04", "Arizona"),
    "AR": ("05", "Arkansas"), "CA": ("06", "California"), "CO": ("08", "Colorado"),
    "CT": ("09", "Connecticut"), "DE": ("10", "Delaware"), "DC": ("11", "District of Columbia"),
    "FL": ("12", "Florida"), "GA": ("13", "Georgia"), "HI": ("15", "Hawaii"),
    "ID": ("16", "Idaho"), "IL": ("17", "Illinois"), "IN": ("18", "Indiana"),
    "IA": ("19", "Iowa"), "KS": ("20", "Kansas"), "KY": ("21", "Kentucky"),
    "LA": ("22", "Louisiana"), "ME": ("23", "Maine"), "MD": ("24", "Maryland"),
    "MA": ("25", "Massachusetts"), "MI": ("26", "Michigan"), "MN": ("27", "Minnesota"),
    "MS": ("28", "Mississippi"), "MO": ("29", "Missouri"), "MT": ("30", "Montana"),
    "NE": ("31", "Nebraska"), "NV": ("32", "Nevada"), "NH": ("33", "New Hampshire"),
    "NJ": ("34", "New Jersey"), "NM": ("35", "New Mexico"), "NY": ("36", "New York"),
    "NC": ("37", "North Carolina"), "ND": ("38", "North Dakota"), "OH": ("39", "Ohio"),
    "OK": ("40", "Oklahoma"), "OR": ("41", "Oregon"), "PA": ("42", "Pennsylvania"),
    "RI": ("44", "Rhode Island"), "SC": ("45", "South Carolina"), "SD": ("46", "South Dakota"),
    "TN": ("47", "Tennessee"), "TX": ("48", "Texas"), "UT": ("49", "Utah"),
    "VT": ("50", "Vermont"), "VA": ("51", "Virginia"), "WA": ("53", "Washington"),
    "WV": ("54", "West Virginia"), "WI": ("55", "Wisconsin"), "WY": ("56", "Wyoming"),
    "PR": ("72", "Puerto Rico"),
}
NAME_TO_ABBR = {name: abbr for abbr, (_fips, name) in STATES.items()}
FIPS_TO_ABBR = {fips: abbr for abbr, (fips, _name) in STATES.items()}

# Years covered by the whole project. OEWS is published for May of each year.
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
