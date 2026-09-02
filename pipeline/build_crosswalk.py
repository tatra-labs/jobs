"""
build_crosswalk.py - SOC 2018 hierarchy + OOH <-> SOC <-> OEWS crosswalk.

Outputs
-------
data/soc_structure.json   the full SOC 2018 tree (major -> minor -> broad -> detailed)
data/soc_crosswalk.json   every OOH occupation joined to SOC codes and to the OEWS
                          occupation universe, per year, plus category->major-group
                          and a curated IT occupation list.

Sources (all verified live by this script)
-----------------------------------------
* https://www.bls.gov/soc/2018/soc_structure_2018.xlsx  - the official SOC 2018
  structure.  The URL in the brief is correct; it just 403s for every non-browser
  client (see common.fetch_bls).
* https://www.bls.gov/soc/2018/soc_2010_to_2018_crosswalk.xlsx and
  https://www.bls.gov/soc/2018/soc_2010_codes_deleted_in_2018.xlsx - the official
  2010->2018 SOC bridge.  Both are live at those paths (HTTP 200) and are read
  here.  They are used ONLY to LABEL the May-2018 point of each series as
  comparable or not with May 2019+; no number is ever bridged or restated.
* https://www.bls.gov/oes/special-requests/oesm{18..24}nat.zip - OEWS national
  files, used ONLY as the occupation universe (which codes OEWS publishes, at
  which aggregation level, in which year).  No wage/employment value is read.
  NOTE: the national file publishes ~15 codes TWICE in the same year - one row
  tagged o_group=broad and one tagged o_group=detailed, with byte-identical
  estimates.  build_oews_universe() keeps BOTH labels per (code, year); a
  last-wins dict would hide the duplication and let a consumer sum the same row
  twice.
* occupations.csv  - the 342 OOH occupations; its `soc_code` column is populated
  for 290 of them.
* html/<slug>.html - the already-scraped OOH detail pages.  Each carries BLS's own
  "Employment projections data for ..." table (table#outlook-table) with a SOC
  Code cell per row.  That table is the AUTHORITATIVE OOH->SOC mapping and is how
  the 52 umbrella pages (e.g. "Software developers, quality assurance analysts,
  and testers") get resolved.  Nothing here is guessed from titles.

Table shape, which matters for not double counting:
    row with no indent class  = the OOH occupation itself; its SOC cell is either
                                a real code or an em dash
    rows with class "sub1"    = its immediate components
    rows with class "sub2"    = components of the sub1 above them
So: if the root row has a code, the OOH page IS that code.  If it is an em dash,
the page is the aggregate of the sub1 codes (never the sub2 codes - those are
already inside their sub1 parent).

Run:  uv run python pipeline/build_crosswalk.py
"""

import csv
import io
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DATA,
    RAW,
    ROOT,
    YEARS,
    close_bls_browser,
    fetch_bls,
    write_json,
)

RAW_GEO = RAW / "geo"
HTML_DIR = ROOT / "html"
OCC_CSV = ROOT / "occupations.csv"

SOC_STRUCTURE_URL = "https://www.bls.gov/soc/2018/soc_structure_2018.xlsx"
SOC_XWALK_URL = "https://www.bls.gov/soc/2018/soc_2010_to_2018_crosswalk.xlsx"
SOC_DELETED_URL = "https://www.bls.gov/soc/2018/soc_2010_codes_deleted_in_2018.xlsx"
SOC_REFERER = "https://www.bls.gov/soc/2018/"
OEWS_NAT_URL = "https://www.bls.gov/oes/special-requests/oesm{yy}nat.zip"
OEWS_REFERER = "https://www.bls.gov/oes/tables.htm"

CODE_RE = re.compile(r"^\d{2}-\d{4}$")
LEVELS = ["major", "minor", "broad", "detailed"]

# OEWS o_group values, broadest first.  Used to pick ONE canonical level for a
# code that OEWS publishes under two different o_group labels in the same year.
OEWS_LEVEL_RANK = {"total": 0, "major": 1, "minor": 2, "broad": 3, "detailed": 4}

# Vintage statuses that make the May-2018 point NOT comparable with 2019+.
# ("oews_combined_code" and "no_2010_counterpart" are informational: the series
# is consistent, we just cannot describe it in SOC terms from the crosswalk.)
BREAKING_VINTAGE_STATUSES = {"split", "absorbed", "renumbered",
                             "group_membership_changed"}


def canonical_oews_level(groups):
    """The broadest o_group OEWS used for a code in one year.

    ~15 codes per year are published TWICE in the same national file - once
    tagged `broad` and once tagged `detailed` - with byte-identical estimates.
    Every one of them is a broad group in SOC 2018 whose detailed members OEWS
    does not break out, so `broad` is the structurally correct label and the
    `detailed` twin is a publication artefact, not a second occupation.
    """
    return min(groups, key=lambda g: OEWS_LEVEL_RANK.get(g, 99))

# BLS suppression / not-applicable markers.  None of them are ever a code.
SUPPRESSED = {"*", "**", "#", "~", "-", "—", "–", "�", ""}


# ---------------------------------------------------------------------------
# 1. SOC 2018 structure
# ---------------------------------------------------------------------------
def build_soc_structure():
    path = fetch_bls(SOC_STRUCTURE_URL, RAW_GEO / "soc_structure_2018.xlsx",
                     referer=SOC_REFERER)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Locate the header row by content, never by position - BLS moves the
    # preamble around between vintages.
    hdr_i = None
    for i, r in enumerate(rows):
        cells = [str(c).strip().lower() if c else "" for c in r]
        if "major group" in cells and "detailed occupation" in cells:
            hdr_i = i
            break
    if hdr_i is None:
        raise SystemExit("could not find the header row in soc_structure_2018.xlsx")
    hdr = [str(c).strip().lower() if c else "" for c in rows[hdr_i]]
    col = {}
    for want, key in (("major group", "major"), ("minor group", "minor"),
                      ("broad group", "broad"), ("detailed occupation", "detailed")):
        col[key] = hdr.index(want)
    # the title column is the one with no header, to the right of the code columns
    title_col = max(col.values()) + 1
    if title_col >= len(hdr):
        raise SystemExit("no title column to the right of the code columns")
    print(f"soc_structure_2018.xlsx: header at row {hdr_i + 1}, "
          f"code columns {col}, title column {title_col}")

    codes = []
    seen = {}
    running = {}          # level -> most recent code at that level
    rows_in = 0
    skipped_rows = 0
    for r in rows[hdr_i + 1:]:
        if r is None or all(c is None for c in r):
            continue
        rows_in += 1
        level = None
        code = None
        for lv in LEVELS:
            v = r[col[lv]]
            if v is not None and str(v).strip():
                level, code = lv, str(v).strip()
                break
        title = r[title_col]
        title = str(title).strip() if title is not None else ""
        if code is None or not CODE_RE.match(code) or not title:
            skipped_rows += 1
            continue
        depth = LEVELS.index(level)
        parent = running.get(LEVELS[depth - 1]) if depth > 0 else None
        running[level] = code
        for deeper in LEVELS[depth + 1:]:
            running.pop(deeper, None)
        if code in seen:
            raise SystemExit(f"duplicate SOC code in the structure file: {code}")
        seen[code] = True
        codes.append({"code": code, "title": title, "level": level, "parent": parent})

    by_code = {c["code"]: c for c in codes}
    orphans = [c["code"] for c in codes
               if c["level"] != "major" and (c["parent"] is None or c["parent"] not in by_code)]
    if orphans:
        raise SystemExit(f"SOC codes with an unresolvable parent: {orphans[:10]}")

    # Sanity: a detailed code's broad parent should be its own code with a 0 last
    # digit.  Minor groups do NOT follow a digit rule in SOC 2018 (15-1200 sits
    # under 15-0000), which is exactly why parents come from the sheet order.
    mismatched = [c["code"] for c in codes
                  if c["level"] == "detailed" and c["parent"] != c["code"][:-1] + "0"]
    n_by_level = Counter(c["level"] for c in codes)
    print(f"  rows in {rows_in}  (skipped {skipped_rows} non-code rows)")
    print(f"  codes out {len(codes)}: " +
          ", ".join(f"{lv}={n_by_level[lv]}" for lv in LEVELS))
    print(f"  detailed codes whose broad parent is NOT code[:-1]+'0': "
          f"{len(mismatched)} {mismatched[:8]}")

    major_group = {c["code"][:2]: {"code": c["code"], "title": c["title"]}
                   for c in codes if c["level"] == "major"}
    struct = {
        "vintage": "SOC 2018",
        "source": {
            "url": SOC_STRUCTURE_URL,
            "note": "US Bureau of Labor Statistics / OMB Standard Occupational "
                    "Classification, 2018 revision. Public domain.",
        },
        "levels": LEVELS,
        "codes": codes,
        "major_group": major_group,
    }
    return struct, by_code


