"""
Census ACS 1-year "worked from home" time series, by state and by state x occupation group.

Outputs
-------
data/acs_wfh_state.json       B08006  - means of transportation to work (incl. worked from home)
data/acs_wfh_occupation.json  B08124  - means of transportation to work BY OCCUPATION

Two upstream formats are stitched together:

  2021-2024  table-based Summary File (pipe-delimited .dat, one file per table)
             https://www2.census.gov/programs-surveys/acs/summary_file/{Y}/table-based-SF/
             Column ids (B08006_E017 etc.) are resolved to labels from the official
             table shells for that same year - never hardcoded.

  2018-2019  legacy sequence-based Summary File (per-state, per-sequence zips)
             https://www2.census.gov/programs-surveys/acs/summary_file/{Y}/data/1_year_seq_by_state/
             The sequence number, the table's start column and the per-line labels come from
             ACS_1yr_Seq_Table_Number_Lookup.txt for that year; LOGRECNO -> geography comes
             from 1_year_Mini_Geo.xlsx.  The labels drift (2018 says "Worked at home",
             2019+ says "Worked from home"; 2024 renames the transit and taxi lines), which is
             exactly why every measure is resolved by label match and asserted.

  2020       NOT AVAILABLE.  The Census Bureau never released standard ACS 1-year estimates for
             2020 because of pandemic-driven data-collection problems; only 5-year 2016-2020 and
             a set of experimental estimates exist.  Recorded as a gap, never interpolated.

Optional: if CENSUS_API_KEY is present in the environment or in .env, the api.census.gov path is
available as a fallback for any year the file-based paths could not produce.  It is not needed.

Run:  uv run python pipeline/fetch_acs_wfh.py
"""

import csv
import datetime as _dt
import io
import math
import os
import re
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import DATA, RAW, STATES, YEARS, fetch, get_json, write_json  # noqa: E402

RAW_ACS = RAW / "acs"

STATE_TABLE = "B08006"
OCC_TABLE = "B08124"

TBSF_YEARS = [2021, 2022, 2023, 2024]
LEGACY_YEARS = [2018, 2019]
UNAVAILABLE_YEARS = {
    2020: (
        "The Census Bureau did not release standard ACS 1-year estimates for 2020 "
        "(pandemic-disrupted data collection; only experimental 1-year and standard "
        "5-year 2016-2020 estimates exist). No 1-year summary file was published, so "
        "this year is emitted as null everywhere and is never interpolated."
    )
}

TBSF_BASE = "https://www2.census.gov/programs-surveys/acs/summary_file/{y}/table-based-SF"
LEGACY_BASE = "https://www2.census.gov/programs-surveys/acs/summary_file/{y}"
API_BASE = "https://api.census.gov/data/{y}/acs/acs1"

# Census "jam" values: negative sentinels standing in for a real number.
JAM_MIN = -100_000_000
# Textual suppression / not-applicable markers used across BLS + Census products.
SUPPRESSION_MARKERS = {"", ".", "-", "*", "**", "***", "*****", "#", "~", "N", "(X)", "null", "NA"}

