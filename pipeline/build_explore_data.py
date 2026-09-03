"""
build_explore_data.py - slice data/*.json into browser payloads for site/explore.html.

The site is served with site/ as the document root, so the page cannot read ../data/.
Everything the explore page needs is written to site/explore/data/*.json by this script.

Design constraints (from the brief):
  * initial page load < 1.5 MB, everything else lazy
  * data/oews_state.json alone is 10.9 MB, so the per-occupation state series MUST be split
  * nulls, suppression markers and top-codes are preserved and distinguishable
  * every number is traceable to a file in data/; nothing is invented

Idempotent: no wall-clock timestamp is written. Provenance is recorded as the sha256 prefix
and byte size of every input file, so re-running on unchanged inputs produces byte-identical
output and `git status` stays clean.

Run:  uv run python pipeline/build_explore_data.py
"""

import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA, SITE_DATA  # noqa: E402

OUT = SITE_DATA / "data"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------
BUDGET_MB = 1.5
# Everything the page fetches before it can paint the default view (#map, showing
# "All Occupations"). Every other payload is fetched on demand by App.load().
INITIAL = ["manifest", "geo", "occ_index", "series_g00"]
LAZY_SOFT_LIMIT_MB = 1.0  # a single lazy payload above this gets a loud warning

WRITTEN = {}  # name -> bytes
PROVENANCE = {}  # data/<file> -> {bytes, sha256}


def emit(name, obj):
    path = OUT / f"{name}.json"
    blob = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    path.write_text(blob, encoding="utf-8")
    WRITTEN[name] = len(blob.encode("utf-8"))
    return WRITTEN[name]


def load(name):
    p = DATA / name
    raw = p.read_bytes()
    PROVENANCE[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16]}
    return json.loads(raw)


def r(x, dp):
    """Round preserving None. Keeps JSON short and avoids float noise."""
    return None if x is None else round(x, dp)


def bitmask(bools):
    """Pack a short bool list into an int, bit i = element i."""
    n = 0
    for i, b in enumerate(bools):
        if b:
            n |= 1 << i
    return n


# ===========================================================================
# 1. geo  (INITIAL)
# ===========================================================================
def build_geo(geo, oews_areas):
    shapes = {s["abbr"] for s in geo["states"]}
    no_shape = [
        {"fips": a["fips"], "abbr": a["abbr"], "title": a["title"], "kind": a["kind"]}
        for a in oews_areas
        if a["abbr"] not in shapes
    ]
    out = {
        "meta": {
            "source": geo["source"],
            "projection": geo["projection"]["name"],
            "insets": geo["projection"]["insets"],
            "note": (
                "Alaska, Hawaii and Puerto Rico are inset by the Albers-USA projection and are "
                "NOT to scale with the lower 48; each carries inset='ak'|'hi'|'pr'."
            ),
            "areas_without_shape": no_shape,
            "areas_without_shape_note": (
                "OEWS publishes these state-equivalents but data/geo_states.json carries no "
                "polygon for them, so the map cannot draw them. Show them in a list, not the map."
            ),
        },
        "viewBox": geo["viewBox"],
        "width": geo["width"],
        "height": geo["height"],
        "states": [
            {
                "fips": s["fips"],
                "abbr": s["abbr"],
                "name": s["name"],
                "d": s["path"],
                "c": s["centroid"],
                "lp": s["label_pos"],
                "bb": s["bbox"],
                **({"inset": s["inset"]} if s.get("inset") else {}),
            }
            for s in geo["states"]
        ],
    }
    return emit("geo", out)


# ===========================================================================
# 2. occupation index (INITIAL) + 23 lazy state-series bundles
# ===========================================================================
def bundle_name(code, group):
    return "series_g00" if group in ("total", "major") else f"series_g{code[:2]}"


