"""
Build the metro-area (MSA) BLS OEWS occupational time series, 2018-2024.

Output: data/oews_metro.json  (columnar / index-based, sized for the browser)
Raw:    raw/oews/oesm{YY}ma.zip               metro OEWS release, one per year
        raw/oews/nat/oesm{YY}nat.zip          national OEWS release (occ ranking + LQ base)
        raw/oews/2024_Gaz_cbsa_national.zip   CBSA internal points (lat/lon)
        raw/oews/2024_Gaz_place_national.zip  place internal points (NECTA fallback)

--------------------------------------------------------------------------------
WHAT COUNTS AS A METRO AREA
--------------------------------------------------------------------------------
Each oesm{YY}ma.zip holds two data workbooks:
    MSA_M{YYYY}_dl.xlsx  -> metropolitan areas
    BOS_M{YYYY}_dl.xlsx  -> "balance of state" nonmetropolitan areas
Only the MSA_* workbook is read; the BOS_* workbook is never opened.  For 2019-2024
the MSA workbook carries an AREA_TYPE column and every row is AREA_TYPE == 4 (the
BLS code for "metropolitan statistical area"); the loader still filters on it
explicitly and prints anything it drops.  The 2018 workbook predates AREA_TYPE, so
the rule there is "every row of the MSA workbook is a metro area"; the script
verifies that by checking the 2018 area-code set against the union of AREA_TYPE==4
codes seen in 2019-2024, and prints any code that is not covered.

In New England OEWS publishes NECTAs (New England City and Town Areas, CBSA codes
70000-79999) instead of the county-based MSAs.  Those are genuine OEWS metro areas
and are kept - they are what the Boston / Hartford / Providence rows are.

--------------------------------------------------------------------------------
EXPLICIT SIZE-BUDGET RULES  (target: data/oews_metro.json < ~9 MB)
--------------------------------------------------------------------------------
METROS kept = union of
  (M1) the TOP_METROS largest metro areas by total employment (occ 00-0000) in the
       MOST RECENT YEAR ALONE.  Only areas that actually have a most-recent-year
       00-0000 value compete here, so the cutoff this rule reports is a true
       latest-year cutoff rather than a mixed-vintage one.
  (M1b) area codes that stop being published before the most recent year - the OMB
       2023 re-delineation retired every New England NECTA plus Cleveland-Elyria,
       Dayton, California-Lexington Park and Poughkeepsie - are invisible to M1
       because they have no latest-year total at all.  Such an area is ranked on the
       most recent year in which it does have one and kept when that total clears
       the SAME M1 cutoff, so its 2018-2023 history is not silently lost.  Kept as a
       separate, separately counted rule precisely so it cannot quietly consume M1
       slots and displace areas that genuinely are in the latest-year top TOP_METROS.
  (M2) every metro area that is top-IT_TOP_N by employment for ANY detailed
       15-xxxx (Computer and Mathematical) occupation in ANY year 2018-2024.
       This is what keeps Provo, Boulder, Raleigh, Huntsville, etc.
  (M3) the crosswalk closure of M1|M1b|M2 - if a kept area is the renamed/recoded
       continuation of another area (see below), that other area is kept too, so a
       place never appears to start or stop existing mid-series.
OCCUPATIONS kept = union of
  (O1) 00-0000, All Occupations
  (O2) every major group XX-0000
  (O3) every 15-xxxx (Computer and Mathematical) code the metro files publish, in
       every SOC vintage - 2010 (15-1132 Software Developers, Applications), the
       2019-2020 combined codes (15-1256 Software Developers and Software Quality
       Assurance Analysts and Testers) and 2018 SOC (15-1252 Software Developers)
  (O4) for EACH year, the NAT_TOP_N largest detailed occupations, unioned over the
       seven years.  Per-year (not just latest-year) because SOC codes were
       renumbered twice in this window; ranking on 2024 alone leaves earlier years
       blank for every renumbered job.
Everything else is dropped.  The script prints exact kept/dropped counts.

The occupation menu is derived from the METRO workbooks, not from the national
workbook.  In May 2019 and May 2020 BLS published Software Developers (15-1252)
and Software QA (15-1253) as separate national estimates but only as the combined
15-1256 at metro level; a national-derived menu therefore drops every 2019-2020
metro software-developer estimate.  "Largest detailed occupations" is consequently
ranked by employment summed over ALL OEWS metro areas for that year (suppressed
cells skipped, never zero-filled).  That is a ranking input only - no summed value
is ever emitted.

--------------------------------------------------------------------------------
SOC VINTAGE WARNING (do not draw a line across the break blindly)
--------------------------------------------------------------------------------
The vintage boundaries in the METRO files are NOT the ones in the national release,
and the script measures them instead of assuming them (meta.soc_breaks, printed):

    2018        2010 SOC.  The only 2010-SOC year at metro level - 15-1132
                "Software Developers, Applications" and every other 2010
                computer/math code appears in 2018 and in no later year.
    2019-2020   already 2018 SOC, but with the aggregated codes BLS published
                while the sample rotated in: 15-1256 (Software Developers and
                Software Quality Assurance Analysts and Testers), 15-1245
                (Database and Network Administrators and Architects), 15-1257,
                15-2098.  The two years publish all but two codes in common.
    2021-2024   2018 SOC with those aggregates split apart: 15-1252 Software
                Developers, 15-1253 Software QA, 15-1242/15-1243, 15-2051.

So the metro breaks are 2018|2019 and 2020|2021.  There is no meaningful 2019|2020
break here and 2019 is NOT a 2010-SOC year - looking for 15-1132 in 2019 finds
nothing.  Renumbered occupations appear as TWO entries with disjoint year coverage
(15-1132 in 2018, 15-1256 in 2019-2020, 15-1252 in 2021-2024).  No crosswalk is
applied - codes are emitted as published, and every occupation record carries a
per-year presence vector "y".

--------------------------------------------------------------------------------
GEOGRAPHY BREAK 2023 -> 2024 (OMB 2023 delineations) AND THE DERIVED CROSSWALK
--------------------------------------------------------------------------------
The 2024 OEWS adopted the OMB 2023 delineations: every New England NECTA was
replaced by a county-based MSA with a new CBSA code (71650 Boston-Cambridge-Nashua
-> 14460 Boston-Cambridge-Newton, 77200 -> 39300 Providence, ...), and a handful of
other areas were renamed and recoded (17460 Cleveland-Elyria -> 17410 Cleveland,
19380 Dayton -> 19430 Dayton-Kettering-Beavercreek, 15680 California-Lexington Park
-> 30500 Lexington Park, 39100 Poughkeepsie-Newburgh-Middletown ->
28880 Kiryas Joel-Poughkeepsie-Newburgh).

These are NOT merged - the underlying county sets differ, so merging would fabricate
a continuous series.  Instead the script derives a crosswalk from the data: for each
consecutive year pair, an area that disappears is linked to an area that appears iff
they share at least one principal-city token AND at least one component state, and
the match is unique in both directions.  Every proposed link is printed.  The link
is emitted as metros[].x (a list of the other area codes for the same place) and the
crosswalk closure is added to the kept set, so both halves are always present.

--------------------------------------------------------------------------------
SUPPRESSION / CENSORING
--------------------------------------------------------------------------------
BLS markers are mapped to null, never to 0, and never summed:
    "*"  estimate not released           "**" data not available
    "#"  wage at or above the OEWS top code
    "~"  employment rounds to < 0.05 %   ""   blank
"#" is a censored *high* value rather than a missing one, so it is emitted as null
(no value is invented) but its position is recorded in series[].tc / national.tc as
[row, year_index, 0=median|1=mean].

THE TOP CODE IS NOT CONSTANT OVER 2018-2024, so tc cannot carry one global label.
BLS raised it from $100.00/hr ($208,000/yr) to $115.00/hr ($239,200/yr) with the
May 2022 release.  The script derives the threshold per year from the workbooks
rather than hard-coding it (derive_topcodes): OEWS censors the median and every
percentile at a round hourly figure, so the largest value it actually publishes
sits a few cents under that figure - $99.98/hr in 2018 rising to $114.97/hr in
2024 - and rounding the per-year maximum up to the next whole dollar recovers the
top code.  The result is emitted as meta.topcode.hourly / .annual, aligned to
years[], so a tc entry is read against topcode.annual[year_index].  Using a single
$208,000 label would misstate every 2022-2024 top-coded median by $31,200; those
years publish medians as high as $239,130 outright.

THE MEAN IS NOT CENSORED AT THE TOP CODE.  Published annual means run far above it
in every year (up to $291,360 in 2018 and $1,805,790 in 2023, against caps of
$208,000 and $239,200), so a "#" on a_mean is NOT the statement "mean >= top code".
It is only "not published, censored high".  In six of the seven years every mean
"#" sits in a row whose whole percentile distribution is "#" as well, but 2022 has
21 rows whose mean is "#" beside a published median of $211,610-$237,620.  So tc
entries with metric 1 (mean) are emitted as position flags with NO value bound, and
meta says so; only metric 0 (median) carries ">= topcode.annual[year_index]".
"""