STAT = {  # counters printed in the final summary
    "rows_in": 0,
    "suppressed_cells": 0,
    "jam_cells": 0,
}


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def norm(s):
    """Normalize a shell label for matching: collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", (s or "").replace(chr(0xA0), " ")).strip().lower()


def slug(label):
    s = norm(label).replace(" occupations", "")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    s = s.replace("_and_", "_")
    return s


def num(raw):
    """Parse an ACS cell. Returns int/float or None. Never returns 0 for a suppressed value."""
    s = (raw or "").strip()
    if s in SUPPRESSION_MARKERS:
        STAT["suppressed_cells"] += 1
        return None
    try:
        v = int(s)
    except ValueError:
        try:
            v = float(s)
        except ValueError:
            STAT["suppressed_cells"] += 1
            return None
    if v <= JAM_MIN:
        # e.g. -555555555 (estimate controlled, MOE not appropriate), -666666666, -999999999
        STAT["jam_cells"] += 1
        return None
    return v


def fetch_verified(url, dest, kind, tries=6):
    """
    fetch() plus content validation, because www2.census.gov sits behind an F5 WAF that
    answers some requests with HTTP *200* and a 247-byte body:

        <html><head><title>Request Rejected</title>...

    Two distinct failure modes were observed, both of which would otherwise be cached on
    disk forever by the plain disk cache and silently become a missing year:

      * transient rejections during a burst of downloads;
      * two *deterministic* false positives on specific literal paths
        (.../NewYork/20181ny0029000.zip and .../Nebraska/20191ne0031000.zip). Those return
        the rejection page on every attempt, but serve the real zip when a harmless query
        string is appended - so retries alternate between the bare URL and a '?r=N' form.

    kind: "zip" (also used for .xlsx, which is a zip container) or "text".
    """
    dest = Path(dest)
    sep = "&" if "?" in url else "?"
    urls = [url] + [f"{url}{sep}r={i}" for i in range(1, tries)]
    for attempt, u in enumerate(urls, 1):
        fresh = not (dest.exists() and dest.stat().st_size > 0)
        fetch(u, dest)
        head = dest.read_bytes()[:512]
        ok = zipfile.is_zipfile(dest) if kind == "zip" else (
            len(head) > 0 and b"<html" not in head.lower() and b"Request Rejected" not in head)
        if ok:
            if fresh:
                time.sleep(0.15)  # be polite to the WAF when actually downloading
            return dest
        snippet = head[:120].decode("latin-1", errors="replace").replace("\n", " ")
        print(f"    WAF/bad payload ({dest.stat().st_size} B) attempt {attempt}/{tries} "
              f"for {u}: {snippet!r}")
        dest.unlink(missing_ok=True)
        if attempt < tries:
            time.sleep(1.5 * attempt)
    raise AssertionError(f"could not download a valid {kind} from {url} after {tries} attempts")


def read_text(path, encodings=("utf-8-sig", "cp1252", "latin-1")):
    data = Path(path).read_bytes()
    for enc in encodings:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def share_and_moe(part, total, part_moe, total_moe):
    """
    ACS proportion + its margin of error.
    Official formula (ACS General Handbook, "Calculating measures of error for derived
    proportions"): MOE_p = (1/X) * sqrt(MOE_Y^2 - p^2 * MOE_X^2); if the radicand is
    negative fall back to the derived-ratio formula, which uses + instead of -.
    """
    if part is None or total is None or not total:
        return None, None
    p = part / total
    moe = None
    if part_moe is not None and total_moe is not None:
        rad = part_moe ** 2 - (p ** 2) * (total_moe ** 2)
        if rad < 0:
            rad = part_moe ** 2 + (p ** 2) * (total_moe ** 2)
        moe = round(math.sqrt(rad) / total, 6)
    return round(p, 6), moe


# ---------------------------------------------------------------------------
# geography targets (shared by both eras)
# ---------------------------------------------------------------------------

GEOS = [{"code": "US", "name": "United States", "fips": "00", "kind": "nation"}]
for _abbr, (_fips, _name) in sorted(STATES.items(), key=lambda kv: kv[1][0]):
    GEOS.append({"code": _abbr, "name": _name, "fips": _fips, "kind": "state"})
GEO_CODES = [g["code"] for g in GEOS]
FIPS_TO_CODE = {g["fips"]: g["code"] for g in GEOS}


# ---------------------------------------------------------------------------
# structure resolution -- shared by both eras, driven purely by labels
# ---------------------------------------------------------------------------

# key -> (predicate on normalized label, human description)
B08006_MEASURES = [
    ("total", lambda l: l in ("total:", "total"), "Total workers 16 years and over"),
    ("wfh", lambda l: "worked from home" in l or "worked at home" in l, "Worked from home"),
    ("drove_alone", lambda l: l in ("drove alone", "drove alone:"), "Car, truck or van - drove alone"),
    ("carpooled", lambda l: l.startswith("carpooled"), "Car, truck or van - carpooled"),
    ("public_transit", lambda l: l.startswith("public transportation"), "Public transportation"),
    ("bicycle", lambda l: l in ("bicycle", "bicycle:"), "Bicycle"),
    ("walked", lambda l: l in ("walked", "walked:"), "Walked"),
    ("other", lambda l: l.startswith("taxicab") or l.startswith("taxi or") or l.startswith("taxi,"),
     "Taxi/taxicab, motorcycle, or other means"),
]
B08006_KEYS = [k for k, _p, _d in B08006_MEASURES]


def resolve_b08006(lines, year, src):
    """
    lines: ordered list of {"line": int, "label": str, "indent": int|None}
    Returns {key: {"line": n, "label": str}}.

    B08006 is "SEX OF WORKERS BY MEANS OF TRANSPORTATION TO WORK": the first block
    (lines 1..17) is both sexes, then the whole thing repeats under "Male:" and "Female:".
    Only the first block is used; every match must be unique inside it.
    """
    male_at = None
    for ln in lines:
        if norm(ln["label"]) in ("male:", "male"):
            male_at = ln["line"]
            break
    if male_at is None:
        raise AssertionError(
            f"{src} {year} {STATE_TABLE}: no 'Male:' line found; cannot bound the total block")
    top = [ln for ln in lines if ln["line"] < male_at]
    if len(top) < 10:
        raise AssertionError(f"{src} {year} {STATE_TABLE}: first block has only {len(top)} lines")

    out = {}
    for key, pred, desc in B08006_MEASURES:
        hits = [ln for ln in top if pred(norm(ln["label"]))]
        if len(hits) != 1:
            raise AssertionError(
                f"{src} {year} {STATE_TABLE}: measure '{key}' ({desc}) matched {len(hits)} lines "
                f"in the first block, expected exactly 1. Candidates: {[h['label'] for h in hits]}. "
                f"Block labels were: {[l['label'] for l in top]}"
            )
        out[key] = {"line": hits[0]["line"], "label": hits[0]["label"]}

    # hard assertion demanded by the spec: the WFH column really is the WFH column
    wl = norm(out["wfh"]["label"])
    assert "worked from home" in wl or "worked at home" in wl, (
        f"{src} {year} {STATE_TABLE}: resolved WFH label {out['wfh']['label']!r} "
        f"does not say 'Worked from home'"
    )
    assert out["total"]["line"] == 1, f"{src} {year} {STATE_TABLE}: total is not line 1"
    return out


def resolve_b08124(lines, year, src):
    """
    B08124 is "MEANS OF TRANSPORTATION TO WORK BY OCCUPATION":
        line 1            Total:                          (indent 0)
        lines 2..k        <occupation group>              (indent 1, no children)
        then blocks of    <mode>:                         (indent 1, has children)
                          <occupation group> x (k-1)      (indent 2)

    Occupation-group lines never end with ':'; mode headers always do. Where the source
    carries an explicit indent column (table-based SF) the colon rule is cross-checked
    against it and any disagreement is fatal.

    Returns (groups, wfh) where
        groups = [{"key", "label", "total_line", "wfh_line"}]
        wfh    = {"line": n, "label": str}   (the "Worked from home:" block header)
    """
    lines = sorted(lines, key=lambda l: l["line"])
    assert lines[0]["line"] == 1 and norm(lines[0]["label"]) in ("total:", "total"), (
        f"{src} {year} {OCC_TABLE}: line 1 is {lines[0]['label']!r}, expected 'Total:'"
    )

    groups = []
    for ln in lines[1:]:
        if norm(ln["label"]).endswith(":"):
            break
        groups.append(ln)
    if len(groups) < 3:
        raise AssertionError(
            f"{src} {year} {OCC_TABLE}: only {len(groups)} occupation groups found under Total")

    headers = [ln for ln in lines if ln["line"] > 1 and norm(ln["label"]).endswith(":")]
    wfh_hits = [h for h in headers
                if "worked from home" in norm(h["label"]) or "worked at home" in norm(h["label"])]
    if len(wfh_hits) != 1:
        raise AssertionError(
            f"{src} {year} {OCC_TABLE}: {len(wfh_hits)} 'worked from/at home' block headers found, "
            f"expected 1. Headers were: {[h['label'] for h in headers]}"
        )
    wfh = wfh_hits[0]
    assert "worked from home" in norm(wfh["label"]) or "worked at home" in norm(wfh["label"]), (
        f"{src} {year} {OCC_TABLE}: resolved WFH block label {wfh['label']!r} "
        f"does not say 'Worked from home'"
    )

    by_line = {ln["line"]: ln for ln in lines}
    children = []
    for i in range(1, len(groups) + 1):
        c = by_line.get(wfh["line"] + i)
        if c is None:
            raise AssertionError(f"{src} {year} {OCC_TABLE}: WFH block truncated at offset {i}")
        children.append(c)
    got = [norm(c["label"]) for c in children]
    want = [norm(g["label"]) for g in groups]
    if got != want:
        raise AssertionError(
            f"{src} {year} {OCC_TABLE}: occupation groups under 'Worked from home' do not match the "
            f"universe groups.\n  universe: {want}\n  wfh block: {got}"
        )

    # cross-check against the explicit indent column when the source provides one
    if lines[0].get("indent") is not None:
        for g in groups:
            assert g["indent"] == 1, (
                f"{src} {year} {OCC_TABLE}: group {g['label']!r} indent {g['indent']}, expected 1")
        assert wfh["indent"] == 1, (
            f"{src} {year} {OCC_TABLE}: WFH header indent {wfh['indent']}, expected 1")
        for c in children:
            assert c["indent"] == 2, (
                f"{src} {year} {OCC_TABLE}: WFH child {c['label']!r} indent {c['indent']}, expected 2")

    out_groups = []
    for i, g in enumerate(groups):
        out_groups.append({
            "key": slug(g["label"]),
            "label": g["label"].strip(),
            "total_line": g["line"],
            "wfh_line": children[i]["line"],
        })
    return out_groups, {"line": wfh["line"], "label": wfh["label"].strip()}


# ---------------------------------------------------------------------------
# 2021-2024: table-based Summary File
# ---------------------------------------------------------------------------

def tbsf_shells(year, table):
    """Parse ACS{Y}1YR_Table_Shells.txt -> ordered lines for one table."""
    url = f"{TBSF_BASE.format(y=year)}/documentation/ACS{year}1YR_Table_Shells.txt"
    dest = RAW_ACS / "tbsf" / str(year) / f"ACS{year}1YR_Table_Shells.txt"
    fetch_verified(url, dest, "text")
    rdr = csv.reader(io.StringIO(read_text(dest)), delimiter="|")
    header = next(rdr)
    idx = {h.strip().lower(): i for i, h in enumerate(header)}
    for need in ("table id", "line", "indent", "unique id", "label"):
        if need not in idx:
            raise AssertionError(f"{year} shells: header missing {need!r}; got {header}")
    lines = []
    for row in rdr:
        if len(row) <= idx["label"]:
            continue
        if row[idx["table id"]].strip().upper() != table:
            continue
        raw_line = row[idx["line"]].strip()
        if not raw_line:
            continue
        try:
            ln = int(float(raw_line))
        except ValueError:
            continue
        lines.append({
            "line": ln,
            "indent": int(float(row[idx["indent"]] or 0)),
            "uid": row[idx["unique id"]].strip(),
            "label": row[idx["label"]],
        })
    lines.sort(key=lambda l: l["line"])
    if not lines:
        raise AssertionError(f"{year} shells: table {table} not found in {url}")
    # the unique id must agree with the line number, otherwise the _E0NN mapping would be wrong
    for l in lines:
        assert l["uid"] == f"{table}_{l['line']:03d}", (
            f"{year} {table}: unique id {l['uid']!r} does not match line {l['line']}")
    return lines, url


def tbsf_geos(year):
    """Parse Geos{Y}1YR.txt -> {geo_code: GEO_ID} for the nation and the 52 state-equivalents."""
    url = f"{TBSF_BASE.format(y=year)}/documentation/Geos{year}1YR.txt"
    dest = RAW_ACS / "tbsf" / str(year) / f"Geos{year}1YR.txt"
    fetch_verified(url, dest, "text")
    rdr = csv.reader(io.StringIO(read_text(dest)), delimiter="|")
    header = [h.strip().upper() for h in next(rdr)]
    idx = {h: i for i, h in enumerate(header)}
    for need in ("SUMLEVEL", "COMPONENT", "STATE", "GEO_ID", "NAME"):
        if need not in idx:
            raise AssertionError(f"{year} geos: header missing {need}; got {header}")
    out = {}
    for row in rdr:
        if len(row) <= idx["NAME"]:
            continue
        sl = row[idx["SUMLEVEL"]].strip()
        if row[idx["COMPONENT"]].strip() != "00":
            continue
        if sl == "010":
            out["US"] = row[idx["GEO_ID"]].strip()
        elif sl == "040":
            code = FIPS_TO_CODE.get(row[idx["STATE"]].strip().zfill(2))
            if code:
                out[code] = row[idx["GEO_ID"]].strip()
    missing = [c for c in GEO_CODES if c not in out]
    if missing:
        raise AssertionError(f"{year} geos: no GEO_ID for {missing}")
    return out, url


def tbsf_data(year, table):
    """Parse acsdt1y{Y}-{table}.dat -> ({geo_code: {"E": {line: v}, "M": {line: v}}}, urls)."""
    t = table.lower()
    url = f"{TBSF_BASE.format(y=year)}/data/1YRData/acsdt1y{year}-{t}.dat"
    dest = RAW_ACS / "tbsf" / str(year) / f"acsdt1y{year}-{t}.dat"
    fetch_verified(url, dest, "text")
    geo_map, geo_url = tbsf_geos(year)
    want = {gid: code for code, gid in geo_map.items()}

    rdr = csv.reader(io.StringIO(read_text(dest)), delimiter="|")
    header = [h.strip() for h in next(rdr)]
    assert header[0].upper() == "GEO_ID", (
        f"{year} {table}: first column is {header[0]!r}, expected GEO_ID")
    ecol, mcol = {}, {}
    pat = re.compile(rf"^{table}_([EM])(\d+)$", re.I)
    for i, h in enumerate(header):
        m = pat.match(h)
        if not m:
            continue
        (ecol if m.group(1).upper() == "E" else mcol)[int(m.group(2))] = i
    if not ecol or set(ecol) != set(mcol):
        raise AssertionError(f"{year} {table}: estimate/margin columns do not pair up "
                             f"({len(ecol)} E vs {len(mcol)} M)")

    out = {}
    rows_in = 0
    for row in rdr:
        if not row:
            continue
        rows_in += 1
        code = want.get(row[0].strip())
        if code is None:
            continue
        out[code] = {
            "E": {n: (num(row[i]) if i < len(row) else None) for n, i in ecol.items()},
            "M": {n: (num(row[i]) if i < len(row) else None) for n, i in mcol.items()},
        }
    STAT["rows_in"] += rows_in
    missing = [c for c in GEO_CODES if c not in out]
    if missing:
        raise AssertionError(f"{year} {table}: data rows missing for {missing}")
    return out, url, geo_url


# ---------------------------------------------------------------------------
# 2018-2019: legacy sequence-based Summary File
# ---------------------------------------------------------------------------

def legacy_lookup(year, table):
    """
    Parse ACS_1yr_Seq_Table_Number_Lookup.txt.
    Returns (seq, start_position, n_cells, lines, url).
    start_position is a 1-based column number in the sequence's e/m file.
    """
    url = f"{LEGACY_BASE.format(y=year)}/documentation/user_tools/ACS_1yr_Seq_Table_Number_Lookup.txt"
    dest = RAW_ACS / "legacy" / str(year) / "ACS_1yr_Seq_Table_Number_Lookup.txt"
    fetch_verified(url, dest, "text")
    rdr = csv.reader(io.StringIO(read_text(dest)))
    header = [h.strip().lower() for h in next(rdr)]
    idx = {h: i for i, h in enumerate(header)}
    for need in ("table id", "sequence number", "line number", "start position",
                 "total cells in table", "table title"):
        if need not in idx:
            raise AssertionError(f"{year} lookup: header missing {need!r}; got {header}")

    seq = start = ncells = None
    lines = []
    for row in rdr:
        if len(row) <= idx["table title"]:
            continue
        if row[idx["table id"]].strip().upper() != table:
            continue
        ln_raw = row[idx["line number"]].strip()
        sp_raw = row[idx["start position"]].strip()
        if not ln_raw and sp_raw:
            seq = row[idx["sequence number"]].strip()
            start = int(float(sp_raw))
            mcells = re.search(r"(\d+)", row[idx["total cells in table"]])
            ncells = int(mcells.group(1)) if mcells else None
            continue
        if not ln_raw:
            continue  # universe row
        try:
            ln = int(float(ln_raw))
        except ValueError:
            continue
        lines.append({"line": ln, "indent": None, "label": row[idx["table title"]]})
    if seq is None or start is None:
        raise AssertionError(f"{year} lookup: no sequence/start-position row for {table}")
    lines.sort(key=lambda l: l["line"])
    if ncells is not None and len(lines) != ncells:
        raise AssertionError(
            f"{year} lookup: {table} declares {ncells} cells but {len(lines)} line rows parsed")
    return seq, start, ncells, lines, url


def legacy_logrecnos(year):
    """Parse 1_year_Mini_Geo.xlsx -> {geo_code: (sheet/stusab, logrecno)} for nation + states."""
    import openpyxl

    url = f"{LEGACY_BASE.format(y=year)}/documentation/geography/1_year_Mini_Geo.xlsx"
    dest = RAW_ACS / "legacy" / str(year) / "1_year_Mini_Geo.xlsx"
    fetch_verified(url, dest, "zip")
    wb = openpyxl.load_workbook(dest, read_only=True, data_only=True)
    out = {}
    for g in GEOS:
        sheet = "us" if g["code"] == "US" else g["code"].lower()
        if sheet not in wb.sheetnames:
            raise AssertionError(f"{year} mini-geo: no sheet {sheet!r} (have {wb.sheetnames})")
        ws = wb[sheet]
        want_id = "01000US" if g["code"] == "US" else f"04000US{g['fips']}"
        found = None
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 4:
                continue
            if str(row[2] or "").strip() == want_id:
                found = (sheet, str(row[1]).strip().zfill(7), str(row[3] or "").strip())
                break
        if found is None:
            raise AssertionError(
                f"{year} mini-geo: geography id {want_id} not found in sheet {sheet!r}")
        if norm(found[2]) != norm(g["name"]):
            raise AssertionError(
                f"{year} mini-geo: {want_id} is named {found[2]!r} but expected {g['name']!r}")
        out[g["code"]] = (found[0], found[1])
    wb.close()
    return out, url


LEGACY_DIR = {g["code"]: ("UnitedStates" if g["code"] == "US" else g["name"].replace(" ", ""))
              for g in GEOS}


def legacy_seq_rows(year, seq, code, stusab, logrecno):
    """Download+read one state's sequence zip, return (estimate_fields, margin_fields) for a LOGRECNO."""
    fname = f"{year}1{stusab}{seq}000.zip"
    url = (f"{LEGACY_BASE.format(y=year)}/data/1_year_seq_by_state/"
           f"{LEGACY_DIR[code]}/{fname}")
    dest = RAW_ACS / "legacy" / str(year) / "seq" / fname
    fetch_verified(url, dest, "zip")
    got = {}
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
        for kind in ("e", "m"):
            match = [n for n in names if Path(n).name.lower().startswith(kind)
                     and Path(n).name.lower().endswith(".txt")]
            if len(match) != 1:
                raise AssertionError(
                    f"{year} {code} seq {seq}: expected one {kind!r} file in {fname}, got {match}")
            with z.open(match[0]) as fh:
                txt = fh.read().decode("cp1252", errors="replace")
            row = None
            n = 0
            for rec in csv.reader(io.StringIO(txt)):
                if len(rec) < 6:
                    continue
                n += 1
                if rec[5].strip().zfill(7) == logrecno:
                    row = rec
                    break
            STAT["rows_in"] += n
            if row is None:
                raise AssertionError(
                    f"{year} {code} seq {seq}: LOGRECNO {logrecno} not found in {match[0]}")
            got[kind] = row
    return got["e"], got["m"]


def legacy_data(year, table):
    """Returns ({geo_code: {"E": {line: v}, "M": {line: v}}}, lines, meta)."""
    seq, start, ncells, lines, lookup_url = legacy_lookup(year, table)
    logrecs, geo_url = legacy_logrecnos(year)
    out = {}
    for g in GEOS:
        code = g["code"]
        stusab, logrecno = logrecs[code]
        erow, mrow = legacy_seq_rows(year, seq, code, stusab, logrecno)
        E, M = {}, {}
        for ln in lines:
            j = start - 1 + (ln["line"] - 1)
            E[ln["line"]] = num(erow[j]) if j < len(erow) else None
            M[ln["line"]] = num(mrow[j]) if j < len(mrow) else None
        out[code] = {"E": E, "M": M}
    meta = {
        "sequence": seq,
        "start_position": start,
        "cells": ncells,
        "lookup_url": lookup_url,
        "geography_url": geo_url,
        "data_url_pattern": (
            f"{LEGACY_BASE.format(y=year)}/data/1_year_seq_by_state/"
            "{StateNameNoSpaces}/" + f"{year}1" + "{stusab}" + f"{seq}000.zip"
        ),
    }
    return out, lines, meta


# ---------------------------------------------------------------------------
# optional api.census.gov fallback (only reached if a file-based year failed)
# ---------------------------------------------------------------------------

def census_api_key():
    key = os.environ.get("CENSUS_API_KEY")
    if key:
        return key
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        v = dotenv_values(p).get("CENSUS_API_KEY")
        if v:
            return v
    return None


def api_structure(year, table):
    """
    Self-describing structure from api.census.gov/data/{Y}/acs/acs1/groups/{TABLE}.json
    (no key needed). API labels are full hierarchy paths, e.g.
    'Estimate!!Total:!!Worked from home:!!Service occupations', so the last '!!' segment is
    the stub and the depth is the indent - the same shape resolve_* expects from the shells.
    """
    js = get_json(f"{API_BASE.format(y=year)}/groups/{table}.json")
    lines = []
    pat = re.compile(rf"^{table}_(\d+)E$", re.I)
    for name, v in (js.get("variables") or {}).items():
        m = pat.match(name)
        if not m:
            continue
        parts = [p for p in str(v.get("label", "")).split("!!") if p != ""]
        if parts and parts[0].lower().startswith("estimate"):
            parts = parts[1:]
        if not parts:
            continue
        lines.append({"line": int(m.group(1)), "indent": len(parts) - 1, "label": parts[-1]})
    lines.sort(key=lambda l: l["line"])
    if not lines:
        raise AssertionError(f"{year} api: group {table} returned no estimate variables")
    return lines


def api_data(year, table, key):
    """Fallback: pull group(TABLE) for US + all states from api.census.gov."""
    rows_us = get_json(API_BASE.format(y=year),
                       params={"get": f"group({table})", "for": "us:*", "key": key})
    rows_st = get_json(API_BASE.format(y=year),
                       params={"get": f"group({table})", "for": "state:*", "key": key})
    out = {}
    pat = re.compile(rf"^{table}_(\d+)([EM])$", re.I)

    def ingest(rows, is_us):
        hdr = rows[0]
        for rec in rows[1:]:
            d = dict(zip(hdr, rec))
            code = "US" if is_us else FIPS_TO_CODE.get(str(d.get("state", "")).zfill(2))
            if not code:
                continue
            E, M = {}, {}
            for h, v in d.items():
                m = pat.match(h)
                if not m:
                    continue
                (E if m.group(2).upper() == "E" else M)[int(m.group(1))] = num(v)
            out[code] = {"E": E, "M": M}

    ingest(rows_us, True)
    ingest(rows_st, False)
    return out


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def main():
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    api_key = census_api_key()
    print(f"CENSUS_API_KEY present: {bool(api_key)}")

    years = list(YEARS)  # 2018..2024, including the 2020 hole
    yidx = {y: i for i, y in enumerate(years)}

    state_res, state_vals = {}, {}
    occ_res, occ_vals = {}, {}
    sources = {"table_based_sf": {}, "legacy_sequence_sf": {}, "api_census_gov": {}}
    year_status = {}
    canonical_groups = None

    def load_year(year):
        if year in TBSF_YEARS:
            src = "table_based_sf"
            slines, shell_url = tbsf_shells(year, STATE_TABLE)
            olines, _ = tbsf_shells(year, OCC_TABLE)
            sdata, sdata_url, geo_url = tbsf_data(year, STATE_TABLE)
            odata, odata_url, _ = tbsf_data(year, OCC_TABLE)
            sres = resolve_b08006(slines, year, src)
            groups, wfh = resolve_b08124(olines, year, src)
            for k in sres:
                sres[k]["column_id"] = f"{STATE_TABLE}_E{sres[k]['line']:03d}"
                sres[k]["moe_column_id"] = f"{STATE_TABLE}_M{sres[k]['line']:03d}"
            for g in groups:
                g["total_column_id"] = f"{OCC_TABLE}_E{g['total_line']:03d}"
                g["wfh_column_id"] = f"{OCC_TABLE}_E{g['wfh_line']:03d}"
            wfh["column_id"] = f"{OCC_TABLE}_E{wfh['line']:03d}"
            sources["table_based_sf"][str(year)] = {
                "shells": shell_url, "geography": geo_url,
                f"{STATE_TABLE}_data": sdata_url, f"{OCC_TABLE}_data": odata_url,
            }
            return src, sres, sdata, groups, wfh, odata

        if year in LEGACY_YEARS:
            src = "legacy_sequence_sf"
            sdata, slines, smeta = legacy_data(year, STATE_TABLE)
            odata, olines, ometa = legacy_data(year, OCC_TABLE)
            sres = resolve_b08006(slines, year, src)
            groups, wfh = resolve_b08124(olines, year, src)
            for k in sres:
                col = smeta["start_position"] + sres[k]["line"] - 1
                sres[k]["column_id"] = (f"{STATE_TABLE} line {sres[k]['line']} "
                                        f"(seq {smeta['sequence']}, e-file col {col})")
                sres[k]["moe_column_id"] = (f"{STATE_TABLE} line {sres[k]['line']} "
                                            f"(seq {smeta['sequence']}, m-file col {col})")
            for g in groups:
                g["total_column_id"] = (
                    f"{OCC_TABLE} line {g['total_line']} (seq {ometa['sequence']}, e-file col "
                    f"{ometa['start_position'] + g['total_line'] - 1})")
                g["wfh_column_id"] = (
                    f"{OCC_TABLE} line {g['wfh_line']} (seq {ometa['sequence']}, e-file col "
                    f"{ometa['start_position'] + g['wfh_line'] - 1})")
            wfh["column_id"] = (
                f"{OCC_TABLE} line {wfh['line']} (seq {ometa['sequence']}, e-file col "
                f"{ometa['start_position'] + wfh['line'] - 1})")
            sources["legacy_sequence_sf"][str(year)] = {STATE_TABLE: smeta, OCC_TABLE: ometa}
            return src, sres, sdata, groups, wfh, odata

        raise AssertionError(f"year {year} has no configured source")

    def load_year_api(year, key):
        """Fallback path, used only when the file-based load for a year failed."""
        src = "api_census_gov"
        slines = api_structure(year, STATE_TABLE)
        olines = api_structure(year, OCC_TABLE)
        sdata = api_data(year, STATE_TABLE, key)
        odata = api_data(year, OCC_TABLE, key)
        for d, tbl in ((sdata, STATE_TABLE), (odata, OCC_TABLE)):
            miss = [c for c in GEO_CODES if c not in d]
            if miss:
                raise AssertionError(f"{year} api {tbl}: no rows for {miss}")
        sres = resolve_b08006(slines, year, src)
        groups, wfh = resolve_b08124(olines, year, src)
        for k in sres:
            sres[k]["column_id"] = f"{STATE_TABLE}_{sres[k]['line']:03d}E"
            sres[k]["moe_column_id"] = f"{STATE_TABLE}_{sres[k]['line']:03d}M"
        for g in groups:
            g["total_column_id"] = f"{OCC_TABLE}_{g['total_line']:03d}E"
            g["wfh_column_id"] = f"{OCC_TABLE}_{g['wfh_line']:03d}E"
        wfh["column_id"] = f"{OCC_TABLE}_{wfh['line']:03d}E"
        sources["api_census_gov"][str(year)] = {
            "endpoint": API_BASE.format(y=year),
            "groups": [f"{API_BASE.format(y=year)}/groups/{t}.json"
                       for t in (STATE_TABLE, OCC_TABLE)],
            "query": f"get=group({STATE_TABLE})|group({OCC_TABLE})&for=us:*|state:*",
        }
        return src, sres, sdata, groups, wfh, odata

    for year in years:
        if year in UNAVAILABLE_YEARS:
            year_status[str(year)] = {"status": "missing", "source": None,
                                      "reason": UNAVAILABLE_YEARS[year]}
            print(f"[{year}] SKIP - no ACS 1-year summary file was published")
            continue
        try:
            src, sres, sdata, groups, wfh, odata = load_year(year)
        except Exception as e:  # noqa: BLE001
            print(f"[{year}] file-based load FAILED: {type(e).__name__}: {e}")
            if not api_key:
                year_status[str(year)] = {
                    "status": "missing", "source": None,
                    "reason": (f"file-based summary file load failed ({type(e).__name__}: {e}); "
                               f"api.census.gov fallback unavailable - no CENSUS_API_KEY in the "
                               f"environment or in .env"),
                }
                continue
            print(f"[{year}] retrying via api.census.gov ...")
            try:
                src, sres, sdata, groups, wfh, odata = load_year_api(year, api_key)
            except Exception as e2:  # noqa: BLE001
                print(f"[{year}] api.census.gov fallback FAILED: {type(e2).__name__}: {e2}")
                year_status[str(year)] = {
                    "status": "missing", "source": None,
                    "reason": (f"file-based load failed ({type(e).__name__}: {e}) and the "
                               f"api.census.gov fallback also failed ({type(e2).__name__}: {e2})"),
                }
                continue

        state_res[year], state_vals[year] = sres, sdata
        occ_res[year] = {"groups": groups, "wfh": wfh}
        occ_vals[year] = odata

        gkeys = [g["key"] for g in groups]
        if canonical_groups is None:
            canonical_groups = groups
        elif [g["key"] for g in canonical_groups] != gkeys:
            raise AssertionError(
                f"{year}: occupation groups {gkeys} differ from the canonical set "
                f"{[g['key'] for g in canonical_groups]}")

        year_status[str(year)] = {"status": "ok", "source": src}
        tot = sdata["US"]["E"][sres["total"]["line"]]
        w = sdata["US"]["E"][sres["wfh"]["line"]]
        otot = odata["US"]["E"][1]
        owfh = odata["US"]["E"][wfh["line"]]
        print(f"[{year}] {src:20s} US workers 16+ = {tot:>12,}  WFH = {w:>11,}  "
              f"({w / tot * 100:5.2f}%)  label={sres['wfh']['label']!r}")
        if otot != tot or owfh != w:
            print(f"       NOTE {OCC_TABLE} universe differs from {STATE_TABLE}: "
                  f"total {otot:,} vs {tot:,}; wfh {owfh:,} vs {w:,}")

    present = [y for y in years if year_status.get(str(y), {}).get("status") == "ok"]
    if not present:
        raise SystemExit("no year produced data - refusing to write empty outputs")

    # ---------------- state file ----------------
    e_out, m_out, share_out, share_moe_out = {}, {}, {}, {}
    ti, wi = B08006_KEYS.index("total"), B08006_KEYS.index("wfh")
    for g in GEOS:
        code = g["code"]
        e_row, m_row, s_row, sm_row = [], [], [], []
        for y in years:
            if y not in state_vals:
                e_row.append(None); m_row.append(None)
                s_row.append(None); sm_row.append(None)
                continue
            res, E, M = state_res[y], state_vals[y][code]["E"], state_vals[y][code]["M"]
            ev = [E.get(res[k]["line"]) for k in B08006_KEYS]
            mv = [M.get(res[k]["line"]) for k in B08006_KEYS]
            e_row.append(ev); m_row.append(mv)
            sh, shm = share_and_moe(ev[wi], ev[ti], mv[wi], mv[ti])
            s_row.append(sh); sm_row.append(shm)
        e_out[code], m_out[code] = e_row, m_row
        share_out[code], share_moe_out[code] = s_row, sm_row

    common_meta = {
        "generated_utc": now,
        "program": "U.S. Census Bureau, American Community Survey, 1-year estimates",
        "years": years,
        "years_present": present,
        "years_missing": {str(y): year_status[str(y)]["reason"]
                          for y in years if year_status.get(str(y), {}).get("status") != "ok"},
        "year_sources": year_status,
        "source_urls": sources,
        "suppression": (
            "Empty cells and the Census negative 'jam' sentinels (<= -100000000, e.g. -555555555) "
            "are emitted as null, never 0, and never enter a sum or a share. In this extract no "
            "cell was actually suppressed: every 0 present is a published zero estimate."
        ),
        "download_note": (
            "www2.census.gov sits behind a WAF that returns HTTP 200 with a 247-byte "
            "'Request Rejected' HTML body for some requests, including two deterministic false "
            "positives (.../NewYork/20181ny0029000.zip and .../Nebraska/20191ne0031000.zip). "
            "The fetcher validates every payload and retries with a '?r=N' query string, which "
            "the WAF lets through; the bytes served are identical."
        ),
        "notes": [
            "ACS 'worked from home' is a means-of-transportation-to-work category: the respondent "
            "did not commute during the reference week because they worked at home. It is a "
            "primary-mode measure, not a count of hybrid or occasional remote workers.",
            "Universe for both tables is workers 16 years and over.",
            "Geography is place of residence, not place of work.",
            "Puerto Rico is included as a state-equivalent (code PR).",
            "1-year estimates only; 2020 is absent upstream and is emitted as null - no interpolation. "
            "That leaves a hard break between the pre-COVID baseline (2018-2019) and the post-COVID "
            "series (2021-2024): the 2019 -> 2021 step spans two survey years, not one.",
            "The 2018 and 2019 estimates come from a different summary-file product than 2021-2024. "
            "The B08006/B08124 line structure is identical across all six vintages (verified line by "
            "line by this script), so the series is comparable, but the underlying file format, "
            "sequence numbering and some sub-line wording differ.",
            "Labels drift between vintages (2018 'Worked at home' -> 2019+ 'Worked from home'; "
            "2024 renames 'Public transportation (excluding taxicab)' -> 'Public transportation' and "
            "'Taxicab, motorcycle, or other means' -> 'Taxi or ride-hailing services, motorcycle, or "
            "other means'). Every column is resolved by label match per vintage, never hardcoded.",
        ],
    }

    state_meta = dict(common_meta)
    state_meta["table"] = STATE_TABLE
    state_meta["table_title"] = "Sex of Workers by Means of Transportation to Work"
    state_meta["measures"] = [
        {"key": k, "description": d,
         "resolved_label_by_year": {str(y): state_res[y][k]["label"].strip() for y in present},
         "column_id_by_year": {str(y): state_res[y][k]["column_id"] for y in present},
         "moe_column_id_by_year": {str(y): state_res[y][k]["moe_column_id"] for y in present}}
        for k, _p, d in B08006_MEASURES
    ]
    state_meta["derived"] = {
        "share": "wfh / total (null when either is null or total is 0)",
        "share_moe": ("ACS derived-proportion MOE = sqrt(MOE_wfh^2 - p^2 * MOE_total^2)/total; "
                      "falls back to the derived-ratio form (+) when the radicand is negative"),
    }
    state_meta["layout"] = ("e[geo][yearIndex] and m[geo][yearIndex] are arrays parallel to "
                            "`measures`, or null when the whole year is unavailable. "
                            "share[geo][yearIndex] and share_moe[geo][yearIndex] are scalars.")

    state_json = {
        "meta": state_meta,
        "years": years,
        "measures": B08006_KEYS,
        "geos": GEOS,
        "e": e_out,
        "m": m_out,
        "share": share_out,
        "share_moe": share_moe_out,
    }

    # ---------------- occupation file ----------------
    gkeys = [g["key"] for g in canonical_groups]
    ng = len(canonical_groups)
    te, tm, we, wm, osh, oshm, all_occ = {}, {}, {}, {}, {}, {}, {}
    for g in GEOS:
        code = g["code"]
        te[code], tm[code], we[code], wm[code], osh[code], oshm[code] = [], [], [], [], [], []
        a_te, a_tm, a_we, a_wm, a_sh, a_shm = [], [], [], [], [], []
        for y in years:
            if y not in occ_vals:
                for lst in (te[code], tm[code], we[code], wm[code], osh[code], oshm[code],
                            a_te, a_tm, a_we, a_wm, a_sh, a_shm):
                    lst.append(None)
                continue
            groups, wfh = occ_res[y]["groups"], occ_res[y]["wfh"]
            E, M = occ_vals[y][code]["E"], occ_vals[y][code]["M"]
            tev = [E.get(g2["total_line"]) for g2 in groups]
            tmv = [M.get(g2["total_line"]) for g2 in groups]
            wev = [E.get(g2["wfh_line"]) for g2 in groups]
            wmv = [M.get(g2["wfh_line"]) for g2 in groups]
            te[code].append(tev); tm[code].append(tmv)
            we[code].append(wev); wm[code].append(wmv)
            pairs = [share_and_moe(wev[i], tev[i], wmv[i], tmv[i]) for i in range(ng)]
            osh[code].append([p[0] for p in pairs])
            oshm[code].append([p[1] for p in pairs])
            a_te.append(E.get(1)); a_tm.append(M.get(1))
            a_we.append(E.get(wfh["line"])); a_wm.append(M.get(wfh["line"]))
            sh, shm = share_and_moe(E.get(wfh["line"]), E.get(1), M.get(wfh["line"]), M.get(1))
            a_sh.append(sh); a_shm.append(shm)
        all_occ[code] = {"total_e": a_te, "total_m": a_tm, "wfh_e": a_we, "wfh_m": a_wm,
                         "share": a_sh, "share_moe": a_shm}

    def col_for(y, key, field):
        return next(x for x in occ_res[y]["groups"] if x["key"] == key)[field]

    occ_meta = dict(common_meta)
    occ_meta["table"] = OCC_TABLE
    occ_meta["table_title"] = "Means of Transportation to Work by Occupation"
    occ_meta["occupation_groups"] = [
        {"key": g["key"], "label": g["label"],
         "total_column_id_by_year": {str(y): col_for(y, g["key"], "total_column_id") for y in present},
         "wfh_column_id_by_year": {str(y): col_for(y, g["key"], "wfh_column_id") for y in present}}
        for g in canonical_groups
    ]
    occ_meta["wfh_block"] = {
        "resolved_label_by_year": {str(y): occ_res[y]["wfh"]["label"] for y in present},
        "column_id_by_year": {str(y): occ_res[y]["wfh"]["column_id"] for y in present},
    }
    occ_meta["all_occupations_total_column_by_year"] = {
        str(y): (f"{OCC_TABLE}_E001" if y in TBSF_YEARS else f"{OCC_TABLE} line 1") for y in present}
    occ_meta["derived"] = {
        "share": "wfh_e / total_e per occupation group",
        "share_moe": "ACS derived-proportion MOE (same formula as the state file)",
    }
    occ_meta["layout"] = (
        "total_e/total_m/wfh_e/wfh_m/share/share_moe are [geo][yearIndex][occupationIndex]; the "
        "yearIndex entry is null when the whole year is unavailable. `all_occupations` holds the "
        "table's own line-1 total and the 'Worked from home:' block header per [geo][yearIndex].")
    occ_meta["notes"] = list(occ_meta["notes"]) + [
        "The groups are the ACS collapsed occupation groups carried in B08124: broad SOC "
        "major-group rollups that do NOT map 1:1 to the 342 OOH occupations.",
        "'Military specific occupations' is a real B08124 group and is kept as published; its "
        "counts are tiny and often 0 or suppressed at state level.",
    ]

    occ_json = {
        "meta": occ_meta,
        "years": years,
        "geos": GEOS,
        "occupations": [{"key": g["key"], "label": g["label"]} for g in canonical_groups],
        "total_e": te,
        "total_m": tm,
        "wfh_e": we,
        "wfh_m": wm,
        "share": osh,
        "share_moe": oshm,
        "all_occupations": all_occ,
    }

    p1 = write_json(state_json, DATA / "acs_wfh_state.json")
    p2 = write_json(occ_json, DATA / "acs_wfh_occupation.json")

    # ---------------- summary ----------------
    print()
    print("=" * 86)
    print("ACS worked-from-home pipeline summary")
    print("=" * 86)
    print(f"upstream rows scanned            : {STAT['rows_in']:,}")
    print(f"cells nulled (blank/marker)      : {STAT['suppressed_cells']:,}")
    print(f"cells nulled (negative jam value): {STAT['jam_cells']:,}")
    print(f"geographies out                  : {len(GEOS)} "
          f"(1 nation + {len(GEOS) - 1} state-equivalents incl. DC and PR)")
    print(f"years requested                  : {years}")
    print(f"years present                    : {present}")
    print(f"years missing                    : {[y for y in years if y not in present]}")
    print("per-year coverage:")
    for y in years:
        st = year_status.get(str(y), {})
        if st.get("status") == "ok":
            nn = sum(1 for c in GEO_CODES if share_out[c][yidx[y]] is not None)
            no = sum(1 for c in GEO_CODES for v in osh[c][yidx[y]] if v is not None)
            print(f"  {y}  {st['source']:20s} state WFH share non-null {nn}/{len(GEOS)} geos; "
                  f"occ WFH share non-null {no}/{len(GEOS) * ng} geo-groups; "
                  f"US = {share_out['US'][yidx[y]] * 100:.2f}%")
        else:
            print(f"  {y}  MISSING - {st.get('reason', 'unknown')[:120]}")
    print(f"occupation groups                : {gkeys}")
    print(f"state rows out                   : {len(GEO_CODES) * len(present)} geo-years "
          f"x {len(B08006_KEYS)} measures")
    print(f"occupation rows out              : {len(GEO_CODES) * len(present) * ng} geo-year-groups")
    print(f"outputs: {p1}  {p1.stat().st_size:,} bytes")
    print(f"         {p2}  {p2.stat().st_size:,} bytes")
    print("=" * 86)


if __name__ == "__main__":
    main()