def build_occupations(st, nat, cw):
    years = st["years"]
    yidx = {y: i for i, y in enumerate(years)}
    areas = st["areas"]
    occ = st["occupations"]

    # ---- pivot the columnar state file into per-occupation sparse matrices ------
    cell = defaultdict(dict)  # occ_idx -> (area_idx, year_idx) -> (e, j, q, w)
    present = defaultdict(set)  # occ_idx -> {(area_idx, year_idx)} rows OEWS actually published
    wage_topcoded = defaultdict(set)
    wage_missing = defaultdict(set)
    emp_suppressed = defaultdict(set)

    for y in years:
        v = st["values"][str(y)]
        yi = yidx[y]
        o, s = v["o"], v["s"]
        te, jj, lq, am = v["tot_emp"], v["jobs_1000"], v["loc_quotient"], v["a_median"]
        tc_rows = set(v["flags"].get("a_median", {}).get("#", []))
        star_rows = set(v["flags"].get("a_median", {}).get("*", []))
        sup_rows = set(v["flags"].get("tot_emp", {}).get("**", []))
        for i in range(len(o)):
            oi, ai = o[i], s[i]
            cell[oi][(ai, yi)] = (te[i], jj[i], lq[i], am[i])
            present[oi].add((ai, yi))
            if i in tc_rows:
                wage_topcoded[oi].add((ai, yi))
            if i in star_rows:
                wage_missing[oi].add((ai, yi))
            if i in sup_rows:
                emp_suppressed[oi].add((ai, yi))

    # ---- the national file, keyed by (code, group) -----------------------------
    nat_occ = nat["occupations"]
    nat_key = {(o["code"], o["group"]): i for i, o in enumerate(nat_occ)}
    nat_cell = defaultdict(dict)  # nat_occ_idx -> year -> (emp, a_median)
    nat_tc, nat_star, nat_sup = defaultdict(set), defaultdict(set), defaultdict(set)
    for y in nat["years"]:
        v = nat["values"][str(y)]
        tc_rows = set(v["flags"].get("a_median", {}).get("#", []))
        star_rows = set(v["flags"].get("a_median", {}).get("*", []))
        sup_rows = set(v["flags"].get("tot_emp", {}).get("**", []))
        for i, oi in enumerate(v["o"]):
            nat_cell[oi][y] = (v["tot_emp"][i], v["a_median"][i])
            if i in tc_rows:
                nat_tc[oi].add(y)
            if i in star_rows:
                nat_star[oi].add(y)
            if i in sup_rows:
                nat_sup[oi].add(y)

    # ---- OOH crosswalk ---------------------------------------------------------
    slug_meta = {o["slug"]: o for o in cw["occupations"]}
    soc_to_ooh = cw["soc_to_ooh"]
    vintage_break = set()
    for o in cw["occupations"]:
        vintage_break.update(o.get("soc_vintage_break_2018") or [])

    # BLS's official SOC 2010 -> 2018 crosswalk, restricted to codes state OEWS really
    # published, so the UI can offer a predecessor/successor series that actually exists.
    state_codes = {o["code"] for o in occ}
    x2010 = cw["soc_vintage"]["crosswalk_2010_to_2018"]
    succ, pred = {}, defaultdict(set)
    for c10, c18s in x2010.items():
        keep = sorted(c for c in c18s if c in state_codes and c != c10)
        if keep and c10 in state_codes:
            succ[c10] = keep
        for c in c18s:
            if c10 in state_codes and c != c10:
                pred[c].add(c10)
    oews_only = {c["code"] for c in cw["oews_only_codes"]}
    # OOH occupations whose OEWS code set changes across years - the 15-1132 -> 15-1256
    # -> 15-1252 problem. These are exactly the series a naive 7-year line gets wrong.
    lineage = {}
    for o in cw["occupations"]:
        sets = {y: sorted(v) for y, v in o["oews"].items()}
        if len({tuple(v) for v in sets.values()}) > 1:
            lineage[o["slug"]] = {
                "t": o["title"],
                "cat": o["category"],
                "o": sets,
                **({"gaps": o["oews_gaps"]} if o.get("oews_gaps") else {}),
            }

    # ---- build the picker index + the per-occupation series records ------------
    major_titles = {o["code"][:2]: o["title"] for o in occ if o["group"] == "major"}
    index = []
    by_bundle = defaultdict(list)
    title_history = st.get("title_history", {})

    for oi, o in enumerate(occ):
        code, group = o["code"], o["group"]
        oyears = o["years"]
        ys = [yidx[y] for y in oyears]

        # national employment in the LATEST year this (code, group) exists
        ni = nat_key.get((code, group))
        n_emp = n_yr = None
        if ni is not None:
            for y in sorted(nat_cell[ni], reverse=True):
                if nat_cell[ni][y][0] is not None:
                    n_emp, n_yr = nat_cell[ni][y][0], y
                    break

        rec = {"c": code, "g": group, "t": o["title"], "mg": code[:2],
               "y": oyears, "b": bundle_name(code, group)}
        if n_emp is not None:
            rec["n"], rec["ny"] = n_emp, n_yr
        slugs = soc_to_ooh.get(code) or []
        if slugs:
            primary = next(
                (s for s in slugs if slug_meta.get(s, {}).get("csv_soc_code") == code), slugs[0]
            )
            rec["s"] = primary
            rec["cat"] = slug_meta[primary]["category"]
            if len(slugs) > 1:
                rec["so"] = [s for s in slugs if s != primary]
            if primary in lineage:
                rec["lin"] = 1
        if len(oyears) != len(years):
            rec["gap"] = 1
        if code in vintage_break:
            rec["vb"] = 1
        if code in title_history:
            rec["th"] = 1
        if code in succ:
            rec["post"] = succ[code]
        if pred.get(code):
            rec["pre"] = sorted(pred[code])
        if code in oews_only:
            rec["oo"] = 1
        index.append(rec)

        used = sorted({a for a, _ in present[oi]})
        c = cell[oi]
        blank = (None, None, None, None)
        series = {
            "c": code, "g": group, "t": o["title"], "y": oyears, "a": used,
            "p": [bitmask([(a, i) in present[oi] for i in ys]) for a in used],
            "e": [[c.get((a, i), blank)[0] for i in ys] for a in used],
            "j": [[c.get((a, i), blank)[1] for i in ys] for a in used],
            "q": [[c.get((a, i), blank)[2] for i in ys] for a in used],
            "w": [[c.get((a, i), blank)[3] for i in ys] for a in used],
        }
        pos = {a: k for k, a in enumerate(used)}
        for key, src in (("tc", wage_topcoded), ("wm", wage_missing), ("es", emp_suppressed)):
            lst = sorted([pos[a], ys.index(i)] for a, i in src[oi] if a in pos)
            if lst:
                series[key] = lst
        if ni is not None:
            uy = nat_occ[ni]["years"]
            us = {
                "y": uy,
                "e": [nat_cell[ni].get(y, (None, None))[0] for y in uy],
                "w": [nat_cell[ni].get(y, (None, None))[1] for y in uy],
                "p": bitmask([y in nat_cell[ni] for y in uy]),
            }
            for key, src in (("tc", nat_tc), ("wm", nat_star), ("es", nat_sup)):
                lst = sorted(uy.index(y) for y in src[ni] if y in uy)
                if lst:
                    us[key] = lst
            series["us"] = us
        if code in title_history:
            series["th"] = title_history[code]
        by_bundle[rec["b"]].append(series)

    topcode = {str(y): (208000 if y <= 2021 else 239200) for y in years}

    idx_out = {
        "meta": {
            "source": "data/oews_state.json + data/oews_national.json + data/soc_crosswalk.json",
            "level": "OEWS state, May reference period",
            "counts": {
                "occupations": len(index),
                "with_ooh_slug": sum(1 for x in index if "s" in x),
                "not_in_every_year": sum(1 for x in index if x.get("gap")),
                "may_2018_vintage_break": sum(1 for x in index if x.get("vb")),
                "title_changed": sum(1 for x in index if x.get("th")),
                "bundles": len(by_bundle),
            },
            "fields": {
                "c": "OEWS occupation code",
                "g": "o_group: total | major | detailed (state OEWS publishes only these three)",
                "t": "title as published in the most recent year the code appears",
                "mg": "SOC major group (first two digits)",
                "y": "exactly the years this (code, group) was published - NOT always all 7",
                "b": "name of the lazy bundle carrying this occupation's state series",
                "n": "national employment in year `ny`, the latest year the code exists "
                     "(the picker's sort-by-size key)",
                "s": "OOH slug - bls.gov/ooh/<cat>/<s>.htm, and the key the existing treemap uses",
                "cat": "OOH category slug",
                "so": "other OOH slugs sharing this SOC code",
                "lin": "1 = this occupation's OOH lineage uses DIFFERENT OEWS codes in different "
                       "years; look the slug up in `lineage` before drawing a 7-year line",
                "gap": "1 = the code is missing from at least one year of 2018-2024. A line chart "
                       "must break, not slope.",
                "vb": "1 = BLS's own 2010->2018 SOC crosswalk says the May-2018 value is not "
                      "comparable with 2019+",
                "th": "1 = the published title changed across years; the series bundle carries a "
                      "`th` map of the per-year titles",
                "pre": "SOC 2010 codes that BLS's official 2010->2018 crosswalk maps INTO this "
                       "code, filtered to codes state OEWS actually published. This is the "
                       "predecessor series to offer when a line starts mid-window: 15-1252 "
                       "carries pre=['15-1132','15-1133'].",
                "post": "the reverse: SOC 2018 codes this (2010-vintage) code maps to, filtered "
                        "the same way. Offer these as the continuation of a line that stops.",
                "oo": "1 = an OEWS-only publication code, present in NEITHER SOC vintage "
                      "(15-1256 and 15-1257, the 2019-2020 combined computer codes). The BLS "
                      "crosswalk has no entry for it, so no pre/post is asserted; say so rather "
                      "than guessing a lineage.",
            },
            "caveats": [
                "The index key is (code, group), NOT code alone.",
                "Software Developers is 15-1132 (2018) -> 15-1256, combined with what later "
                "becomes 15-1253, (2019-2020) -> 15-1252 (2021+). Three separate index entries "
                "with disjoint `y` lists. The absence of 15-1252 before 2021 is a code change, "
                "not a collapse in employment. `pre`/`post` give the official crosswalk hop "
                "(15-1252.pre = 15-1132, 15-1133); 15-1256 is an OEWS-only combined code with "
                "no crosswalk entry at all and is flagged `oo`, so the 2019-2020 hop must be "
                "labelled, not silently bridged.",
                "May 2020 is a COVID-affected reference period: national employment falls 5.3% "
                "from 2019. Do not read 2019->2020->2021 as structural.",
                "State-level OEWS publishes no broad or minor groups, so `g` is only "
                "total/major/detailed. Summing detailed rows to a broad group is the caller's job.",
                "State and national totals differ slightly (40 to 6,130 out of ~150M) because BLS "
                "rounds every estimate independently. The state sum is not the national figure.",
            ],
            "soc_vintage": st["meta"]["soc_vintage"],
            "covid_years": st["meta"]["covid_years"],
            "flag_legend": st["meta"]["flag_legend"],
            "topcode_note": "topcode_annual gives the BLS annual wage top code per year. A wage "
                            "flagged '#' is AT OR ABOVE that figure - a censored HIGH value, not "
                            "missing data. Never draw it the same as '*' or '**'.",
        },
        "years": years,
        "areas": [
            {"fips": a["fips"], "abbr": a["abbr"], "name": a["title"], "kind": a["kind"]}
            for a in areas
        ],
        "majors": [{"mg": k, "t": v} for k, v in sorted(major_titles.items())],
        "topcode_annual": topcode,
        "occupations": index,
        "lineage": lineage,
    }
    emit("occ_index", idx_out)

    series_schema = {
        "y": "years this occupation was published; EVERY value array below is aligned to this list",
        "a": "area indices into occ_index.areas that have at least one published row",
        "p": "per-area bitmask over `y`: bit i set = OEWS published a row for that area-year. A "
             "null value where the bit is SET is suppressed or unavailable; a null where the bit "
             "is CLEAR means the occupation was simply not published there that year.",
        "e": "tot_emp [area][year] - employment, integer",
        "j": "jobs_1000 [area][year] - jobs per 1,000 jobs in that state, 3 dp, as published",
        "q": "loc_quotient [area][year] - location quotient vs the nation, 2 dp, as published",
        "w": "a_median [area][year] - annual median wage USD, integer",
        "tc": "[[area_pos, year_pos]] wage flagged '#': AT OR ABOVE occ_index.topcode_annual for "
              "that year. A CENSORED HIGH value, not missing data.",
        "wm": "[[area_pos, year_pos]] wage flagged '*': wage estimate not available. Missing.",
        "es": "[[area_pos, year_pos]] employment flagged '**': estimate not released. e, j and q "
              "are all null together on these rows.",
        "us": "the national row from data/oews_national.json, on its OWN year axis `y` (the "
              "national file can publish a code in years the state files do not). e = employment, "
              "w = annual median. BLS publishes no jobs_1000 / loc_quotient nationally, so "
              "neither is present. tc/wm/es are year positions, not [area, year] pairs.",
        "th": "per-year published titles, present only where the title changed",
    }
    for name, recs in sorted(by_bundle.items()):
        recs.sort(key=lambda x: (x["c"], x["g"]))
        emit(name, {
            "meta": {
                "bundle": name,
                "source": "data/oews_state.json + data/oews_national.json",
                "major_group": None if name == "series_g00" else name[-2:],
                "n_occupations": len(recs),
                "schema": series_schema,
                "note": "series_g00 carries the 00-0000 total and the 22 major groups; every "
                        "other bundle carries the detailed occupations of one SOC major group.",
            },
            "years": years,
            "occ": recs,
        })
    return index, by_bundle