# ---------------------------------------------------------------------------
# 2. OEWS occupation universe (which codes exist, at which level, per year)
# ---------------------------------------------------------------------------
def build_oews_universe():
    """{year: {code: (level, title, groups)}} from the OEWS national files.

    `groups` is EVERY o_group value OEWS published for that code in that year.
    It usually has one element; for the handful of codes OEWS prints twice it
    has two ("broad", "detailed") and the two rows carry identical estimates.
    `level` is the canonical (broadest) one.  Reading the file into a plain
    {code: row} dict silently drops the first of the two rows and makes the
    duplication invisible, which is exactly how a frontend ends up summing the
    same employment twice.
    """
    universe = {}
    for year in YEARS:
        yy = f"{year % 100:02d}"
        zpath = fetch_bls(OEWS_NAT_URL.format(yy=yy), RAW_GEO / f"oesm{yy}nat.zip",
                          referer=OEWS_REFERER)
        z = zipfile.ZipFile(zpath)
        names = [i.filename for i in z.infolist() if i.filename.lower().endswith("_dl.xlsx")]
        if len(names) != 1:
            raise SystemExit(f"expected one *_dl.xlsx in {zpath.name}, got {names}")
        wb = openpyxl.load_workbook(io.BytesIO(z.read(names[0])), read_only=True,
                                    data_only=True)
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h).strip().lower() if h is not None else "" for h in next(it)]
        # Column names drift: 2018 has OCC_GROUP, 2019 is lowercase, 2020+ is
        # O_GROUP with a dozen extra geography columns in front.
        try:
            ci = hdr.index("occ_code")
            ti = hdr.index("occ_title")
            gi = hdr.index("o_group") if "o_group" in hdr else hdr.index("occ_group")
        except ValueError as e:
            raise SystemExit(f"{names[0]}: unexpected header {hdr}") from e
        rows = {}
        n_rows = 0
        for r in it:
            if r[ci] is None:
                continue
            n_rows += 1
            code = str(r[ci]).strip()
            if code in SUPPRESSED:
                continue
            grp = str(r[gi]).strip().lower() if r[gi] is not None else ""
            title = str(r[ti]).strip() if r[ti] is not None else ""
            if code in rows:
                prev_lvl, prev_title, prev_groups = rows[code]  # noqa: F841
                groups = tuple(sorted(set(prev_groups) | {grp}))
                rows[code] = (canonical_oews_level(groups), prev_title or title,
                              groups)
            else:
                rows[code] = (canonical_oews_level((grp,)), title, (grp,))
        wb.close()
        universe[year] = rows
        lv = Counter(lvl for lvl, _t, _g in rows.values())
        dups = sorted(c for c, v in rows.items() if len(v[2]) > 1)
        print(f"  OEWS {year}: {n_rows} rows -> {len(rows)} codes  "
              + " ".join(f"{k}={v}" for k, v in sorted(lv.items()))
              + f"  | {len(dups)} codes published twice ({n_rows - len(rows)} "
                f"extra rows): {dups}")
    return universe


# ---------------------------------------------------------------------------
# 2b. The official 2010 SOC -> 2018 SOC crosswalk
#
# May 2018 OEWS is coded to the 2010 SOC; May 2019 onward to the 2018 SOC.  BLS
# DOES publish a machine-readable bridge at
# https://www.bls.gov/soc/2018/soc_2010_to_2018_crosswalk.xlsx (900 detailed
# code pairs, 148 of them with a different 2018 number), plus the list of 2010
# codes deleted in 2018.  It is used here ONLY to LABEL series as comparable or
# not across the 2018<->2019 break - no number is ever bridged or restated.
# ---------------------------------------------------------------------------
def build_soc_vintage():
    xw_path = fetch_bls(SOC_XWALK_URL, RAW_GEO / "soc_2010_to_2018_crosswalk.xlsx",
                        referer=SOC_REFERER)
    del_path = fetch_bls(SOC_DELETED_URL,
                         RAW_GEO / "soc_2010_codes_deleted_in_2018.xlsx",
                         referer=SOC_REFERER)

    def sheet_rows(path):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        return rows

    def header_index(rows, *wanted):
        for i, r in enumerate(rows):
            cells = [str(c).strip().lower() if c else "" for c in r]
            if all(w in cells for w in wanted):
                return i, cells
        raise SystemExit(f"could not find a header row containing {wanted}")

    rows = sheet_rows(xw_path)
    hdr_i, hdr = header_index(rows, "2010 soc code", "2018 soc code")
    c10 = hdr.index("2010 soc code")
    t10c = hdr.index("2010 soc title")
    c18 = hdr.index("2018 soc code")
    t18c = hdr.index("2018 soc title")
    to18, from10 = defaultdict(set), defaultdict(set)
    title10, title18 = {}, {}
    n_pairs = 0
    for r in rows[hdr_i + 1:]:
        if r is None:
            continue
        a = str(r[c10]).strip() if r[c10] is not None else ""
        b = str(r[c18]).strip() if r[c18] is not None else ""
        if not (CODE_RE.match(a) and CODE_RE.match(b)):
            continue
        n_pairs += 1
        to18[a].add(b)
        from10[b].add(a)
        title10[a] = str(r[t10c]).strip() if r[t10c] is not None else ""
        title18[b] = str(r[t18c]).strip() if r[t18c] is not None else ""

    rows = sheet_rows(del_path)
    hdr_i, hdr = header_index(rows, "2010 soc code", "2010 soc group")
    dc, dg = hdr.index("2010 soc code"), hdr.index("2010 soc group")
    dt = dc + 1
    deleted = {}
    for r in rows[hdr_i + 1:]:
        if r is None or r[dc] is None:
            continue
        c = str(r[dc]).strip()
        if CODE_RE.match(c):
            deleted[c] = {"level": str(r[dg]).strip().lower(),
                          "title": str(r[dt]).strip() if r[dt] is not None else ""}

    changed = sorted(c for c, v in to18.items() if v != {c})
    absorbing = sorted(c for c, v in from10.items() if len(v) > 1)
    splitting = sorted(c for c, v in to18.items() if len(v) > 1)
    changed_pairs = sum(1 for a, v in to18.items() for b in v if a != b)
    print(f"  soc_2010_to_2018_crosswalk.xlsx: {n_pairs} code pairs")
    print(f"    distinct 2010 detailed codes    : {len(to18)}")
    print(f"    distinct 2018 detailed codes    : {len(from10)}")
    print(f"    pairs whose 2018 number differs : {changed_pairs}")
    print(f"    2010 codes whose content moved  : {len(changed)}")
    print(f"    2010 codes SPLIT into >1 2018   : {len(splitting)}")
    print(f"    2018 codes fed by >1 2010 code  : {len(absorbing)}")
    print(f"  soc_2010_codes_deleted_in_2018.xlsx: {len(deleted)} deleted 2010 codes "
          + " ".join(f"{k}={sum(1 for v in deleted.values() if v['level'] == k)}"
                     for k in LEVELS))
    return {"to18": {k: sorted(v) for k, v in to18.items()},
            "from10": {k: sorted(v) for k, v in from10.items()},
            "title10": title10, "title18": title18,
            "n_pairs": n_pairs, "n_changed_pairs": changed_pairs,
            "deleted": deleted}