import datetime
import io
import math
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.common import DATA, RAW, YEARS, fetch, write_json  # noqa: E402

RAW_OEWS = RAW / "oews"
RAW_NAT = RAW_OEWS / "nat"
OUT = DATA / "oews_metro.json"

# ---- tunable budget knobs (copied into meta so the output is self-describing) ----
TOP_METROS = 200      # rule M1
IT_TOP_N = 25         # rule M2
NAT_TOP_N = 150       # rule O4
BUDGET_MB = 9.0

# All CBSA gazetteer vintages are merged, newest first, so CBSA codes retired by
# the OMB 2023 delineations (15680, 17460, 19380, 39100, ...) still get a real
# internal point instead of falling back to a city centroid.
GAZ_CBSA_YEARS = (2024, 2023, 2021, 2020, 2019)
GAZ_PLACE_YEARS = (2024, 2023)

SUPPRESSION = {"*", "**", "#", "~"}
PLACE_SUFFIXES = (
    "city", "town", "village", "borough", "municipality", "CDP",
    "city and borough", "consolidated government", "metro government",
    "metropolitan government", "unified government", "urban county",
    "government", "corporation", "plantation", "township", "comunidad", "zona urbana",
)

MAJOR_RE = re.compile(r"^\d{2}-0000$")
IT_RE = re.compile(r"^15-")
STATE_TAIL = re.compile(r",\s*([A-Z]{2}(?:-[A-Z]{2})*)\s*$")
MARKERS = Counter()

# The wage columns OEWS censors at the top code.  The mean is deliberately NOT in
# here: it is not censored at the top code (see derive_topcodes), so including it
# would corrupt the threshold this script derives from the data.
PCT_HOURLY = ("h_pct10", "h_pct25", "h_median", "h_pct75", "h_pct90")
PCT_ANNUAL = ("a_pct10", "a_pct25", "a_median", "a_pct75", "a_pct90")
HOURS_PER_YEAR = 2080          # BLS converts hourly <-> annual at exactly this


# --------------------------------------------------------------------------- io
def download():
    """Fetch every upstream file (disk-cached by common.fetch)."""
    metro, nat = {}, {}
    for y in YEARS:
        yy = f"{y % 100:02d}"
        metro[y] = fetch(f"https://www.bls.gov/oes/special-requests/oesm{yy}ma.zip",
                         RAW_OEWS / f"oesm{yy}ma.zip", timeout=1800)
        nat[y] = fetch(f"https://www.bls.gov/oes/special-requests/oesm{yy}nat.zip",
                       RAW_NAT / f"oesm{yy}nat.zip", timeout=900)
    gaz_cbsa, gaz_place = [], None
    for gy in GAZ_CBSA_YEARS:
        try:
            gaz_cbsa.append((gy, fetch(
                "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
                f"{gy}_Gazetteer/{gy}_Gaz_cbsa_national.zip",
                RAW_OEWS / f"{gy}_Gaz_cbsa_national.zip", timeout=600)))
        except Exception as e:  # noqa: BLE001
            print(f"  gazetteer cbsa {gy} unavailable: {type(e).__name__}")
    for gy in GAZ_PLACE_YEARS:
        try:
            gaz_place = (gy, fetch(
                "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
                f"{gy}_Gazetteer/{gy}_Gaz_place_national.zip",
                RAW_OEWS / f"{gy}_Gaz_place_national.zip", timeout=600))
            break
        except Exception as e:  # noqa: BLE001
            print(f"  gazetteer place {gy} unavailable: {type(e).__name__}")
    return metro, nat, gaz_cbsa, gaz_place