# ===========================================================================
# 3. metro (lazy)
# ===========================================================================
def build_metro(m):
    years = m["years"]
    occs = m["occupations"]
    metros = m["metros"]
    nat = m["national"]
    nat_pos = {o: i for i, o in enumerate(nat["o"])}
    total_i = next(i for i, o in enumerate(occs) if o["c"] == "00-0000")
    nat_total = nat["e"][nat_pos[total_i]] if total_i in nat_pos else [None] * len(years)

    # per-metro total employment: the LQ denominator and the bubble-size baseline
    metro_total = []
    for s in m["series"]:
        pos = {o: k for k, o in enumerate(s["o"])}
        metro_total.append(s["e"][pos[total_i]] if total_i in pos else [None] * len(years))

    # national share of each occupation per year - the other half of the LQ
    nat_share = {}
    for oi in range(len(occs)):
        k = nat_pos.get(oi)
        nat_share[oi] = [
            (nat["e"][k][yi] / nat_total[yi])
            if (k is not None and nat["e"][k][yi] is not None and nat_total[yi])
            else None
            for yi in range(len(years))
        ]

    def shard_of(o):
        return "metro_g00" if o["g"] in ("total", "major") else f"metro_g{o['c'][:2]}"

    it_codes = sorted(o["c"] for o in occs if o.get("it"))
    index = {
        "meta": {
            "source": "data/oews_metro.json",
            "level": "OEWS metropolitan / NECTA areas, May reference period",
            "counts": {"metros": len(metros), "occupations": len(occs),
                       "it_occupations": len(it_codes)},
            "coverage": m["meta"]["counts"],
            "metro_rule": m["meta"]["metro_rule"],
            "occ_rule": m["meta"]["occ_rule"],
            "soc_warning": m["meta"]["soc_warning"],
            "topcode": m["meta"]["topcode"],
            "it_codes": it_codes,
            "caveats": [
                "219 metros = 79.1% of 2024 US employment, not all of it. The bubbles are a "
                "sample of metros and never sum to a national total.",
                "The 2024 OMB re-delineation retired every NECTA and reissued codes: 204 of 219 "
                "metros have 2024 data, 203 have 2018-2023 data. `x` links the two halves of a "
                "re-delineated place; they are deliberately NOT spliced, because the county sets "
                "differ and the level shift across the join is real.",
                "Only 241 of the metro-published occupation codes are carried, for size - but ALL "
                "40 15-xxxx computer occupations are complete (see it_codes).",
                "q (location quotient) is DERIVED here, not published in data/oews_metro.json: "
                "q = (metro emp in occ / metro emp in 00-0000) / (national emp in occ / national "
                "emp in 00-0000), computed from this same file's own national block and rounded "
                "to 2 dp. It is null whenever any input is null.",
                "A '#' on a metro MEAN wage carries no value bound at all (see topcode.mean_note "
                "upstream); only the median is carried here, and only its '#' cells are in tc.",
            ],
        },
        "years": years,
        "metros": [
            {"a": x["a"], "t": x["t"], "s": x["s"], "ps": x["ps"], "lat": x["lat"], "lon": x["lon"],
             **({"x": x["x"]} if x.get("x") else {}),
             **({"yt": x["yt"]} if x.get("yt") else {})}
            for x in metros
        ],
        "metro_total_emp": metro_total,
        "national_total_emp": nat_total,
        "occ": [
            {"c": o["c"], "t": o["t"], "g": o["g"], "mg": o["c"][:2],
             "y": [years[i] for i, p in enumerate(o["y"]) if p],
             "it": o.get("it", 0), "b": shard_of(o)}
            for o in occs
        ],
    }
    emit("metro_index", index)

    # ---- invert metro-major into occupation-major, then shard by SOC major -----
    per_occ = defaultdict(lambda: {"e": {}, "w": {}, "tc": set()})
    for mi, s in enumerate(m["series"]):
        tc = {(row, yi) for row, yi, metric in s.get("tc", []) if metric == 0}
        for k, oi in enumerate(s["o"]):
            po = per_occ[oi]
            po["e"][mi] = s["e"][k]
            po["w"][mi] = s["m"][k]
            for yi in range(len(years)):
                if (k, yi) in tc:
                    po["tc"].add((mi, yi))

    nat_tc = {(row, yi) for row, yi, metric in nat.get("tc", []) if metric == 0}
    shards = defaultdict(list)
    for oi, o in enumerate(occs):
        po = per_occ[oi]
        used = sorted(po["e"])
        pos = {mi: k for k, mi in enumerate(used)}
        rec = {
            "c": o["c"], "g": o["g"], "t": o["t"], "m": used,
            "e": [po["e"][mi] for mi in used],
            "w": [po["w"][mi] for mi in used],
            "q": [
                [
                    r(po["e"][mi][yi] / metro_total[mi][yi] / nat_share[oi][yi], 2)
                    if (po["e"][mi][yi] is not None and metro_total[mi][yi] and nat_share[oi][yi])
                    else None
                    for yi in range(len(years))
                ]
                for mi in used
            ],
        }
        tc = sorted([pos[mi], yi] for mi, yi in po["tc"] if mi in pos)
        if tc:
            rec["tc"] = tc
        k = nat_pos.get(oi)
        if k is not None:
            us = {"e": nat["e"][k], "w": nat["m"][k]}
            ntc = sorted(yi for row, yi in nat_tc if row == k)
            if ntc:
                us["tc"] = ntc
            rec["us"] = us
        shards[shard_of(o)].append(rec)

    schema = {
        "m": "metro indices into metro_index.metros",
        "e": "employment [metro][year], aligned to metro_index.years - 7 slots, null where "
             "OEWS published nothing for that metro-occupation-year",
        "w": "annual median wage USD [metro][year]",
        "q": "DERIVED location quotient [metro][year], 2 dp - see metro_index.meta.caveats",
        "tc": "[[metro_pos, year_pos]] median wage flagged '#': at or above "
              "metro_index.meta.topcode.annual[year_pos]. Censored high, not missing.",
        "us": "the national row for this occupation: e, w, and tc as bare year indices",
    }
    for name, recs in sorted(shards.items()):
        recs.sort(key=lambda x: (x["c"], x["g"]))
        emit(name, {
            "meta": {"shard": name, "source": "data/oews_metro.json",
                     "n_occupations": len(recs), "schema": schema},
            "years": years,
            "occ": recs,
        })
    return shards