# ---------------------------------------------------------------------------
# 3. OOH -> SOC, from the BLS projections table on each OOH page
# ---------------------------------------------------------------------------
def parse_ooh_page(slug):
    """Return (root_code_or_None, [sub1 codes], [(indent, code, title)])."""
    path = HTML_DIR / f"{slug}.html"
    if not path.exists():
        return None, [], [], "no scraped html page"
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    table = soup.find("table", id="outlook-table")
    if table is None or table.find("tbody") is None:
        return None, [], [], "no table#outlook-table on the page"
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        th = tr.find("th")
        if th is None:
            continue
        p = th.find("p")
        cls = " ".join(p.get("class") or []) if p is not None else ""
        indent = 2 if "sub2" in cls else (1 if "sub1" in cls else 0)
        code = None
        for td in tr.find_all("td"):
            if (td.get("data-label") or "").strip().lower().startswith("soc code"):
                v = td.get_text().strip()
                code = v if CODE_RE.match(v) else None
        rows.append((indent, code, th.get_text().strip()))
    if not rows:
        return None, [], [], "projections table has no data rows"
    root = rows[0]
    if root[0] != 0:
        return None, [], rows, "projections table does not start at indent 0"
    if root[1]:
        return root[1], [root[1]], rows, None
    subs = [c for ind, c, _t in rows[1:] if ind == 1 and c]
    if not subs:
        return None, [], rows, "root row has no SOC code and no sub-rows carry one"
    return None, subs, rows, None


# ---------------------------------------------------------------------------
# 4. Curated IT occupation list
# ---------------------------------------------------------------------------
# Core = every DETAILED occupation under SOC minor group 15-1200 "Computer
# Occupations".  That is the profession as BLS defines it: analysts, security,
# research scientists, support, network/database administration and architecture,
# programmers, software developers, QA, web and digital interface design, plus
# 15-1299 "Computer Occupations, All Other".
IT_CORE_MINOR = "15-1200"
# Adjacent = jobs a person browsing "IT jobs" applies to, that SOC files elsewhere
# purely because of how the taxonomy is organised.  Each one is justified; the
# frontend can show core only if it wants a strict definition.
IT_ADJACENT = {
    "11-3021": "Computer and Information Systems Managers - the management rung of "
               "the same career ladder; SOC files it under Management (11-0000) but "
               "it is an IT job and the usual promotion target for 15-12xx staff.",
    "15-2051": "Data Scientists - SOC files it under Mathematical Science "
               "Occupations (15-2000), but it competes for the same candidates as "
               "software and ML engineering. New in SOC 2018; OEWS first published "
               "it in May 2021, so its series starts in 2021, not 2018.",
    "17-2061": "Computer Hardware Engineers - SOC files it under Architecture and "
               "Engineering (17-0000); it is the SOC home of chip, board and "
               "firmware work and belongs in any 'which IT job' comparison.",
}
# Deliberately EXCLUDED, recorded so the boundary is auditable rather than implied.
IT_EXCLUDED = {
    "15-2031": "Operations Research Analysts - quantitative, but an analytics "
               "function rather than an IT role; hiring pipelines barely overlap.",
    "15-2041": "Statisticians - same reason; mostly government, pharma and survey "
               "research rather than technology employers.",
    "27-1024": "Graphic Designers - the digital side of design is already covered "
               "by 15-1255 Web and Digital Interface Designers.",
    "17-2071": "Electrical Engineers - hardware-adjacent but not a computing "
               "occupation; 17-2061 already covers computer hardware.",
    "13-1111": "Management Analysts - includes some IT consulting but is dominated "
               "by non-technical management consulting.",
    "43-9011": "Computer Operators - a real IT-ops job, but the code exists only "
               "in the 2010 SOC (it appears in May 2018 OEWS and nowhere after). "
               "SOC 2018 deleted it, so it cannot carry a 2018-2024 series.",
}