def sheet_rows(zip_path, want):
    """Yield header-keyed dicts from the first sheet of the workbook whose basename
    contains `want`. Header names are lowercased and space -> underscore, because
    OEWS flips case and punctuation between vintages (2018 'LOC QUOTIENT' /
    'AREA_NAME' / 'OCC_GROUP' vs 2019 lowercase vs 2020+ uppercase 'O_GROUP')."""
    zf = zipfile.ZipFile(zip_path)
    members = [i.filename for i in zf.infolist()
               if i.filename.lower().endswith(".xlsx")
               and not os.path.basename(i.filename).startswith("~$")]
    picks = [m for m in members if want.lower() in os.path.basename(m).lower()]
    if not picks:
        raise RuntimeError(f"{zip_path.name}: no workbook matching {want!r} in {members}")
    if len(picks) > 1:
        raise RuntimeError(f"{zip_path.name}: ambiguous workbook match {picks}")
    wb = openpyxl.load_workbook(io.BytesIO(zf.read(picks[0])), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    hdr = [str(h).strip().lower().replace(" ", "_") if h is not None else ""
           for h in next(it)]
    for row in it:
        if row is None or all(v is None for v in row):
            continue
        yield dict(zip(hdr, row))
    wb.close()


def col(rec, *names):
    """First present alias. Raises if none exist, so schema drift is loud."""
    for n in names:
        if n in rec:
            return rec[n]
    raise KeyError(f"none of {names} present; header has {sorted(rec)[:40]}")


def cell(v, kind):
    """OEWS cell -> (float|None, marker|None). Counts every marker it sees."""
    if v is None:
        MARKERS[f"{kind}:blank"] += 1
        return None, "blank"
    if isinstance(v, (int, float)):
        return float(v), None
    s = str(v).strip()
    if s == "":
        MARKERS[f"{kind}:blank"] += 1
        return None, "blank"
    if s in SUPPRESSION:
        MARKERS[f"{kind}:{s}"] += 1
        return None, s
    try:
        return float(s.replace(",", "").replace("$", "")), None
    except ValueError:
        MARKERS[f"{kind}:unparsed({s[:12]})"] += 1
        return None, "unparsed"


def num(v, kind):
    """OEWS cell -> float or None (marker discarded)."""
    return cell(v, kind)[0]


def plain_num(v):
    """Parse a cell WITHOUT touching the marker census.

    Used by the pass-A diagnostic scans (wage-cap detection), which read columns
    the output never emits; counting their markers would inflate the suppression
    census that the summary prints for the columns that ARE emitted."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def i(x):
    return None if x is None else int(round(x))


def derive_topcodes(wagecap):
    """{year: {"hourly": $/hr, "annual": $/yr}} - the OEWS wage top code per year.

    Derived from the workbooks rather than hard-coded, because BLS moves it: it was
    $100.00/hr = $208,000/yr for May 2018-2021 and $115.00/hr = $239,200/yr from
    May 2022 on, and the next change should need no edit here.

    The derivation: OEWS censors the median and every percentile at a round hourly
    figure and prints '#' instead, so the largest value it actually publishes sits
    just under that figure ($99.98/hr in 2018, $114.97/hr in 2024).  Rounding that
    per-year maximum UP to the next whole dollar recovers the top code.  The epsilon
    keeps a published value landing exactly on the cap from rounding up a dollar.

    Two independent cross-checks are printed: the hourly cap x 2080 must not be
    below any published ANNUAL percentile, and the maximum published percentile must
    be within a dime of the derived cap (if it is not, no row in that year came near
    the cap and the derivation is not trustworthy - it says so, loudly)."""
    out = {}
    for y in sorted(wagecap):
        w = wagecap[y]
        hourly = math.ceil(w["max_h_pct"] - 1e-9)
        annual = hourly * HOURS_PER_YEAR
        gap = hourly - w["max_h_pct"]
        flags = []
        if gap > 0.10:
            flags.append("*** max published percentile is $%.2f under the derived "
                         "cap - no row approaches it, CHECK ***" % gap)
        if w["max_a_pct"] > annual:
            flags.append("*** a published ANNUAL percentile (%.0f) exceeds the "
                         "derived cap - CHECK ***" % w["max_a_pct"])
        out[y] = {"hourly": hourly, "annual": annual}
        print(f"  {y}: max published percentile {w['max_h_pct']:7.2f}/hr "
              f"{w['max_a_pct']:>10,.0f}/yr  ->  top code ${hourly}.00/hr = "
              f"${annual:,}/yr"
              + ("".join("\n       " + f for f in flags) if flags else ""))
        print(f"        mean is uncensored here: max published annual mean "
              f"{w['max_a_mean']:,.0f} "
              f"({'ABOVE' if w['max_a_mean'] > annual else 'below'} the cap); "
              f"{w['n_mean_tc']} rows have a top-coded mean, "
              f"{w['n_mean_tc_med_pub']} of them beside a PUBLISHED median"
              + (f" ({w['mean_tc_med_lo']:,.0f}-{w['mean_tc_med_hi']:,.0f})"
                 if w["n_mean_tc_med_pub"] else ""))
    return out


# ----------------------------------------------------------------- title parsing
def states_from_title(title):
    m = STATE_TAIL.search(str(title or ""))
    return m.group(1).split("-") if m else []


def first_city(title):
    head = str(title or "").rsplit(",", 1)[0]
    return head.split("-")[0].strip()


def strip_place_suffix(name):
    n = name.strip()
    low = n.lower()
    for suf in sorted(PLACE_SUFFIXES, key=len, reverse=True):
        if low.endswith(" " + suf):
            return n[: -(len(suf) + 1)].strip()
    return n


# ----------------------------------------------------------------------- loaders
def city_tokens(title):
    """{'boston','cambridge','nashua'} from 'Boston-Cambridge-Nashua, MA-NH'."""
    head = str(title or "").rsplit(",", 1)[0]
    return {t.strip().lower() for t in head.split("-") if t.strip()}


def derive_crosswalk(titles):
    """Link areas that vanish after year Y to areas that appear in year Y+1.

    A pair is a candidate when the two titles share at least one principal-city
    token AND at least one component state.  Candidates are then matched greedily,
    best token overlap first, one-to-one, so 'Boston-Cambridge-Nashua, MA-NH' binds
    to 'Boston-Cambridge-Newton, MA-NH' (2 shared cities) rather than to
    'Manchester-Nashua, NH' (1), and 'Manchester, NH' gets the leftover.

    Returns (links {area: set}, accepted [(year, old, oldtitle, new, newtitle,
    shared_tokens)], leftover [(year, why, area, title, candidates)])."""
    years = sorted({y for ty in titles.values() for y in ty})
    links = defaultdict(set)
    accepted, leftover = [], []
    for a, b in zip(years, years[1:]):
        gone = [x for x in titles if a in titles[x] and b not in titles[x]]
        new = [x for x in titles if b in titles[x] and a not in titles[x]]
        pairs = []
        cand = defaultdict(list)
        for g in gone:
            tg, sg = city_tokens(titles[g][a]), set(states_from_title(titles[g][a]))
            for n in new:
                tn, sn = city_tokens(titles[n][b]), set(states_from_title(titles[n][b]))
                shared, sst = tg & tn, sg & sn
                if shared and sst:
                    pairs.append((len(shared), len(sst), g, n, sorted(shared)))
                    cand[g].append((n, sorted(shared)))
        pairs.sort(key=lambda p: (-p[0], -p[1], p[2], p[3]))
        used_g, used_n = set(), set()
        for _ns, _nst, g, n, shared in pairs:
            if g in used_g or n in used_n:
                continue
            used_g.add(g)
            used_n.add(n)
            links[g].add(n)
            links[n].add(g)
            accepted.append((b, g, titles[g][a], n, titles[n][b], shared))
        for g in sorted(gone):
            if g not in used_g:
                leftover.append((b, "no successor", g, titles[g][a],
                                 [(n, titles[n][b]) for n, _ in cand.get(g, [])]))
        for n in sorted(new):
            if n not in used_n:
                leftover.append((b, "no predecessor", n, titles[n][b], []))
    return links, accepted, leftover


def closure(seed, links):
    out, stack = set(seed), list(seed)
    while stack:
        x = stack.pop()
        for y in links.get(x, ()):
            if y not in out:
                out.add(y)
                stack.append(y)
    return out


def load_gaz_cbsa(path):
    """GEOID (CBSA code) -> (lat, lon) internal point."""
    zf = zipfile.ZipFile(path)
    txt = zf.read(zf.infolist()[0].filename).decode("latin-1").splitlines()
    hdr = [h.strip().upper() for h in txt[0].split("\t")]
    ig, ilat, ilon = hdr.index("GEOID"), hdr.index("INTPTLAT"), hdr.index("INTPTLONG")
    out = {}
    for line in txt[1:]:
        p = line.split("\t")
        if len(p) <= ilon:
            continue
        try:
            out[p[ig].strip().zfill(5)] = (round(float(p[ilat]), 4), round(float(p[ilon]), 4))
        except ValueError:
            continue
    return out


def load_gaz_place(path):
    """(USPS, bare place name lowercased) -> (lat, lon). Prefers incorporated
    cities (LSAD 25), then largest land area, so 'Portland city, ME' wins."""
    zf = zipfile.ZipFile(path)
    txt = zf.read(zf.infolist()[0].filename).decode("latin-1").splitlines()
    hdr = [h.strip().upper() for h in txt[0].split("\t")]
    iu, inm, ilsad = hdr.index("USPS"), hdr.index("NAME"), hdr.index("LSAD")
    ial, ilat, ilon = hdr.index("ALAND"), hdr.index("INTPTLAT"), hdr.index("INTPTLONG")
    best = {}
    for line in txt[1:]:
        p = line.split("\t")
        if len(p) <= ilon:
            continue
        try:
            aland, lat, lon = float(p[ial]), float(p[ilat]), float(p[ilon])
        except ValueError:
            continue
        key = (p[iu].strip().upper(), strip_place_suffix(p[inm]).lower())
        rank = (1 if p[ilsad].strip() == "25" else 0, aland)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, (round(lat, 4), round(lon, 4)))
    return {k: v[1] for k, v in best.items()}


def load_national(paths):
    """{year: {occ_code: {"g","t","emp","med","mean","tc"}}} for total/major/detailed.

    "tc" carries the same top-code bits as the metro pass (1=median, 2=mean) so the
    national block can publish a tc list of its own - the national medians for
    physicians and surgeons are top-coded too, and a frontend using national as the
    denominator needs to tell "censored high" from "no data" there as well."""
    out = {}
    for y, p in paths.items():
        rows = {}
        n_tc = 0
        for r in sheet_rows(p, "national"):
            grp = str(col(r, "o_group", "occ_group") or "").strip().lower()
            # 'broad' is kept because the metro files publish some broad codes as
            # their finest metro-level detail (15-1256 in 2019-2020); without it
            # those codes would have no national row for a location-quotient base.
            if grp not in ("total", "major", "broad", "detailed"):
                continue
            code = str(col(r, "occ_code")).strip()
            med, med_mk = cell(col(r, "a_median"), "nat_med")
            mean, mean_mk = cell(col(r, "a_mean"), "nat_mean")
            tc = (1 if med_mk == "#" else 0) | (2 if mean_mk == "#" else 0)
            n_tc += (1 if tc & 1 else 0) + (1 if tc & 2 else 0)
            rows[code] = {
                "g": grp,
                "t": str(col(r, "occ_title")).strip(),
                "emp": num(col(r, "tot_emp"), "nat_emp"),
                "med": med,
                "mean": mean,
                "tc": tc,
            }
        out[y] = rows
        print(f"  national {y}: {len(rows):5d} occ rows kept "
              f"({sum(1 for v in rows.values() if v['g'] == 'detailed')} detailed, "
              f"{sum(1 for v in rows.values() if v['g'] == 'broad')} broad, "
              f"{n_tc} top-coded wage cells)")
    return out


def _row_key(r):
    """(area, title, occ, group) with the per-vintage column aliases resolved."""
    area = str(col(r, "area")).strip()
    if area.endswith(".0"):
        area = area[:-2]
    return (area.zfill(5),
            str(col(r, "area_title", "area_name")).strip(),
            str(col(r, "occ_code")).strip(),
            str(col(r, "o_group", "occ_group") or "").strip().lower())


def _area_type_ok(r, y, seen):
    at = r.get("area_type")
    if at is None:
        return True
    at = str(at).strip()
    if at.endswith(".0"):
        at = at[:-2]
    seen[y][at] += 1
    return at == "4"


def scan_metro(paths):
    """PASS A over the seven MSA workbooks - catalogs only, no wage cells.

    The occupation menu has to be built from what the METRO files publish, not
    from the national file: for May 2019 and May 2020 BLS published Software
    Developers (15-1252) and Software QA (15-1253) separately nationally but only
    as the combined 15-1256 at metro level, so a national-derived menu silently
    loses every 2019-2020 software-developer estimate.

    Returns
      titles   {area: {year: title}}
      pstate   {area: {year: prim_state}}
      occmeta  {occ: {"t": newest title, "g": newest group, "y": set(years)}}
      occ_emp  {(occ, year): summed employment over ALL metro areas}  (ranking only)
      itemp    {(occ, year): {area: employment}} for detailed 15-xxxx  (rule M2)
      area_tot {area: {year: 00-0000 employment}}                      (rule M1)
      wagecap  {year: top-code evidence} -> derive_topcodes()
      stats, area_types_seen
    """
    titles, pstate = defaultdict(dict), defaultdict(dict)
    occmeta, occ_emp, itemp = {}, defaultdict(float), defaultdict(dict)
    area_tot = defaultdict(dict)
    stats, area_types_seen = {}, defaultdict(Counter)
    wagecap = {}
    for y in sorted(paths):
        rows_in = dropped_type = 0
        areas = set()
        # top-code evidence for this year (see derive_topcodes)
        cap = {"max_h_pct": 0.0, "max_a_pct": 0.0, "max_a_mean": 0.0,
               "n_mean_tc": 0, "n_mean_tc_med_pub": 0,
               "mean_tc_med_lo": None, "mean_tc_med_hi": None}
        for r in sheet_rows(paths[y], "MSA"):
            rows_in += 1
            if not _area_type_ok(r, y, area_types_seen):
                dropped_type += 1
                continue
            for f in PCT_HOURLY:
                v = plain_num(r.get(f))
                if v is not None and v > cap["max_h_pct"]:
                    cap["max_h_pct"] = v
            for f in PCT_ANNUAL:
                v = plain_num(r.get(f))
                if v is not None and v > cap["max_a_pct"]:
                    cap["max_a_pct"] = v
            am = plain_num(r.get("a_mean"))
            if am is not None and am > cap["max_a_mean"]:
                cap["max_a_mean"] = am
            if str(r.get("a_mean") or "").strip() == "#":
                cap["n_mean_tc"] += 1
                amed = plain_num(r.get("a_median"))
                if amed is not None:
                    cap["n_mean_tc_med_pub"] += 1
                    if cap["mean_tc_med_lo"] is None or amed < cap["mean_tc_med_lo"]:
                        cap["mean_tc_med_lo"] = amed
                    if cap["mean_tc_med_hi"] is None or amed > cap["mean_tc_med_hi"]:
                        cap["mean_tc_med_hi"] = amed
            area, title, occ, grp = _row_key(r)
            areas.add(area)
            titles[area][y] = title
            ps = r.get("prim_state")
            if ps:
                pstate[area][y] = str(ps).strip().upper()
            meta = occmeta.setdefault(occ, {"t": None, "g": grp, "y": set()})
            meta["t"] = str(col(r, "occ_title")).strip()   # later year wins
            meta["y"].add(y)
            if grp:
                meta["g"] = grp
            e = num(r.get("tot_emp"), "metro_emp_scan")
            if e is not None:
                occ_emp[(occ, y)] += e
                if occ == "00-0000":
                    area_tot[area][y] = int(round(e))
                if grp == "detailed" and IT_RE.match(occ):
                    itemp[(occ, y)][area] = e
        stats[y] = {"rows_in": rows_in, "dropped_area_type": dropped_type,
                    "areas": len(areas)}
        wagecap[y] = cap
        print(f"  scan {y}: {rows_in:7,d} rows  {len(areas):3d} areas  "
              f"{len({o for (o, yy) in occ_emp if yy == y}):3d} occ codes  "
              f"dropped_by_area_type={dropped_type}  "
              f"area_type={dict(area_types_seen[y]) or 'COLUMN ABSENT'}")
    return (titles, pstate, occmeta, occ_emp, itemp, area_tot, wagecap, stats,
            area_types_seen)


def extract_metro(paths, keep_occ, keep_area):
    """PASS B - pull the wage/employment cells for the selected areas x occupations.

    cells {(area, occ): {year: (emp, a_median, a_mean, topcode_bits)}}"""
    cells = defaultdict(dict)
    kept = {}
    for y in sorted(paths):
        n = 0
        seen = defaultdict(Counter)
        for r in sheet_rows(paths[y], "MSA"):
            if not _area_type_ok(r, y, seen):
                continue
            area, _t, occ, _g = _row_key(r)
            if area not in keep_area or occ not in keep_occ:
                continue
            n += 1
            emp, _ = cell(r.get("tot_emp"), "metro_emp")
            med, med_mk = cell(r.get("a_median"), "metro_a_median")
            mean, mean_mk = cell(r.get("a_mean"), "metro_a_mean")
            # bit 1 = median top-coded ('#'), bit 2 = mean top-coded ('#')
            tc = (1 if med_mk == "#" else 0) | (2 if mean_mk == "#" else 0)
            cells[(area, occ)][y] = (i(emp), i(med), i(mean), tc)
        kept[y] = n
        print(f"  extract {y}: {n:7,d} rows kept")
    return cells, kept


# --------------------------------------------------------------------------- main
def main():
    print("== download ==")
    metro_paths, nat_paths, gaz_cbsa, gaz_place = download()
    latest, earliest = max(YEARS), min(YEARS)

    print("== national ==")
    nat = load_national(nat_paths)

    print("== metro pass A: catalog scan ==")
    (titles, pstate, occmeta, occ_emp, itemp, area_tot, wagecap, stats,
     at_seen) = scan_metro(metro_paths)

    print("== wage top code, derived per year from the published data ==")
    topcode = derive_topcodes(wagecap)

    # ---- occupation selection, from what the METRO files actually publish -------
    o1 = {"00-0000"}
    o2 = {c for c, v in occmeta.items() if v["g"] == "major" or MAJOR_RE.match(c)}
    o3 = {c for c in occmeta if IT_RE.match(c)}
    o4, o4_by_year = set(), {}
    for y in YEARS:
        d = sorted(((c, e) for (c, yy), e in occ_emp.items()
                    if yy == y and occmeta[c]["g"] == "detailed"),
                   key=lambda t: (-t[1], t[0]))
        o4_by_year[y] = [c for c, _ in d[:NAT_TOP_N]]
        o4 |= set(o4_by_year[y])
    keep_occ = o1 | o2 | o3 | o4
    print(f"  occ candidates: total=1  major={len(o2)}  15-xxxx(all groups)={len(o3)}  "
          f"top{NAT_TOP_N}-detailed-per-year union={len(o4)}  -> keep={len(keep_occ)}")
    for y in YEARS:
        new_here = set(o4_by_year[y]) - set(o4_by_year[latest])
        print(f"      {y}: top{NAT_TOP_N} adds {len(new_here)} codes not in the "
              f"{latest} top{NAT_TOP_N} (SOC vintage drift)")
    print(f"  occupation codes published at metro level but NOT selected: "
          f"{len(set(occmeta) - keep_occ)}")

    # ---- SOC vintage boundaries, MEASURED from the metro files -------------------
    # The metro breaks are not the national ones: at metro level 2018 is the only
    # 2010-SOC year (2019 is already 2018 SOC, with the aggregated 15-1256/15-1245/
    # 15-1257/15-2098 codes), and 2019 vs 2020 is not a vintage break at all.
    # Measuring it here keeps meta.soc_warning from going stale.
    codes_by_year = {y: {c for c, v in occmeta.items() if y in v["y"]} for y in YEARS}
    soc_breaks = []
    for ya, yb in zip(YEARS, YEARS[1:]):
        gone, new = codes_by_year[ya] - codes_by_year[yb], codes_by_year[yb] - codes_by_year[ya]
        soc_breaks.append({
            "from": ya, "to": yb, "retired": len(gone), "added": len(new),
            "retired_15xxxx": sorted(c for c in gone if IT_RE.match(c)),
            "added_15xxxx": sorted(c for c in new if IT_RE.match(c)),
        })
    print("  SOC vintage boundaries measured at metro level:")
    for d in soc_breaks:
        tag = "MAJOR BREAK" if d["retired"] > 10 else "minor      "
        print(f"      {d['from']}->{d['to']}: {tag}  {d['retired']:3d} codes retired, "
              f"{d['added']:3d} added  (15-xxxx: -{len(d['retired_15xxxx'])} "
              f"+{len(d['added_15xxxx'])})")
    big = [d for d in soc_breaks if d["retired"] > 10]
    print(f"      -> vintage breaks are "
          + ", ".join(f"{d['from']}|{d['to']}" for d in big)
          + f"; {len(soc_breaks) - len(big)} other year pairs are the same vintage")

    # 2018 has no AREA_TYPE column; check its areas against the typed years
    typed_areas = {a for a in titles for y in titles[a] if at_seen[y]}
    for y in YEARS:
        if at_seen[y]:
            continue
        ua = {a for a in titles if y in titles[a]}
        extra = ua - typed_areas
        print(f"  {y}: no AREA_TYPE column; {len(ua)} areas, {len(extra)} never seen "
              f"as AREA_TYPE==4 in another year"
              + (f" -> {sorted(extra)}" if extra else " (rule verified)"))

    # ---- metro selection --------------------------------------------------------
    # Display size for every area: its `latest` total where it has one, otherwise the
    # most recent total it does have (with the year it came from).  This is the sort
    # key for reporting only - the M1 ranking below deliberately does NOT use it.
    tot_by_area = {}          # area -> (employment, year_used)
    for area, yv in area_tot.items():
        for y in sorted(yv, reverse=True):
            if yv[y] is not None:
                tot_by_area[area] = (yv[y], y)
                break

    # M1 - the brief's rule taken literally: rank on the MOST RECENT YEAR ALONE.
    # Only areas that actually have a `latest` 00-0000 value compete, so the cutoff
    # reported here is a true `latest` cutoff.  Ranking on "best available year"
    # instead would let area codes retired before `latest` occupy M1 slots at their
    # last-published size and displace areas that really are in the `latest`
    # top TOP_METROS; those retired codes are rule M1b's job, counted separately.
    ranked = sorted(((a, yv[latest]) for a, yv in area_tot.items()
                     if yv.get(latest) is not None),
                    key=lambda t: (-t[1], t[0]))
    m1 = {a for a, _ in ranked[:TOP_METROS]}
    cutoff = ranked[min(TOP_METROS, len(ranked)) - 1][1]
    print(f"  M1: top {len(m1)} of the {len(ranked)} areas that publish a {latest} "
          f"00-0000 total; cutoff = {cutoff:,} jobs in {latest}")
    print(f"      (of {len(tot_by_area)} areas in the seven files, "
          f"{len(tot_by_area) - len(ranked)} have no {latest} total and are not "
          f"ranked here)")

    # M1b - area codes retired before `latest`.  They cannot appear in M1 at all, so
    # without this rule an area as large as Boston-Cambridge-Nashua would lose its
    # entire 2018-2023 history the moment its CBSA code was retired.  Ranked on the
    # last year each one published, and kept when that total clears the SAME cutoff.
    m1b = {a for a, (e, yy) in tot_by_area.items() if yy != latest and e >= cutoff}
    retired = sorted((a for a, (_e, yy) in tot_by_area.items() if yy != latest),
                     key=lambda a: -tot_by_area[a][0])
    print(f"  M1b: {len(m1b)} of the {len(retired)} retired area codes clear the same "
          f"{cutoff:,} cutoff on their last published year and are kept:")
    for a in retired:
        e, yy = tot_by_area[a]
        print(f"      {'kept  ' if a in m1b else 'below '} {a} {titles[a][yy]} = "
              f"{e:,} jobs (last published {yy})")

    m2, m2_reason = set(), defaultdict(list)
    for (occ, y), d in itemp.items():
        for a, _e in sorted(d.items(), key=lambda t: (-t[1], t[0]))[:IT_TOP_N]:
            m2.add(a)
            m2_reason[a].append(f"{occ}@{y}")
    m2_only = m2 - m1 - m1b
    print(f"  M2: {len(m2)} metros are top-{IT_TOP_N} for some detailed 15-xxxx "
          f"occupation in some year; {len(m2_only)} are not in M1/M1b and are rescued:")
    for a in sorted(m2_only, key=lambda x: -tot_by_area.get(x, (0, 0))[0]):
        t = titles[a].get(latest) or titles[a][max(titles[a])]
        print(f"      rescued {a} {t} (total emp {tot_by_area.get(a, (0, 0))[0]:,}; "
              f"{len(m2_reason[a])} top-{IT_TOP_N} IT appearances, "
              f"e.g. {', '.join(sorted(m2_reason[a])[:3])})")

    # ---- M3: crosswalk closure over the OMB re-delineations ---------------------
    links, proposals, unmatched = derive_crosswalk(titles)
    print(f"  crosswalk: {len(proposals)} renamed/recoded area links derived from "
          f"shared principal city + shared state:")
    for b, g, gt, n, nt, shared in proposals:
        print(f"      {b}: {g} {gt}  ->  {n} {nt}   [{'/'.join(shared)}]")
    for b, why, a, t, alt in unmatched:
        print(f"      {b}: {why:14s} {a} {t}"
              + (f"  (unused candidates {alt})" if alt else ""))
    keep_area = closure(m1 | m1b | m2, links)
    m3_only = keep_area - m1 - m1b - m2
    print(f"  M3: {len(m3_only)} areas added as the other half of a kept place: "
          + "; ".join(f"{a} {titles[a][max(titles[a])]}" for a in sorted(m3_only)))

    dropped_area = sorted(set(titles) - keep_area,
                          key=lambda a: -tot_by_area.get(a, (0, 0))[0])
    print(f"  metros kept {len(keep_area)} of {len(titles)} "
          f"(M1 {len(m1)} + M1b {len(m1b)} + M2 {len(m2_only)} + M3 {len(m3_only)}); "
          f"dropped {len(dropped_area)}")
    print("      largest dropped: " + "; ".join(
        f"{a} {titles[a].get(latest) or titles[a][max(titles[a])]} "
        f"({tot_by_area.get(a, (0, 0))[0]:,})" for a in dropped_area[:5]))
    # every dropped area is genuinely below the cutoff - assert it rather than
    # trusting the arithmetic, since this is exactly what went wrong before
    over = [a for a in dropped_area
            if area_tot.get(a, {}).get(latest) is not None
            and area_tot[a][latest] > cutoff]
    print(f"      dropped areas above the {latest} cutoff: {len(over)}"
          + (f"  *** {over} ***" if over else "  (none - cutoff is exact)"))
    print(f"      dropped total employment {sum(tot_by_area.get(a, (0, 0))[0] for a in dropped_area):,} "
          f"vs kept {sum(tot_by_area.get(a, (0, 0))[0] for a in keep_area):,}")

    print("== metro pass B: cell extraction ==")
    cells, kept_rows = extract_metro(metro_paths, keep_occ, keep_area)
    for y in YEARS:
        stats[y]["rows_kept"] = kept_rows[y]

    # ---- indexes ----------------------------------------------------------------
    present = {occ for (a, occ) in cells if a in keep_area}
    occ_codes = sorted(present, key=lambda c: (c != "00-0000", c))
    occ_ix = {c: k for k, c in enumerate(occ_codes)}
    missing_occ = sorted(keep_occ - present)
    if missing_occ:
        print(f"  occ candidates with no row in any kept metro, dropped "
              f"({len(missing_occ)}): {missing_occ}")

    cbsa_pts = {}
    for gy, gp in reversed(gaz_cbsa):        # oldest first so newest overwrites
        cbsa_pts.update(load_gaz_cbsa(gp))
    place_pts = load_gaz_place(gaz_place[1]) if gaz_place else {}
    print(f"  gazetteer cbsa={[gy for gy, _ in gaz_cbsa]} merged -> {len(cbsa_pts)} pts, "
          f"place={gaz_place[0] if gaz_place else None} ({len(place_pts)} pts)")

    metros, geo_counts, no_geo, via_place, via_link = [], Counter(), [], [], []
    for area in sorted(keep_area, key=lambda a: (-tot_by_area.get(a, (0, 0))[0], a)):
        ty = titles[area]
        title = ty.get(latest) or ty[max(ty)]
        sts = states_from_title(title)
        ps = pstate.get(area, {}).get(latest)
        if not ps and pstate.get(area):
            ps = pstate[area][max(pstate[area])]
        if not ps:
            ps = sts[0] if sts else None
        # 1) the area's own CBSA gazetteer internal point.
        pt, src = cbsa_pts.get(area), "c"
        # 2) the internal point of the area this one was recoded to/from, so the two
        #    halves of a re-delineated place sit on the same map dot instead of
        #    jumping when the series crosses the 2024 code change. Also covers the
        #    OEWS-only codes (19380 Dayton, 39140 Prescott) that no gazetteer has.
        if pt is None:
            for other in sorted(links.get(area, set())):
                if other in cbsa_pts:
                    pt, src = cbsa_pts[other], "l"
                    via_link.append((area, title, other))
                    break
        # 3) the first principal city's internal point (NECTAs, which have no CBSA
        #    gazetteer record at all).
        if pt is None:
            pt = place_pts.get((sts[0] if sts else "", first_city(title).lower()))
            src = "p"
            if pt is not None:
                via_place.append((area, title, first_city(title)))
        if pt is None:
            src = None
            no_geo.append((area, title))
        geo_counts[src] += 1
        rec = {"a": area, "t": title, "s": sts, "ps": ps,
               "lat": pt[0] if pt else None, "lon": pt[1] if pt else None, "g": src}
        # historical titles, recorded only where the title actually changes, keyed
        # by the first year that spelling was used (the current spelling is "t")
        alt, prev = {}, None
        for y, t in sorted(ty.items()):
            if t != prev and t != title:
                alt[str(y)] = t
            prev = t
        if alt:
            rec["yt"] = alt
        xl = sorted(links.get(area, set()) & keep_area)
        if xl:
            rec["x"] = xl
        metros.append(rec)
    print(f"  geocoding: own_cbsa={geo_counts['c']} linked_cbsa={geo_counts['l']} "
          f"place_centroid={geo_counts['p']} none={geo_counts[None]}")
    for a, t, o in via_link:
        print(f"      via linked CBSA {o}: {a} {t}")
    for a, t, c in via_place:
        print(f"      via place centroid ({c}): {a} {t}")
    if no_geo:
        print(f"  *** NO COORDINATES ({len(no_geo)}): {no_geo}")

    # ---- series -----------------------------------------------------------------
    series, nz, n_tc = [], 0, 0
    NA = (None, None, None, 0)
    for m in metros:
        area = m["a"]
        oi, emp, med, mean, topc = [], [], [], [], []
        for c in occ_codes:
            yv = cells.get((area, c))
            if not yv:
                continue
            e = [yv.get(y, NA)[0] for y in YEARS]
            md = [yv.get(y, NA)[1] for y in YEARS]
            mn = [yv.get(y, NA)[2] for y in YEARS]
            if all(v is None for v in e) and all(v is None for v in md) \
                    and all(v is None for v in mn):
                continue
            row = len(oi)
            for k, y in enumerate(YEARS):
                tc = yv.get(y, NA)[3]
                if tc & 1:
                    topc.append([row, k, 0])
                if tc & 2:
                    topc.append([row, k, 1])
            oi.append(occ_ix[c])
            emp.append(e)
            med.append(md)
            mean.append(mn)
            nz += sum(1 for v in e if v is not None)
        n_tc += len(topc)
        rec = {"o": oi, "e": emp, "m": med, "w": mean}
        if topc:
            rec["tc"] = topc
        series.append(rec)

    nat_o, nat_e, nat_m, nat_w, nat_tc = [], [], [], [], []
    for c in occ_codes:
        e = [i(nat[y][c]["emp"]) if c in nat[y] else None for y in YEARS]
        md = [i(nat[y][c]["med"]) if c in nat[y] else None for y in YEARS]
        mn = [i(nat[y][c]["mean"]) if c in nat[y] else None for y in YEARS]
        if all(v is None for v in e + md + mn):
            continue
        row = len(nat_o)
        for k, y in enumerate(YEARS):
            t = nat[y][c]["tc"] if c in nat[y] else 0
            if t & 1:
                nat_tc.append([row, k, 0])
            if t & 2:
                nat_tc.append([row, k, 1])
        nat_o.append(occ_ix[c])
        nat_e.append(e)
        nat_m.append(md)
        nat_w.append(mn)

    occupations = [{
        "c": c,
        "t": occmeta[c]["t"],
        "g": occmeta[c]["g"],
        "y": [1 if y in occmeta[c]["y"] else 0 for y in YEARS],
        "it": 1 if (IT_RE.match(c) and occmeta[c]["g"] == "detailed") else 0,
    } for c in occ_codes]

    out = {
        "meta": {
            "title": "BLS OEWS metropolitan-area occupational employment and wages",
            "source": "https://www.bls.gov/oes/special-requests/oesm{YY}ma.zip (MSA workbook only)",
            "national_source": "https://www.bls.gov/oes/special-requests/oesm{YY}nat.zip",
            "gazetteer_cbsa": [f"{gy}_Gaz_cbsa_national" for gy, _ in gaz_cbsa],
            "gazetteer_place": f"{gaz_place[0]}_Gaz_place_national" if gaz_place else None,
            "generated": datetime.date.today().isoformat(),
            "years": YEARS,
            "reference_period": "May of each year",
            "metro_rule": (
                "MSA workbook only (the BOS nonmetro workbook is never read); "
                f"AREA_TYPE==4 where the column exists ({earliest} predates it, so the "
                "whole MSA sheet is treated as metro and verified against later years). "
                f"Kept = (M1) the top {TOP_METROS} areas by 00-0000 employment in "
                f"{latest} ALONE - only the {len(ranked)} areas that publish a {latest} "
                f"total compete, so the cutoff of {cutoff:,} jobs is a true {latest} "
                f"cutoff and no area above it is dropped; UNION (M1b) the {len(m1b)} "
                f"area codes retired before {latest} whose last published 00-0000 total "
                "still clears that same cutoff, ranked on their final year, so a place "
                "whose CBSA code was retired by the OMB 2023 re-delineation (every New "
                "England NECTA, Cleveland-Elyria, Dayton, ...) keeps its 2018-2023 "
                f"history; UNION (M2) every area that is top-{IT_TOP_N} for any detailed "
                "15-xxxx occupation in any year; UNION (M3) the crosswalk closure of "
                "those sets. M1 and M1b are counted separately so retired codes can "
                f"never occupy M1 slots and displace genuine {latest} top-{TOP_METROS} "
                "areas. Through 2023 the New England areas are NECTAs (CBSA "
                "70000-79999), as OEWS published them."),
            "crosswalk": (
                "OEWS 2024 adopted the OMB 2023 delineations: NECTAs were replaced by "
                "county-based MSAs and several areas were renamed/recoded, so a place can "
                "appear as two records with disjoint year coverage. metros[].x lists the "
                "other area code(s) for the same place, derived by requiring a shared "
                "principal-city token and a shared component state with a unique match in "
                "both directions. The county sets differ, so the two halves are NOT summed "
                "or spliced here - a level shift across the join is expected."),
            "occ_rule": (
                "Kept = 00-0000 + all major groups XX-0000 + every 15-xxxx code the metro "
                f"files publish in any SOC vintage + the {NAT_TOP_N} largest detailed "
                "occupations of EACH year, unioned over the seven years. The menu comes "
                "from the metro workbooks, not the national workbook, because 2019-2020 "
                "publish combined metro codes (15-1256) whose national counterparts are "
                "split (15-1252 / 15-1253); 'largest' is ranked on employment summed over "
                "all OEWS metro areas for that year, suppressed cells skipped."),
            "soc_warning": (
                "SOC vintages AT METRO LEVEL, measured from the workbooks rather than "
                "assumed - they are NOT the national release's vintages. "
                f"{earliest} is the only 2010-SOC year here: 15-1132 Software "
                "Developers Applications and every other 2010 computer/math code "
                f"appears in {earliest} and in no later year. 2019 and 2020 are "
                "already 2018 SOC, but with the aggregated codes BLS used while the "
                "sample rotated in (15-1256 Software Developers and Software Quality "
                "Assurance Analysts and Testers, 15-1245 Database and Network "
                "Administrators and Architects, 15-1257, 15-2098); they publish "
                "essentially the same code list as each other, so 2019|2020 is NOT a "
                "vintage break and 2020 is not a 'hybrid' year at metro level. "
                "2021-2024 are 2018 SOC with those aggregates split apart (15-1252 "
                "Software Developers, 15-1253 Software QA, 15-1242/15-1243, 15-2051). "
                "Measured code churn per year pair: "
                + "; ".join(f"{d['from']}|{d['to']} -{d['retired']}/+{d['added']}"
                            for d in soc_breaks)
                + ". So the breaks are "
                + " and ".join(f"{d['from']}|{d['to']}" for d in soc_breaks
                               if d["retired"] > 10)
                + ". Renumbered occupations appear as two or three codes with "
                "disjoint year coverage (15-1132, then 15-1256, then 15-1252); no "
                "crosswalk is applied. Check occupations[].y before joining a series "
                "across a break - see soc_breaks."),
            "soc_breaks": soc_breaks,
            "suppression": (
                "BLS markers *, **, #, ~ and blanks all become null, never 0. "
                "'*' = estimate not released, '**' = data not available, "
                "'~' = employment rounds to less than 0.05%, '#' = the wage estimate "
                "is at or above the OEWS top code. '#' is a censored HIGH value rather "
                "than a missing one, so no value is invented but its position is "
                "listed in series[].tc and national.tc as "
                "[row, year_index, 0=median|1=mean]. THE TOP CODE CHANGES WITHIN THIS "
                "FILE - read topcode.annual[year_index], never one global constant."),
            "topcode": {
                "hourly": [topcode[y]["hourly"] for y in YEARS],
                "annual": [topcode[y]["annual"] for y in YEARS],
                "applies_to": (
                    "tc entries with metric 0 (median) in series[].tc and national.tc: "
                    "the true value is >= topcode.annual[year_index] per year "
                    "(topcode.hourly[year_index] per hour). Metric 1 (mean) carries NO "
                    "value bound - see mean_note."),
                "derivation": (
                    "Derived from these workbooks, not hard-coded. OEWS censors the "
                    "median and every percentile at a round hourly figure and prints "
                    "'#', so the largest value it actually publishes sits a few cents "
                    "under that figure; rounding the per-year maximum up to the next "
                    "whole dollar recovers the top code. Max published percentile by "
                    "year: "
                    + "; ".join(f"{y} ${wagecap[y]['max_h_pct']:.2f}/hr" for y in YEARS)
                    + ". BLS raised the top code from $100.00/hr ($208,000/yr) to "
                    "$115.00/hr ($239,200/yr) with the May 2022 release, so labelling "
                    "every year '>= $208,000' would understate every 2022-2024 "
                    "top-coded median by $31,200 - and those years publish wage "
                    "percentiles up to $"
                    f"{max(wagecap[y]['max_a_pct'] for y in YEARS):,.0f} outright, "
                    "well above $208,000."),
                "mean_note": (
                    "The MEAN is not censored at the top code. Published annual means "
                    "exceed it in every year (max by year: "
                    + "; ".join(f"{y} {wagecap[y]['max_a_mean']:,.0f}" for y in YEARS)
                    + "), so a '#' on a_mean does not mean 'mean >= top code'; it means "
                    "only 'not published, censored high'. In most years every "
                    "top-coded mean sits in a row whose whole percentile distribution "
                    "is top-coded too, but not always - rows with a top-coded mean "
                    "beside a PUBLISHED median, by year: "
                    + "; ".join(f"{y} {wagecap[y]['n_mean_tc_med_pub']}" for y in YEARS)
                    + ". Render metric-1 tc cells as 'not published' with no number."),
            },
            "units": {"e": "employment (persons)", "m": "annual median wage (USD)",
                      "w": "annual mean wage (USD)",
                      "tc": "top-coded wage cells, [row, year_index, 0=median|1=mean]. "
                            "For metric 0 the true value is >= "
                            "topcode.annual[year_index]; for metric 1 no value bound is "
                            "asserted (see topcode.mean_note)"},
            "fields": {
                "occupations[i]": "c=SOC code, t=title, g=total|major|detailed, "
                                  "y=per-year presence flags aligned to years[], "
                                  "it=1 for detailed 15-xxxx (computer/math)",
                "metros[i]": "a=CBSA/NECTA code, t=current title, s=component states, "
                             "ps=BLS primary state, lat/lon=map point, g=geo source, "
                             "x=crosswalk partner codes, yt={first year: older title} "
                             "recorded only where the title changed",
                "series[i]": "aligned to metros[i]. o=occupation indexes into "
                             "occupations[]; e/m/w are parallel arrays of 7-element "
                             "per-year arrays aligned to years[]; tc lists top-coded "
                             "wage positions",
                "national": "same shape as series[i] (o/e/m/w plus its own tc list), "
                            "the US totals for the same occupation indexes - use it as "
                            "the base for location quotients",
            },
            "geo": {"c": "the area's own CBSA gazetteer internal point",
                    "l": "the CBSA gazetteer internal point of its crosswalk partner "
                         "(metros[].x), so both halves of a re-delineated place share "
                         "one map dot; also covers OEWS-only codes such as 19380 Dayton "
                         "and 39140 Prescott that no gazetteer vintage carries",
                    "p": "place gazetteer internal point of the first principal city "
                         "(NECTAs, which have no CBSA gazetteer record at all)"},
            "counts": {
                "metros_available": len(titles),
                "metros_kept": len(keep_area),
                "metros_dropped": len(dropped_area),
                "metros_kept_by_size_rule_m1": len(m1),
                "metros_kept_retired_codes_m1b": len(m1b),
                "metros_rescued_by_it_rule": len(m2_only),
                "metros_added_by_crosswalk": len(m3_only),
                "metros_ranked_in_latest_year": len(ranked),
                "metro_crosswalk_links": len(proposals),
                "metro_employment_cutoff": cutoff,
                "metro_employment_cutoff_year": latest,
                "topcoded_wage_cells": n_tc,
                "topcoded_wage_cells_national": len(nat_tc),
                "occ_kept": len(occ_codes),
                "occ_major": sum(1 for o in occupations if o["g"] == "major"),
                "occ_it_detailed": sum(o["it"] for o in occupations),
                "rows_in": sum(s["rows_in"] for s in stats.values()),
                "rows_kept": sum(s["rows_kept"] for s in stats.values()),
                "nonnull_employment_cells": nz,
            },
            "per_year_rows_in": {str(y): stats[y]["rows_in"] for y in YEARS},
            "per_year_areas_in_file": {str(y): stats[y]["areas"] for y in YEARS},
        },
        "years": YEARS,
        "occupations": occupations,
        "metros": metros,
        "series": series,
        "national": {"o": nat_o, "e": nat_e, "m": nat_m, "w": nat_w, "tc": nat_tc},
    }
    write_json(out, OUT)

    # ------------------------------------------------------------------ summary --
    print("\n== summary ==")
    print(f"  rows in    : {sum(s['rows_in'] for s in stats.values()):,} "
          f"across {len(YEARS)} MSA workbooks")
    print(f"  rows kept  : {sum(s['rows_kept'] for s in stats.values()):,}")
    print(f"  metros     : {len(keep_area)} kept / {len(titles)} available "
          f"= M1 {len(m1)} biggest in {latest} (cutoff {cutoff:,}) "
          f"+ M1b {len(m1b)} retired codes above that cutoff "
          f"+ M2 {len(m2_only)} rescued by the 15-xxxx rule "
          f"+ M3 {len(m3_only)} crosswalk halves")
    print(f"  occupations: {len(occ_codes)} = 1 total + "
          f"{sum(1 for o in occupations if o['g'] == 'major')} major + "
          f"{sum(o['it'] for o in occupations)} detailed 15-xxxx + "
          f"{sum(1 for o in occupations if o['g'] == 'detailed' and not o['it'])} "
          f"other detailed")
    print("  per-year coverage in the output:")
    for k, y in enumerate(YEARS):
        na = sum(1 for s in series if any(r[k] is not None for r in s["e"]))
        nc = sum(1 for s in series for r in s["e"] if r[k] is not None)
        no = len({o for s in series for o, r in zip(s["o"], s["e"]) if r[k] is not None})
        nw = sum(1 for s in series for r in s["m"] if r[k] is not None)
        print(f"    {y}: metros={na:3d} occupations={no:3d} "
              f"employment cells={nc:,} median-wage cells={nw:,}")
    print(f"  top-coded wage cells recorded in series[].tc: {n_tc:,} "
          f"(+{len(nat_tc):,} in national.tc), all emitted as null:")
    for k, y in enumerate(YEARS):
        nmed = sum(1 for s in series for t in s.get("tc", ()) if t[1] == k and t[2] == 0)
        nmean = sum(1 for s in series for t in s.get("tc", ()) if t[1] == k and t[2] == 1)
        print(f"    {y}: median {nmed:4d} (each >= ${topcode[y]['annual']:,}/yr = "
              f"${topcode[y]['hourly']}.00/hr)   mean {nmean:3d} (no value bound "
              f"asserted - the mean is not censored at the top code)")
    print(f"  crosswalk links emitted on metros[].x: "
          f"{sum(1 for m in metros if 'x' in m)} metros")
    print("  suppression / marker counts (upstream cells read, incl. the IT scan):")
    for k, v in sorted(MARKERS.items(), key=lambda t: -t[1]):
        print(f"    {k:34s} {v:,}")
    size = OUT.stat().st_size / 1e6
    print(f"  output     : {OUT}  {size:.2f} MB (budget {BUDGET_MB} MB)")
    if size > BUDGET_MB:
        print(f"  *** OVER BUDGET - lower TOP_METROS (never the 15-xxxx occupations) ***")

    # ------------------------------------------------------------ sanity checks --
    print("\n== sanity checks ==")
    ix = {m["a"]: k for k, m in enumerate(metros)}

    def show(area, occ):
        k = ix.get(area)
        if k is None:
            print(f"    {area:>5s} {occ}: metro NOT kept")
            return
        s = series[k]
        j = occ_ix.get(occ)
        if j is None or j not in s["o"]:
            print(f"    {area:>5s} {metros[k]['t'][:34]:36s} {occ}: no rows")
            return
        r = s["o"].index(j)
        print(f"    {metros[k]['t'][:34]:36s} {occ} emp={s['e'][r]}")
        print(f"    {'':36s} {'':7s} med={s['m'][r]}")

    for a, o in (("35620", "00-0000"), ("31080", "00-0000"), ("16980", "00-0000"),
                 ("41940", "15-1252"), ("41940", "15-1132"), ("36420", "15-1252"),
                 ("39340", "15-1252"), ("14460", "00-0000"), ("71650", "00-0000")):
        show(a, o)
    j = occ_ix.get("00-0000")
    tot24 = sum(s["e"][s["o"].index(j)][-1] or 0 for s in series if j in s["o"])
    natj = nat_o.index(j) if j in nat_o else None
    print(f"    sum of kept-metro total employment {YEARS[-1]}: {tot24:,}")
    if natj is not None:
        print(f"    national total employment {YEARS[-1]}:          "
              f"{nat_e[natj][-1]:,}  "
              f"(kept metros = {100.0 * tot24 / nat_e[natj][-1]:.1f}% of US)")
    for m in metros[:4]:
        print(f"    geo {m['a']} {m['t'][:38]:40s} {m['lat']},{m['lon']} src={m['g']} "
              f"states={m['s']}")


if __name__ == "__main__":
    main()