# ===========================================================================
# 4. remote (lazy)
# ===========================================================================
def build_remote(acs_state, acs_occ, ind):
    years = acs_state["years"]
    missing = sorted(int(y) for y in acs_state["meta"]["years_missing"])

    geos = [{"c": g["code"], "n": g["name"], "k": g["kind"], "fips": g["fips"]}
            for g in acs_state["geos"]]
    wi = acs_state["measures"].index("wfh")
    ti = acs_state["measures"].index("total")

    state = {}
    for g in geos:
        code = g["c"]
        e, mm = acs_state["e"][code], acs_state["m"][code]
        state[code] = {
            "share": acs_state["share"][code],
            "moe": acs_state["share_moe"][code],
            "wfh": [None if y is None else y[wi] for y in e],
            "wfh_moe": [None if y is None else y[wi] for y in mm],
            "total": [None if y is None else y[ti] for y in e],
        }

    by_occ = {
        g["c"]: {
            "share": acs_occ["share"][g["c"]],
            "moe": acs_occ["share_moe"][g["c"]],
            "wfh": acs_occ["wfh_e"][g["c"]],
            "total": acs_occ["total_e"][g["c"]],
            "all": acs_occ["all_occupations"][g["c"]],
        }
        for g in geos
    }

    # ---- Indeed ---------------------------------------------------------------
    months = ind["months"]
    sectors = ind["sectors"]

    def last_month(arr):
        for i in range(len(arr) - 1, -1, -1):
            if arr[i] is not None:
                return months[i]
        return None

    sector_series, sector_last = {}, {}
    for cc, rows in ind["sector_monthly"].items():
        keep, last = {}, {}
        for i, s in enumerate(sectors):
            arr = rows[i]
            if arr is None:
                continue
            keep[s["code"]] = arr
            last[s["code"]] = last_month(arr)
        sector_series[cc] = keep
        sector_last[cc] = last

    country_last = {
        k: {c: last_month(v) for c, v in d.items() if v is not None}
        for k, d in ind["monthly"].items()
    }

    out = {
        "meta": {
            "sources": {
                "acs_state": "data/acs_wfh_state.json - ACS 1-year, table B08006",
                "acs_occupation": "data/acs_wfh_occupation.json - ACS 1-year, table B08124",
                "indeed": "data/indeed_remote.json - Indeed Hiring Lab remote-tracker + ai-tracker",
            },
            "acs": {
                "years": years,
                "years_missing": acs_state["meta"]["years_missing"],
                "gap_rule": "There is NO 2020 ACS 1-year file - the Census Bureau never released "
                            "one. Every 2020 slot is null. Render a visible gap; the 2019 -> 2021 "
                            "step spans two survey years and must never be interpolated.",
                "definition": "ACS 'worked from home' is a means-of-transportation-to-work "
                              "category: the respondent did not commute during the reference week "
                              "because they worked at home. A PRIMARY-mode measure of workers 16+ "
                              "by place of RESIDENCE, so it undercounts hybrid arrangements.",
                "denominator": "all workers 16 and over in that geography (measure 'total')",
                "moe": "90% margin of error. `moe` on a share is DERIVED with the ACS "
                       "derived-proportion formula and is in SHARE units - multiply by 100 for "
                       "percentage points.",
                "series_break": "2018-2019 come from the legacy sequence-based summary file, "
                                "2021-2024 from the table-based summary file. The B08006/B08124 "
                                "line structure was verified identical across vintages, so the "
                                "numbers are comparable, but the products differ.",
                "pr_note": "Puerto Rico is a state-equivalent here but is excluded from ACS "
                           "national controls: the US row equals the 50 states + DC exactly. "
                           "Never add PR into a US total.",
                "military_note": "'Military specific occupations' is a real B08124 group and is "
                                 "kept as published. Its counts are tiny, its share noisy, and its "
                                 "MOE frequently exceeds the estimate.",
            },
            "indeed": {
                "unit": "percent of postings (or of searches), 0-100. Month mean of an upstream "
                        "7-day trailing average - this pipeline adds no smoothing of its own.",
                "span": [months[0], months[-1]],
                "partial_months": ind["partial_months"],
                "remote_definition": ind["meta"]["methodology"]["remote_definition"],
                "remote_searches_definition": ind["meta"]["methodology"]["remote_searches_definition"],
                "ai_definition": ind["meta"]["methodology"]["ai_definition"],
                "methodology_change": ind["meta"]["methodology"]["METHODOLOGY_CHANGE"],
                "country_coverage_note": "Country coverage differs per series: remote postings 7 "
                                         "countries, remote searches 8 (adds JP), AI postings 9 "
                                         "(adds IT and NL, not JP). A country absent from a series "
                                         "is null there - draw nothing, not zero. "
                                         "series_last_month gives each present series' last month.",
                "sector_end_dates": "The brief expected the per-SECTOR remote series to stop at "
                                    "2023-05-26. It does NOT in this retrieval: "
                                    "remote_postings_sector.csv runs the full 2019-01..2026-07 "
                                    "span for every (country, sector) pair present. "
                                    "`sector_last_month` carries each series' true last month, "
                                    "measured from the data itself - draw every line only to its "
                                    "own last month, whatever a future retrieval brings.",
                "sector_taxonomy": "Indeed sectors are built on normalised job titles - NOT SOC "
                                   "and NOT NAICS. They cannot be joined to the OEWS/OOH "
                                   "occupations used elsewhere on this page. 8 of the 53 labels "
                                   "are INFERRED (label_source='inferred') and must not be "
                                   "presented as official Indeed labels.",
                "comparability": "Indeed measures EMPLOYER postings that MENTION remote or hybrid "
                                 "work (it lumps hybrid in with fully remote); ACS measures "
                                 "WORKERS whose primary commute mode was working at home. "
                                 "Different quantities - do not put them on one axis.",
            },
        },
        "acs": {
            "years": years,
            "years_missing": missing,
            "geos": geos,
            "state": state,
            "occupations": acs_occ["occupations"],
            "by_occupation": by_occ,
        },
        "indeed": {
            "months": months,
            "month_days": ind["month_days"],
            "partial_months": ind["partial_months"],
            "countries": [{"c": c, "n": n} for c, n in
                          zip(ind["countries"]["codes"], ind["countries"]["names"])],
            "series": ind["monthly"],
            "series_last_month": country_last,
            "sectors": sectors,
            "sector_series": sector_series,
            "sector_last_month": sector_last,
        },
    }
    return emit("remote", out)