def main():
    print("=" * 78)
    print("1. SOC 2018 structure")
    print("=" * 78)
    struct, soc_by_code = build_soc_structure()
    children = defaultdict(list)
    for c in struct["codes"]:
        if c["parent"]:
            children[c["parent"]].append(c["code"])

    def descendants(code, level="detailed"):
        """All descendants of `code` at `level`, in code order."""
        out, stack = [], [code]
        while stack:
            cur = stack.pop()
            for ch in children.get(cur, ()):
                if soc_by_code[ch]["level"] == level:
                    out.append(ch)
                else:
                    stack.append(ch)
        return sorted(out)

    write_json(struct, DATA / "soc_structure.json")

    print()
    print("=" * 78)
    print("2. OEWS occupation universe")
    print("=" * 78)
    universe = build_oews_universe()
    print()
    print("=" * 78)
    print("2b. SOC 2010 -> SOC 2018 crosswalk (the May-2018 vintage break)")
    print("=" * 78)
    soc_vintage = build_soc_vintage()
    close_bls_browser()
    xw_to18 = {k: set(v) for k, v in soc_vintage["to18"].items()}
    xw_from10 = {k: set(v) for k, v in soc_vintage["from10"].items()}
    codes_2010 = set(xw_to18)

    all_oews_codes = sorted({c for y in universe.values() for c in y})
    oews_only = [c for c in all_oews_codes if c not in soc_by_code]
    ever_in_oews = {k for y in universe.values() for k in y}
    soc_never = [c["code"] for c in struct["codes"]
                 if c["level"] == "detailed" and c["code"] not in ever_in_oews]

    # --- two DIFFERENT phenomena, kept apart --------------------------------
    # (a) same-year duplication: OEWS prints the code twice in one file, tagged
    #     broad and detailed, with identical estimates.  Sum one row, not both.
    duplicate_codes = {}
    for c in all_oews_codes:
        if not any(len(universe[y][c][2]) > 1 for y in YEARS if c in universe[y]):
            continue
        by_grp = defaultdict(list)
        for y in YEARS:
            if c in universe[y]:
                for g in universe[y][c][2]:
                    by_grp[g].append(y)
        duplicate_codes[c] = {g: yrs for g, yrs in sorted(by_grp.items())}
    # (b) genuine cross-year drift: the canonical level of a code differs
    #     BETWEEN years.  Computed after (a) is collapsed, so a code that is
    #     merely printed twice never shows up here.
    level_drift = {}
    for c in all_oews_codes:
        seq = {y: universe[y][c][0] for y in YEARS if c in universe[y]}
        if len(set(seq.values())) > 1:
            level_drift[c] = {"levels": sorted(set(seq.values())),
                              "by_year": {str(y): g for y, g in sorted(seq.items())}}
    print(f"  distinct OEWS codes 2018-2024   : {len(all_oews_codes)}")
    print(f"  codes OEWS prints TWICE in the same year (broad + detailed rows, "
          f"identical estimates): {len(duplicate_codes)}")
    for c, grps in sorted(duplicate_codes.items()):
        both = sorted(set(grps.get("broad", [])) & set(grps.get("detailed", [])))
        t = next(universe[y][c][1] for y in reversed(YEARS) if c in universe[y])
        print(f"    - {c} {t[:46]:<46} duplicated in {both}")
    print(f"  codes whose level genuinely CHANGES between years: {len(level_drift)} "
          f"{dict(list(level_drift.items())[:3])}")
    print(f"  OEWS codes NOT in SOC 2018      : {len(oews_only)}  "
          f"(2010-SOC vintage in May 2018 + OEWS hybrid codes in 2019/2020)")
    print(f"  SOC 2018 detailed never in OEWS : {len(soc_never)}")
    print(f"    e.g. {soc_never[:8]}")

    # --- per-code comparability across the 2018 <-> 2019 vintage break -------
    def group_prefix(code):
        """SOC group codes are zero-padded, so membership is a prefix test.

        Only ever applied to the 2010 side (we have no 2010 structure file, just
        the crosswalk's 2010 code list). The 2018 side always uses the real tree.
        Both halves of that are VERIFIED below rather than assumed.
        """
        return code.rstrip("0")

    # (i) 2018 side: where does the prefix rule disagree with the real tree?
    det18 = {c["code"] for c in struct["codes"] if c["level"] == "detailed"}
    tree_vs_prefix = []
    for c in struct["codes"]:
        if c["level"] == "detailed":
            continue
        pre = group_prefix(c["code"])
        if {d for d in det18 if d.startswith(pre)} != set(descendants(c["code"], "detailed")):
            tree_vs_prefix.append(c["code"])
    print(f"  SOC 2018 groups where the prefix rule != the real tree: "
          f"{len(tree_vs_prefix)} {tree_vs_prefix} "
          f"(handled: the 2018 side of classify_vintage uses the tree)")

    # (ii) 2010 side: rebuild the May-2018 OEWS hierarchy from its own row order
    #      (the file is printed major -> minor -> broad -> detailed) and check
    #      every detailed code sits under a parent it prefix-matches.
    run, parent2010 = {}, {}
    for code, (lvl, _t, _g) in universe[YEARS[0]].items():
        if lvl == "major":
            run = {"major": code}
        elif lvl == "minor":
            run = {"major": run.get("major"), "minor": code}
        elif lvl == "broad":
            run = {"major": run.get("major"), "minor": run.get("minor"), "broad": code}
        elif lvl == "detailed":
            parent2010[code] = run.get("broad") or run.get("minor")
    pref_bad = [(c, p) for c, p in parent2010.items()
                if p and not c.startswith(group_prefix(p))]
    print(f"  May-{YEARS[0]} OEWS (2010 SOC): {len(parent2010)} detailed codes, "
          f"{len(pref_bad)} whose OEWS parent contradicts the prefix rule "
          f"{pref_bad[:5]}")
    assert not pref_bad, "the 2010 prefix rule does not hold in the May-2018 file"

    def classify_vintage(code):
        """Is a May-2018 OEWS row for `code` the same population as 2019+?"""
        lvl = soc_by_code.get(code, {}).get("level")
        if code in codes_2010 and lvl in (None, "detailed"):
            to, fr = xw_to18[code], xw_from10.get(code, set())
            if to == {code} and fr == {code}:
                return {"status": "comparable"}
            if len(to) > 1:
                return {"status": "split",
                        "detail": {"2010_code_became": sorted(to)},
                        "reason": f"2010 SOC {code} was split in 2018 into "
                                  f"{sorted(to)}; the May-2018 row therefore also "
                                  f"contains what 2019+ reports under "
                                  f"{sorted(to - {code})}"}
            if len(fr) > 1:
                return {"status": "absorbed",
                        "detail": {"2018_code_fed_by": sorted(fr)},
                        "reason": f"2018 SOC {code} absorbs 2010 codes "
                                  f"{sorted(fr)}; the May-2018 row covers only "
                                  f"part of what 2019+ reports"}
            other = sorted(to)[0]
            return {"status": "renumbered",
                    "detail": {"2010_code_became": [other]},
                    "reason": f"2010 SOC {code} was renumbered to {other} in 2018"}
        if lvl in ("broad", "minor", "major"):
            s10 = {x for x in codes_2010 if x.startswith(group_prefix(code))}
            s18 = set(descendants(code, "detailed"))
            if not s10:
                return {"status": "no_2010_counterpart",
                        "reason": f"no 2010 SOC detailed code sits under {code}"}
            mapped = set().union(*(xw_to18[x] for x in s10))
            if mapped == s18:
                return {"status": "comparable"}
            gained, lost = sorted(s18 - mapped), sorted(mapped - s18)
            why = [f"the 2010 and 2018 definitions of group {code} cover "
                   f"different detailed occupations"]
            if gained:
                why.append(f"2019+ additionally contains {gained}, fed from "
                           f"outside the group in 2010")
            if lost:
                why.append(f"May 2018 additionally contains what 2019+ reports "
                           f"under {lost}")
            return {"status": "group_membership_changed",
                    "detail": {"in_2019_plus_only": gained,
                               "in_may_2018_only": lost,
                               "members_2010": sorted(s10),
                               "members_2018": sorted(s18)},
                    "reason": "; ".join(why)}
        return {"status": "oews_combined_code",
                "reason": f"{code} is an OEWS publication code, not a SOC code in "
                          f"either vintage; its SOC content is defined in the OEWS "
                          f"technical notes, and OEWS keeps the same number and "
                          f"title for it in every year it publishes"}

    print()
    print("=" * 78)
    print("3. OOH -> SOC -> OEWS")
    print("=" * 78)
    ooh_rows = list(csv.DictReader(OCC_CSV.open(encoding="utf-8")))
    print(f"  occupations.csv rows in: {len(ooh_rows)}")

    entries = []
    unresolved = []
    stats = Counter()
    csv_code_conflicts = []
    codes_not_in_soc = []

    for r in ooh_rows:
        slug = r["slug"].strip()
        title = r["title"].strip()
        category = r["category"].strip()
        csv_code = r["soc_code"].strip()
        root, subs, _rows, err = parse_ooh_page(slug)

        if root:
            comps, method = [root], "ooh_projections_table_root"
        elif subs:
            comps, method = list(dict.fromkeys(subs)), "ooh_projections_table_components"
        elif csv_code:
            comps, method = [csv_code], "occupations_csv_soc_code"
        else:
            unresolved.append({
                "slug": slug, "title": title, "category": category,
                "reason": err or "no SOC code in occupations.csv and none in the "
                                 "OOH projections table",
            })
            stats["unresolved"] += 1
            continue

        if csv_code and csv_code not in comps:
            csv_code_conflicts.append((slug, csv_code, comps))
        bad = [c for c in comps if c not in soc_by_code]
        if bad:
            codes_not_in_soc.append((slug, bad))

        levels = [soc_by_code[c]["level"] if c in soc_by_code else None for c in comps]
        # Which codes to pull out of OEWS, per year, without double counting:
        # use the component itself when OEWS publishes it, otherwise its detailed
        # descendants that OEWS publishes that year.
        oews_by_year = {}
        gaps = {}
        partial = {}
        needed_children = False
        for year in YEARS:
            uni = universe[year]
            use, missing, part = [], [], {}
            for c in comps:
                if c in uni:
                    use.append(c)
                    continue
                all_kids = descendants(c, "detailed")
                kids = [k for k in all_kids if k in uni]
                if kids:
                    use.extend(kids)
                    needed_children = True
                    absent = [k for k in all_kids if k not in uni]
                    if absent:
                        # the sum for this year is INCOMPLETE - say so loudly
                        # rather than letting a short sum look like a real drop
                        part[c] = absent
                else:
                    missing.append(c)
            oews_by_year[str(year)] = sorted(dict.fromkeys(use))
            if missing:
                gaps[str(year)] = missing
            if part:
                partial[str(year)] = part
        years_ok = [y for y in YEARS if oews_by_year[str(y)]
                    and str(y) not in gaps and str(y) not in partial]

        # Codes OEWS prints twice in that year.  The frontend must take ONE row
        # per code (the two carry identical estimates), not both.
        dup_rows = {}
        for year in YEARS:
            ds = [c for c in oews_by_year[str(year)]
                  if len(universe[year][c][2]) > 1]
            if ds:
                dup_rows[str(year)] = ds

        # Comparability of the May-2018 (2010-SOC) point against 2019+.
        vint = {c: classify_vintage(c) for c in oews_by_year["2018"]}
        vint = {c: v for c, v in vint.items() if v["status"] != "comparable"}
        breaks = sorted(c for c, v in vint.items()
                        if v["status"] in BREAKING_VINTAGE_STATUSES)

        if len(comps) == 1 and levels[0] == "detailed" and not needed_children:
            kind = "direct_detailed"
        elif len(comps) == 1 and not needed_children:
            kind = "direct_aggregate"     # OEWS publishes this broad/minor row itself
        elif needed_children:
            kind = "needs_child_summing"
        else:
            kind = "multi_code"
        stats[kind] += 1
        stats["resolved"] += 1

        entries.append({
            "slug": slug,
            "title": title,
            "category": category,
            "csv_soc_code": csv_code or None,
            "soc_codes": comps,
            "soc_levels": levels,
            "method": method,
            "kind": kind,
            "children_detailed": {c: descendants(c, "detailed") for c in comps
                                  if c in soc_by_code
                                  and soc_by_code[c]["level"] != "detailed"},
            "oews": oews_by_year,
            "oews_gaps": gaps,
            "oews_partial": partial,
            "oews_years_complete": years_ok,
            "oews_duplicate_rows": dup_rows,
            "soc_vintage_2018": vint,
            "soc_vintage_break_2018": breaks,
        })

    print(f"  resolved            : {stats['resolved']}/{len(ooh_rows)}")
    print(f"    direct_detailed   : {stats['direct_detailed']}  "
          f"(1 SOC code, detailed, published by OEWS as-is)")
    print(f"    direct_aggregate  : {stats['direct_aggregate']}  "
          f"(1 broad/minor code that OEWS publishes as its own row)")
    print(f"    multi_code        : {stats['multi_code']}  "
          f"(OOH umbrella page = sum of several OEWS rows)")
    print(f"    needs_child_summing: {stats['needs_child_summing']}  "
          f"(at least one code absent from OEWS in some year; expanded to its "
          f"detailed children)")
    print(f"  unresolved          : {stats['unresolved']}")
    for u in unresolved:
        print(f"    - {u['slug']}: {u['reason']}")
    print(f"  occupations.csv soc_code missing from the page's own table: "
          f"{len(csv_code_conflicts)} {csv_code_conflicts[:3]}")
    print(f"  OOH codes absent from the SOC 2018 structure: {len(codes_not_in_soc)} "
          f"{codes_not_in_soc[:5]}")

    # No emitted sum may contain both a code and one of its own ancestors.
    def ancestors(code):
        out, cur = [], soc_by_code.get(code, {}).get("parent")
        while cur:
            out.append(cur)
            cur = soc_by_code.get(cur, {}).get("parent")
        return out

    overlaps = []
    for e in entries:
        for y, use in e["oews"].items():
            su = set(use)
            for c in use:
                if su & set(ancestors(c)):
                    overlaps.append((e["slug"], y, c))
    print(f"  double-counting check (a code plus its own ancestor in one sum): "
          f"{len(overlaps)} {overlaps[:5]}")
    assert not overlaps, "an OEWS sum contains both a parent and its child"

    childsum = [e for e in entries if e["kind"] == "needs_child_summing"]
    print(f"  occupations needing child summing in at least one year: {len(childsum)}")
    for e in childsum:
        yrs = sorted(y for y, v in e["oews"].items() if set(v) != set(e["soc_codes"]))
        print(f"    - {e['slug']} {e['soc_codes']} expanded in {yrs}")
    n_part = [e for e in entries if e["oews_partial"]]
    print(f"  occupations whose expansion is INCOMPLETE in some year "
          f"(sum understates): {len(n_part)}")
    for e in n_part:
        for y, part in sorted(e["oews_partial"].items()):
            for c, absent in part.items():
                print(f"    - {e['slug']} {y}: {c} missing children {absent}")

    # --- OOH codes that OEWS never publishes, at any level, in any year -------
    never = []
    for e in entries:
        dead = [c for c in e["soc_codes"]
                if not any(c in universe[y] for y in YEARS)
                and not any(k in universe[y] for y in YEARS
                            for k in descendants(c, "detailed"))]
        if dead:
            never.append((e["slug"], dead))
    print(f"  OOH SOC codes OEWS never publishes: {len(never)}")
    for slug, dead in never:
        for c in dead:
            t = soc_by_code.get(c, {}).get("title", "?")
            print(f"    - {slug}: {c} {t}")

    # per-year OEWS coverage of the OOH set
    print("  OOH occupations joinable to OEWS, by year "
          "(complete / partial sum / no data):")
    for y in YEARS:
        ys = str(y)
        full = sum(1 for e in entries if y in e["oews_years_complete"])
        part = sum(1 for e in entries
                   if e["oews"][ys] and y not in e["oews_years_complete"])
        none = sum(1 for e in entries if not e["oews"][ys])
        print(f"    {y}: {full:>3} complete, {part:>3} partial, {none:>3} none "
              f"(of {len(entries)})")

    soc_to_ooh_pre = defaultdict(list)
    for e in entries:
        for c in e["soc_codes"]:
            soc_to_ooh_pre[c].append(e["slug"])

    # --- OEWS rows printed twice, as they land in the emitted sums -----------
    dup_used = defaultdict(set)
    for e in entries:
        for y, cs in e["oews_duplicate_rows"].items():
            for c in cs:
                dup_used[c].add(e["slug"])
    n_dup_occ = sum(1 for e in entries if e["oews_duplicate_rows"])
    print(f"  OOH occupations whose sum includes a code OEWS prints twice: "
          f"{n_dup_occ}  (take ONE row per code - the two are identical)")
    for c, slugs in sorted(dup_used.items()):
        print(f"    - {c} used by {sorted(slugs)}")

    # --- May-2018 vintage break ---------------------------------------------
    codes_2018_sums = sorted({c for e in entries for c in e["oews"]["2018"]})
    vintage_by_code = {c: classify_vintage(c) for c in codes_2018_sums}
    v_counts = Counter(v["status"] for v in vintage_by_code.values())
    print(f"  codes used in a May-2018 sum: {len(codes_2018_sums)} -> "
          + ", ".join(f"{k}={v}" for k, v in sorted(v_counts.items())))
    for c, v in sorted(vintage_by_code.items()):
        if v["status"] == "comparable":
            continue
        users = sorted({e["slug"] for e in entries if c in e["oews"]["2018"]})
        print(f"    - {c} [{v['status']}] "
              f"{soc_by_code.get(c, {}).get('title', '(not a SOC 2018 code)')}")
        print(f"        {v['reason']}")
        print(f"        used by {users}")
    broken_occ = [e for e in entries if e["soc_vintage_break_2018"]]
    print(f"  OOH occupations whose May-2018 point is NOT comparable with 2019+: "
          f"{len(broken_occ)}")
    for e in broken_occ:
        print(f"    - {e['slug']} {e['soc_vintage_break_2018']}")

    # --- OEWS title changes across the span, for codes used in 2018 ---------
    title_changes = []
    for c in codes_2018_sums:
        if c not in universe[YEARS[0]]:
            continue
        last = next(y for y in reversed(YEARS) if c in universe[y])
        if last == YEARS[0]:
            continue
        t0, t1 = universe[YEARS[0]][c][1], universe[last][c][1]
        if t0 != t1:
            title_changes.append({"code": c, "title_2018": t0,
                                  "title_latest": t1, "latest_year": last,
                                  "used_by": sorted(
                                      {e["slug"] for e in entries
                                       if c in e["oews"]["2018"]})})
    print(f"  codes used in a May-2018 sum whose OEWS TITLE changed by "
          f"{YEARS[-1]}: {len(title_changes)}  (a renamed code is still the same "
          f"code unless the crosswalk says otherwise)")
    for t in title_changes[:6]:
        print(f"    - {t['code']} {t['title_2018']!r} -> {t['title_latest']!r}")
    print(f"    ... {max(0, len(title_changes) - 6)} more in "
          f"soc_vintage.oews_title_changes")

    # --- cross-check: no 2010 code deleted in 2018 leaks into a 2018 sum -----
    deleted_used = sorted(c for c in codes_2018_sums
                          if c in soc_vintage["deleted"])
    print(f"  2010 codes DELETED in SOC 2018 that appear in a May-2018 sum: "
          f"{len(deleted_used)} {deleted_used}")

    # --- explicitly requested report: no soc_code / soc_code not in OEWS -----
    no_csv_code = [e for e in entries if not e["csv_soc_code"]]
    print(f"  OOH occupations with NO soc_code in occupations.csv: {len(no_csv_code)}"
          f" (+{stats['unresolved']} unresolved) - all resolved from the OOH "
          f"projections table:")
    for e in sorted(no_csv_code, key=lambda x: x["slug"]):
        print(f"    - {e['slug']:<62} -> {len(e['soc_codes']):>2} code(s) "
              f"{e['soc_codes'][:4]}{'...' if len(e['soc_codes']) > 4 else ''}")

    code_never = sorted({c for e in entries for c in e["soc_codes"]
                         if c not in ever_in_oews})
    print(f"  OOH SOC codes that appear in NO OEWS year: {len(code_never)}")
    for c in code_never:
        info = soc_by_code.get(c)
        print(f"    - {c} {info['title'] if info else '(not in SOC 2018)'} "
              f"[{info['level'] if info else '?'}] used by "
              f"{sorted(soc_to_ooh_pre.get(c, []))}")

    aggregated = [e for e in entries if e["csv_soc_code"]
                  and soc_by_code.get(e["csv_soc_code"], {}).get("level")
                  not in (None, "detailed")]
    print(f"  OOH occupations whose occupations.csv soc_code is an AGGREGATE "
          f"(broad/minor) code: {len(aggregated)}")
    agg_not_published = [e for e in aggregated
                         if e["csv_soc_code"] not in universe[YEARS[-1]]]
    print(f"    of those, not published as its own OEWS {YEARS[-1]} row "
          f"(frontend must sum children): {len(agg_not_published)} "
          f"{[e['csv_soc_code'] for e in agg_not_published]}")

    ooh_not_soc = sorted({c for e in entries for c in e["soc_codes"]
                          if c not in soc_by_code})
    print(f"  OOH SOC codes absent from SOC 2018 (BLS Employment Projections / "
          f"OEWS hybrid codes): {len(ooh_not_soc)}")
    for c in ooh_not_soc:
        yrs = [y for y in YEARS if c in universe[y]]
        t = next((universe[y][c][1] for y in yrs), "?")
        print(f"    - {c} {t} | OEWS years {yrs}")

    # --- category -> SOC major groups ---------------------------------------
    cat_groups = defaultdict(Counter)
    cat_titles = {}
    for e in entries:
        cat_titles.setdefault(e["category"], e["category"].replace("-", " ").title())
        for c in e["soc_codes"]:
            cat_groups[e["category"]][c[:2]] += 1
    for r in ooh_rows:
        cat_titles.setdefault(r["category"].strip(),
                              r["category"].strip().replace("-", " ").title())
    category_major_groups = {}
    for cat in sorted(cat_titles):
        cnt = cat_groups.get(cat, Counter())
        groups = [{"major": g,
                   "code": struct["major_group"].get(g, {}).get("code"),
                   "title": struct["major_group"].get(g, {}).get("title"),
                   "n_ooh_soc_codes": n}
                  for g, n in cnt.most_common()]
        category_major_groups[cat] = {
            "primary": groups[0]["major"] if groups else None,
            "groups": groups,
        }
    print(f"  category -> major group: {len(category_major_groups)} categories")
    multi = [c for c, v in category_major_groups.items() if len(v["groups"]) > 1]
    print(f"    categories spanning >1 major group: {len(multi)}")
    for c in multi:
        gs = category_major_groups[c]["groups"]
        print("      " + c + ": " + ", ".join(f"{g['major']}x{g['n_ooh_soc_codes']}" for g in gs))

    # --- curated IT list -----------------------------------------------------
    soc_to_ooh = soc_to_ooh_pre
    # also credit an OOH page for the detailed codes it covers via child summing
    covers = defaultdict(set)
    for e in entries:
        for c in e["soc_codes"]:
            covers[c].add(e["slug"])
            for k in descendants(c, "detailed"):
                covers[k].add(e["slug"])

    it = []
    core = descendants(IT_CORE_MINOR, "detailed")
    for code in core:
        it.append({"code": code, "title": soc_by_code[code]["title"],
                   "tier": "core", "parent": soc_by_code[code]["parent"],
                   "reason": f"detailed occupation under SOC minor group "
                             f"{IT_CORE_MINOR} (Computer Occupations)"})
    for code, why in IT_ADJACENT.items():
        if code not in soc_by_code:
            raise SystemExit(f"IT_ADJACENT code {code} is not in SOC 2018")
        it.append({"code": code, "title": soc_by_code[code]["title"],
                   "tier": "adjacent", "parent": soc_by_code[code]["parent"],
                   "reason": why})
    for row in it:
        row["oews_years"] = [y for y in YEARS if row["code"] in universe[y]]
        row["ooh_slugs"] = sorted(covers.get(row["code"], ()))
    # OEWS codes that are not SOC 2018 at all but sit in the computer families -
    # reported, never silently mapped onto the split codes they replaced.
    hybrid = []
    for code in oews_only:
        yrs = [y for y in YEARS if code in universe[y]]
        title = next(universe[y][code][1] for y in yrs)
        grp = next(universe[y][code][0] for y in yrs)
        hybrid.append({"code": code, "title": title, "oews_level": grp,
                       "oews_years": yrs})
    it_hybrid = [h for h in hybrid if h["code"].startswith("15-1")
                 and min(h["oews_years"]) >= 2019]
    print(f"  IT occupations: {len(core)} core (all detailed in {IT_CORE_MINOR}) + "
          f"{len(IT_ADJACENT)} adjacent, {len(IT_EXCLUDED)} explicitly excluded")
    gapped = [r["code"] for r in it if len(r["oews_years"]) < len(YEARS)]
    print(f"    IT codes without a full 2018-2024 OEWS series: {len(gapped)} {gapped}")
    print(f"    OEWS-only hybrid computer codes (2019/2020): "
          f"{[h['code'] for h in it_hybrid]}")

    notes = [
        "OOH->SOC comes from BLS's own Employment Projections table on each OOH "
        "page (table#outlook-table). Where that table's root row carries a SOC "
        "code the OOH page IS that code; where it carries an em dash the page is "
        "the sum of the sub1 rows. sub2 rows are children of sub1 rows and are "
        "never added, to avoid double counting.",
        "OEWS publishes a handful of codes TWICE in the same national file - one "
        "row tagged o_group=broad and one tagged o_group=detailed, carrying "
        "byte-identical estimates. They are broad SOC groups whose detailed "
        "members OEWS does not break out. When you join oews[year] against the "
        "OEWS file, take exactly ONE row per code (either label; they are equal) "
        "- summing every matching row doubles those occupations. The full list is "
        "oews_universe.duplicate_codes, and each occupation carries the ones that "
        "land in its own sums as oews_duplicate_rows[year]. This is a same-year "
        "publication artefact, NOT a level change between years; genuine "
        "cross-year drift is in oews_universe.level_drift, which is empty.",
        "May 2018 OEWS is coded to the 2010 SOC; May 2019 onward uses the 2018 "
        "SOC. BLS DOES publish a machine-readable bridge - "
        "soc_2010_to_2018_crosswalk.xlsx (900 detailed code pairs, 148 with a "
        "different 2018 number) and soc_2010_codes_deleted_in_2018.xlsx, both at "
        "bls.gov/soc/2018/ - and this build reads both. It uses them ONLY to "
        "LABEL series, never to bridge or restate a number: every code used in a "
        "May-2018 sum is classified in soc_vintage.codes_used_in_may_2018, and "
        "any that is not comparable with 2019+ is repeated on the occupation as "
        "soc_vintage_break_2018 (with the reason in soc_vintage_2018). Codes that "
        "were split or renumbered out of existence simply have no May-2018 OEWS "
        "row, which shows up as oews[\"2018\"] == [] plus oews_gaps.",
        "A changed OEWS occupation TITLE is not by itself a break in the series - "
        "39 codes used in May-2018 sums were merely renamed by 2024 (e.g. 43-5031 "
        "'Police, Fire, and Ambulance Dispatchers' -> 'Public Safety "
        "Telecommunicators'). They are listed in soc_vintage.oews_title_changes so "
        "a label rendered from the latest year is not mistaken for a different "
        "occupation; the crosswalk, not the title, decides comparability.",
        "May 2019 and May 2020 OEWS use hybrid codes that do not exist in SOC "
        "2018 - 15-1245 (database administrators and architects), 15-1256 "
        "(software developers + QA analysts and testers) and 15-1257 (web "
        "developers + digital interface designers) - in place of the split codes "
        "used from 2021. They are listed in oews_only_codes with their years. For "
        "a continuous 2019-2024 IT series, roll up to the broad group (15-1240, "
        "15-1250) which is published in every one of those years.",
        "FRONTEND CONTRACT: oews[year] is the exact, non-overlapping set of OEWS "
        "occupation CODES to sum for that OOH occupation in that year - one "
        "value per code, not one value per matching file row. Where "
        "oews_duplicate_rows[year] names a code, the OEWS file holds two rows "
        "for it with identical estimates; take either, never both. An empty "
        "oews[year] means OEWS publishes nothing for that occupation that year. "
        "oews_gaps[year] lists component codes that could not be resolved at all "
        "for that year, oews_partial[year] the ones whose child expansion is "
        "incomplete (so the sum understates), and soc_vintage_break_2018 the "
        "codes whose May-2018 value is not comparable with 2019+.",
        "OEWS covers wage and salary workers only, so occupations dominated by "
        "the self-employed (e.g. 45-3031 Fishing and Hunting Workers) exist in "
        "SOC but never appear in OEWS.",
    ]

    crosswalk = {
        "sources": {
            "soc_structure": SOC_STRUCTURE_URL,
            "soc_2010_to_2018_crosswalk": SOC_XWALK_URL,
            "soc_2010_codes_deleted_in_2018": SOC_DELETED_URL,
            "oews_national": [OEWS_NAT_URL.format(yy=f"{y % 100:02d}") for y in YEARS],
            "ooh": "occupations.csv + html/<slug>.html (BLS Occupational Outlook "
                   "Handbook, public domain)",
        },
        "years": YEARS,
        "notes": notes,
        "counts": {
            "ooh_total": len(ooh_rows),
            "ooh_resolved": stats["resolved"],
            "ooh_unresolved": stats["unresolved"],
            "direct_detailed": stats["direct_detailed"],
            "direct_aggregate": stats["direct_aggregate"],
            "multi_code": stats["multi_code"],
            "needs_child_summing": stats["needs_child_summing"],
            "ooh_with_csv_soc_code": sum(1 for r in ooh_rows if r["soc_code"].strip()),
        },
        "occupations": entries,
        "unresolved": unresolved,
        "soc_to_ooh": {k: sorted(v) for k, v in sorted(soc_to_ooh.items())},
        "category_major_groups": category_major_groups,
        "oews_universe": {
            "_schema": "parallel arrays, all indexed alike; present[i] is a bitmask "
                       "string over years[] where '1' = OEWS publishes that code "
                       "that year. title is taken from the most recent year the "
                       "code appears in (see soc_vintage.oews_title_changes for "
                       "codes renamed since 2018). level is that year's o_group; "
                       "where OEWS printed the code under two o_group labels in "
                       "the same year, level is the BROADER of the two and the "
                       "code is listed in duplicate_codes.",
            "years": YEARS,
            "code": all_oews_codes,
            "level": [next(universe[y][c][0] for y in reversed(YEARS) if c in universe[y])
                      for c in all_oews_codes],
            "title": [next(universe[y][c][1] for y in reversed(YEARS) if c in universe[y])
                      for c in all_oews_codes],
            "present": ["".join("1" if c in universe[y] else "0" for y in YEARS)
                        for c in all_oews_codes],
            "duplicate_codes": {
                "_schema": "code -> {o_group: [years OEWS published the code under "
                           "that o_group]}. A code is listed when at least one "
                           "year carries BOTH labels; those rows are duplicates of "
                           "each other with identical estimates, so join exactly "
                           "one of them.",
                "codes": duplicate_codes,
            },
            "level_drift": {
                "_schema": "code -> {levels, by_year}: the canonical o_group "
                           "GENUINELY differs between years. Same-year duplication "
                           "is not drift and lives in duplicate_codes.",
                "codes": level_drift,
            },
        },
        "soc_vintage": {
            "_schema": "the official 2010 SOC -> 2018 SOC bridge, used to LABEL "
                       "the May-2018 point only. No number is ever bridged.",
            "sources": {
                "crosswalk": SOC_XWALK_URL,
                "deleted_2010_codes": SOC_DELETED_URL,
            },
            "n_code_pairs": soc_vintage["n_pairs"],
            "n_pairs_with_a_different_2018_number": soc_vintage["n_changed_pairs"],
            "n_2010_codes": len(soc_vintage["to18"]),
            "n_2018_codes": len(soc_vintage["from10"]),
            "n_2010_codes_renumbered_or_split": len(
                [c for c, v in soc_vintage["to18"].items() if set(v) != {c}]),
            "n_2018_codes_fed_by_multiple_2010": len(
                [c for c, v in soc_vintage["from10"].items() if len(v) > 1]),
            "crosswalk_2010_to_2018": soc_vintage["to18"],
            "deleted_2010_codes": soc_vintage["deleted"],
            "deleted_2010_codes_used_in_may_2018_sums": deleted_used,
            "status_counts": dict(sorted(v_counts.items())),
            "codes_used_in_may_2018": [
                dict({"code": c,
                      "title": soc_by_code.get(c, {}).get("title"),
                      "soc_level": soc_by_code.get(c, {}).get("level"),
                      "used_by": sorted({e["slug"] for e in entries
                                         if c in e["oews"]["2018"]})},
                     **v)
                for c, v in sorted(vintage_by_code.items())
                if v["status"] != "comparable"],
            "n_codes_used_in_may_2018": len(codes_2018_sums),
            "oews_title_changes": title_changes,
        },
        "oews_only_codes": hybrid,
        "ooh_without_csv_soc_code": [
            {"slug": e["slug"], "title": e["title"], "category": e["category"],
             "resolved_to": e["soc_codes"], "method": e["method"]}
            for e in sorted(no_csv_code, key=lambda x: x["slug"])],
        "ooh_soc_codes_never_in_oews": [
            {"code": c,
             "title": soc_by_code[c]["title"] if c in soc_by_code else None,
             "soc_level": soc_by_code[c]["level"] if c in soc_by_code else None,
             "used_by": sorted(soc_to_ooh_pre.get(c, []))}
            for c in code_never],
        "ooh_soc_codes_not_in_soc2018": [
            {"code": c,
             "title": next((universe[y][c][1] for y in YEARS if c in universe[y]), None),
             "oews_years": [y for y in YEARS if c in universe[y]],
             "used_by": sorted(soc_to_ooh_pre.get(c, []))}
            for c in ooh_not_soc],
        "ooh_aggregate_csv_soc_codes": [
            {"slug": e["slug"], "code": e["csv_soc_code"],
             "soc_level": soc_by_code[e["csv_soc_code"]]["level"],
             "in_oews_latest": e["csv_soc_code"] in universe[YEARS[-1]],
             "children_detailed": e["children_detailed"].get(e["csv_soc_code"], [])}
            for e in aggregated],
        "soc_detailed_never_in_oews": [
            {"code": c, "title": soc_by_code[c]["title"]} for c in soc_never],
        "it_occupations": {
            "definition": "Every detailed SOC 2018 occupation in minor group "
                          "15-1200 (Computer Occupations), plus four adjacent "
                          "codes SOC files elsewhere. Exclusions are listed so "
                          "the boundary is auditable.",
            "core_minor_group": IT_CORE_MINOR,
            "occupations": it,
            "excluded": [{"code": c, "title": soc_by_code[c]["title"] if c in soc_by_code
                          else None, "reason": why} for c, why in IT_EXCLUDED.items()],
            "oews_hybrid_codes_2019_2020": it_hybrid,
        },
    }
    p = write_json(crosswalk, DATA / "soc_crosswalk.json")

    print()
    print("=" * 78)
    print("summary")
    print("=" * 78)
    print(f"  soc_structure.json : {len(struct['codes'])} codes, "
          f"{len(struct['major_group'])} major groups")
    print(f"  soc_crosswalk.json : {len(entries)} OOH occupations, "
          f"{len(unresolved)} unresolved, {len(all_oews_codes)} OEWS codes, "
          f"{len(it)} IT occupations")
    print(f"                       {len(duplicate_codes)} OEWS codes printed twice "
          f"in a year (sum ONE row each), {len(level_drift)} with genuine "
          f"cross-year level drift")
    print(f"                       {len(broken_occ)} occupations whose May-2018 "
          f"point is not comparable with 2019+, {len(title_changes)} renamed codes")
    print(f"  {p} {p.stat().st_size / 1024:,.0f} KB")


if __name__ == "__main__":
    try:
        main()
    finally:
        close_bls_browser()