# ===========================================================================
# 5. tech (lazy) - HN trends + derived "answer" signals
# ===========================================================================
MIN_SUPPORT = 40  # postings, in whichever endpoint year carries the claim


def two_prop(c0, n0, c1, n1):
    """Share change with a 95% normal-approximation interval on the difference."""
    if not n0 or not n1:
        return None
    p0, p1 = c0 / n0, c1 / n1
    se = math.sqrt(p0 * (1 - p0) / n0 + p1 * (1 - p1) / n1)
    d = p1 - p0
    lo, hi = d - 1.96 * se, d + 1.96 * se
    return {"p0": round(p0, 5), "p1": round(p1, 5), "d": round(d, 5),
            "lo": round(lo, 5), "hi": round(hi, 5), "sig": bool(lo > 0 or hi < 0)}


def build_tech(hn, tax):
    months, years = hn["months"], hn["years"]
    totals_y = hn["totals"]["year"]

    per_year_months = Counter(mth[:4] for mth in months)
    partial = [y for y in years if per_year_months[y] < 12]
    full = [y for y in years if y not in partial]
    base_y, late_y, remote_base_y = "2018", full[-1], "2019"
    bi, li, rbi = years.index(base_y), years.index(late_y), years.index(remote_base_y)

    tech, role = hn["tech"], hn["role"]

    # ---- technologies: share change, plus remote share OF that technology ------
    rows = []
    for tid in tech["ids"]:
        c0, c1 = tech["year_counts"][tid][bi], tech["year_counts"][tid][li]
        if max(c0, c1) < MIN_SUPPORT:
            continue
        s = two_prop(c0, totals_y[bi], c1, totals_y[li])
        if s is None:
            continue
        r0, r1 = tech["year_counts_remote"][tid][rbi], tech["year_counts_remote"][tid][li]
        cr0 = tech["year_counts"][tid][rbi]
        rows.append({
            "id": tid, "label": tech["labels"][tid], "cat": tech["categories"][tid],
            "n0": c0, "n1": c1,
            "share0": s["p0"], "share1": s["p1"], "d": s["d"],
            "ci95": [s["lo"], s["hi"]], "sig": s["sig"],
            "rem0": r(r0 / cr0, 5) if cr0 else None,
            "rem1": r(r1 / c1, 5) if c1 else None,
            "rem_n0": r0, "rem_d0": cr0, "rem_n1": r1, "rem_d1": c1,
        })
    rows.sort(key=lambda x: -x["d"])
    risers = [x for x in rows if x["d"] > 0][:20]
    fallers = [x for x in rows if x["d"] < 0][-20:][::-1]

    # ---- roles: share now, trend, remote share now vs the pre-pandemic base ----
    role_rows = []
    for rid in role["ids"]:
        c0, c1 = role["year_counts"][rid][bi], role["year_counts"][rid][li]
        br = role["year_counts_by_remote"][rid]

        def rshare(yi, keys):
            n = role["year_counts"][rid][yi]
            if not n:
                return None, 0
            k = sum(br[x][yi] for x in keys)
            return round(k / n, 5), k

        rem0, rem_n0 = rshare(rbi, ["remote"])
        rem1, rem_n1 = rshare(li, ["remote"])
        hyb0, _ = rshare(rbi, ["remote", "hybrid"])
        hyb1, _ = rshare(li, ["remote", "hybrid"])
        s = two_prop(c0, totals_y[bi], c1, totals_y[li])
        role_rows.append({
            "id": rid, "label": role["labels"][rid], "n0": c0, "n1": c1,
            "share0": s["p0"] if s else None, "share1": s["p1"] if s else None,
            "d": s["d"] if s else None,
            "ci95": [s["lo"], s["hi"]] if s else None, "sig": s["sig"] if s else None,
            "rem0": rem0, "rem1": rem1, "rem_n0": rem_n0, "rem_n1": rem_n1,
            "rem_d0": role["year_counts"][rid][rbi], "rem_d1": c1,
            "remhyb0": hyb0, "remhyb1": hyb1,
            "d_rem": r(rem1 - rem0, 5) if (rem1 is not None and rem0 is not None) else None,
            "low_support": bool(max(c0, c1) < MIN_SUPPORT),
        })
    role_rows.sort(key=lambda x: -(x["share1"] or 0))

    signals = {
        "definitions": {
            "universe": "Top-level comments on the monthly 'Ask HN: Who is hiring?' thread, "
                        "2011-04 to the latest thread. YC-adjacent startups, NOT the US labour "
                        "market.",
            "base_year": base_y,
            "latest_full_year": late_y,
            "remote_base_year": remote_base_y,
            "partial_years": partial,
            "share": f"share of ALL postings in that year - denominator = signals.totals.postings. "
                     f"A posting counts once per technology however many times it names it.",
            "remote_share_of_tech": "rem = remote postings mentioning the technology / ALL "
                                    "postings mentioning it, that year (rem_n / rem_d). 'remote' "
                                    "is the strict fully-remote class only: the HN technology "
                                    "series carries no hybrid breakdown.",
            "remote_share_of_role": "rem = fully-remote postings in that role / all postings in "
                                    "that role; remhyb additionally counts the hybrid class. "
                                    "Denominator is the role's own posting count, not all "
                                    "postings.",
            "min_support": f"a technology is reported only when it has at least {MIN_SUPPORT} "
                           f"postings in {base_y} or in {late_y}. Raw counts (n0, n1) ship with "
                           f"every row so a 2 -> 6 move cannot masquerade as a trend. "
                           f"Roles are never dropped, but low_support marks the thin ones.",
            "ci95": "95% normal-approximation interval on the share DIFFERENCE of two independent "
                    "proportions. HN is a convenience sample, not a random one, so read this as a "
                    "noise floor, not a confidence statement about the labour market.",
            "counts_warning": "Thread volume fell from 10,259 postings in 2021 to 2,644 in 2026 "
                              "(partial). Counts are NOT a hiring indicator; only shares are.",
        },
        "totals": {"years": years, "postings": totals_y,
                   "months_observed": [per_year_months[y] for y in years]},
        "min_support": MIN_SUPPORT,
        "tech_considered": len(rows),
        "tech_excluded_low_support": len(tech["ids"]) - len(rows),
        "risers": risers,
        "fallers": fallers,
        "tech_all": rows,
        "roles": role_rows,
        "remote_class_share": hn["remote"]["year_shares"],
    }

    out = {
        "meta": {
            "source": "data/hn_trends.json + data/hn_taxonomy.json",
            "universe": signals["definitions"]["universe"],
            "bias": [
                "Hacker News 'Who is hiring?' is a BIASED sample: YC-adjacent startups and "
                "remote-friendly software companies, not the US labour market. Nothing here "
                "generalises to a BLS total.",
                "Thread volume is a popularity signal, not a hiring signal: 10,259 postings in "
                "2021 against 2,644 in 2026. Use shares; never plot raw counts as demand.",
                "PARTIAL years must be marked wherever they appear - see partial_years and "
                "months_per_year. 2011 starts at the first thread (2011-04) and the latest year "
                "runs only to the newest thread, whose own month is still live.",
                "Technology and role shares are share-of-postings, not share-of-mentions.",
                "445 comments could not be confirmed as job ads and were dropped, so per-year "
                "posting counts are a floor, tightest in 2011 and the latest year.",
                "The remote classifier scores 92.9% on a blind held-out set. 'unknown' is kept as "
                "its own class and is never folded into onsite.",
            ],
            "classifier": hn["meta"]["remote_classifier_accuracy"],
            "dropped_unconfirmed": hn["meta"]["postings_dropped_unconfirmed"],
            "dropped_unconfirmed_by_year": hn["meta"]["postings_dropped_unconfirmed_by_year"],
            "taxonomy_notes": tax["_notes"],
            "schema": {
                "shares": "shares are NOT shipped as separate arrays: divide any *_counts array "
                          "by the matching totals array. count/total reproduces the share the "
                          "source file publishes exactly. A zero denominator means null, not 0.",
                "remote_totals": "postings in the strict 'remote' class per month / per year - "
                                 "the denominator for tech.year_counts_remote read as 'share of "
                                 "remote postings', and NOT the denominator for 'remote share of "
                                 "postings mentioning X' (that one is tech.year_counts).",
            },
        },
        "months": months,
        "years": years,
        "partial_years": partial,
        "months_per_year": {y: per_year_months[y] for y in years},
        "totals": hn["totals"],
        "remote": {"month_counts": hn["remote"]["month_counts"],
                   "year_counts": hn["remote"]["year_counts"]},
        "remote_totals": hn["remote_totals"],
        "remote_scope": {"year_counts": hn["remote_scope"]["year_counts"]},
        "seniority": hn["seniority"],
        "tech": {
            "ids": tech["ids"],
            "labels": tech["labels"],
            "categories": tech["categories"],
            "month_counts": tech["month_counts"],
            "year_counts": tech["year_counts"],
            "year_counts_remote": tech["year_counts_remote"],
        },
        "role": {
            "ids": role["ids"],
            "labels": role["labels"],
            "month_counts": role["month_counts"],
            "year_counts": role["year_counts"],
            "year_counts_by_remote": role["year_counts_by_remote"],
        },
        "salary_usd_median": hn["salary_usd_median"],
        "visa": hn["visa"],
        "signals": signals,
    }
    emit("tech", out)
    return signals


# ===========================================================================
# report
# ===========================================================================
def print_signals(sig):
    d = sig["definitions"]
    b, la = d["base_year"], d["latest_full_year"]
    tot = dict(zip(sig["totals"]["years"], sig["totals"]["postings"]))
    n_all = sig["tech_considered"] + sig["tech_excluded_low_support"]
    print()
    print("=" * 104)
    print(f"HN 'Who is hiring?' SIGNALS   base {b} (n={tot[b]:,} postings)"
          f"  ->  latest FULL year {la} (n={tot[la]:,})")
    print(f"  min support: >= {sig['min_support']} postings in one endpoint year. "
          f"{sig['tech_considered']} of {n_all} technologies qualify "
          f"({sig['tech_excluded_low_support']} excluded as too thin).")
    print(f"  PARTIAL years, never compared on level: {', '.join(d['partial_years'])}")
    print("=" * 104)

    def tbl(rows, title):
        print(f"\n{title}")
        print(f"  {'technology':<24}{'cat':<10}{b + ' shr':>9}{'n':>7}{la + ' shr':>9}{'n':>7}"
              f"{'change':>10}{'95% CI (pp)':>19}{'rem% ' + la:>10}")
        for x in rows:
            ci = f"[{x['ci95'][0] * 100:+.2f},{x['ci95'][1] * 100:+.2f}]"
            rem = f"{x['rem1'] * 100:6.1f}%" if x["rem1"] is not None else "     -"
            ns = "" if x["sig"] else "  ns"
            print(f"  {x['label'][:23]:<24}{x['cat']:<10}{x['share0'] * 100:8.2f}%{x['n0']:7,}"
                  f"{x['share1'] * 100:8.2f}%{x['n1']:7,}{x['d'] * 100:+9.2f}pp{ci:>19}"
                  f"{rem:>10}{ns}")

    tbl(sig["risers"][:15],
        f"TOP RISERS   share of all postings, {b} -> {la}   (rem% = remote share OF postings "
        f"mentioning it, {la})")
    tbl(sig["fallers"][:15], f"TOP FALLERS  share of all postings, {b} -> {la}")

    rb = d["remote_base_year"]
    print(f"\nROLES   share of all postings; rem = fully-remote share OF that role "
          f"(denominator = the role's own postings)")
    print(f"  {'role':<26}{la + ' shr':>9}{'n':>7}{'vs ' + b:>11}"
          f"{'rem ' + rb:>9}{'rem ' + la:>9}{'change':>10}{'+hyb ' + la:>10}")
    for x in sig["roles"]:
        if x["share1"] is None:
            continue
        def f(v):
            return "        -" if v is None else f"{v * 100:8.1f}%"
        flag = "  low-n" if x["low_support"] else ""
        dr = x["d_rem"] * 100 if x["d_rem"] is not None else 0.0
        print(f"  {x['label'][:25]:<26}{x['share1'] * 100:8.2f}%{x['n1']:7,}"
              f"{x['d'] * 100:+10.2f}pp{f(x['rem0'])}{f(x['rem1'])}{dr:+9.1f}pp"
              f"{f(x['remhyb1'])}{flag}")

    print("\nREMOTE CLASS SHARE of all postings, by year "
          "(denominator = every posting that year, unknown kept as its own class)")
    ys = sig["totals"]["years"]
    print("  year   " + "".join(f"{y:>8}" for y in ys))
    for k in ("remote", "hybrid", "onsite", "unknown"):
        v = sig["remote_class_share"][k]
        print(f"  {k:<7}" + "".join("       -" if x is None else f"{x * 100:7.1f}%" for x in v))
    print("  n      " + "".join(f"{n:>8,}" for n in sig["totals"]["postings"]))
    print("  partial: " + ", ".join(d["partial_years"]))
    print()


def main():
    print("reading data/ ...")
    geo = load("geo_states.json")
    st = load("oews_state.json")
    nat = load("oews_national.json")
    cw = load("soc_crosswalk.json")
    metro = load("oews_metro.json")
    acs_state = load("acs_wfh_state.json")
    acs_occ = load("acs_wfh_occupation.json")
    ind = load("indeed_remote.json")
    hn = load("hn_trends.json")
    tax = load("hn_taxonomy.json")

    build_geo(geo, st["areas"])
    build_occupations(st, nat, cw)
    build_metro(metro)
    build_remote(acs_state, acs_occ, ind)
    signals = build_tech(hn, tax)

    manifest = {
        "meta": {
            "title": "site/explore payload manifest",
            "built_by": "pipeline/build_explore_data.py",
            "served_from": "site/ as document root; App.load(name) fetches explore/data/<name>.json",
            "idempotent": "no timestamp is written. Provenance is the sha256 prefix and byte size "
                          "of each data/ input, so unchanged inputs give byte-identical output.",
            "initial_load": INITIAL,
            "budget_mb": BUDGET_MB,
            "lazy_note": "everything not in initial_load is fetched on demand. series_g<NN> and "
                         "metro_g<NN> are keyed by SOC major group; occ_index.occupations[].b and "
                         "metro_index.occ[].b name the bundle to fetch for a given occupation.",
            "provenance": PROVENANCE,
        },
        "payloads": {},
    }
    emit("manifest", manifest)  # once, to reserve a size
    manifest["payloads"] = {k: WRITTEN[k] for k in sorted(WRITTEN)}
    emit("manifest", manifest)  # again, now that every size is known
    manifest["payloads"] = {k: WRITTEN[k] for k in sorted(WRITTEN)}
    emit("manifest", manifest)  # fixed point: manifest size is stable after one restatement

    print_signals(signals)

    print("=" * 104)
    print(f"{'payload':<22}{'bytes':>12}{'KB':>10}  load")
    print("-" * 104)
    initial_total = 0
    over = []
    for name in sorted(WRITTEN):
        b = WRITTEN[name]
        is_init = name in INITIAL
        if is_init:
            initial_total += b
        elif b > LAZY_SOFT_LIMIT_MB * 1024 * 1024:
            over.append((name, b))
        print(f"{name:<22}{b:>12,}{b / 1024:>10,.0f}  {'INITIAL' if is_init else 'lazy'}")
    print("-" * 104)
    total = sum(WRITTEN.values())
    limit = int(BUDGET_MB * 1024 * 1024)
    print(f"{'ALL PAYLOADS':<22}{total:>12,}{total / 1024:>10,.0f}  ({len(WRITTEN)} files)")
    print(f"{'INITIAL LOAD':<22}{initial_total:>12,}{initial_total / 1024:>10,.0f}  "
          f"budget {BUDGET_MB} MB = {limit:,} bytes")
    head = max((b for n, b in WRITTEN.items() if n not in INITIAL), default=0)
    hname = max(((b, n) for n, b in WRITTEN.items() if n not in INITIAL), default=(0, "-"))[1]
    print(f"{'largest lazy payload':<22}{head:>12,}{head / 1024:>10,.0f}  {hname}")
    for n, b in over:
        print(f"  WARNING: lazy payload {n} is {b / 1024 / 1024:.2f} MB "
              f"(soft limit {LAZY_SOFT_LIMIT_MB} MB)")
    if initial_total > limit:
        print()
        raise SystemExit(
            f"BUDGET EXCEEDED: initial load is {initial_total / 1024 / 1024:.2f} MB, limit "
            f"{BUDGET_MB} MB. Initial payloads: {INITIAL}"
        )
    print(f"OK: initial load {initial_total / 1024 / 1024:.2f} MB is within the {BUDGET_MB} MB "
          f"budget ({100 * initial_total / limit:.0f}% used).")


if __name__ == "__main__":
    main()
