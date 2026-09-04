"""
Build a Kaggle-ready release of the US remote-work / tech-hiring data.

Reads the normalized files in data/ and writes, into kaggle/:
  * one tidy CSV per table (Kaggle previews and profiles these natively)
  * us_remote_work.sqlite with the same tables, typed, indexed, plus views
  * dataset-metadata.json for `kaggle datasets create -p kaggle/`
  * README.md - the dataset description and full data dictionary

Every table is generated from data/, never hand-edited, so the release can be
rebuilt from scratch:

    uv run python pipeline/build_kaggle_dataset.py

Design rules, which the data dictionary repeats for users:
  - Long/tidy over wide. Many-to-many relations get their own bridge table.
  - Missing is empty in CSV and NULL in SQLite - never 0, never an empty string
    standing in for a real value.
  - BLS suppression markers travel in their own *_flag column so a censored high
    wage ('#') is never confused with an unavailable one ('*' / '**').
  - Partial periods are flagged, never silently compared against full ones.
"""

import csv
import json
import os
import sqlite3

from common import DATA, ROOT, STATES, read_json

OUT = ROOT / "kaggle"
OUT.mkdir(parents=True, exist_ok=True)

# Populated by write_table(); drives the SQLite build and the data dictionary.
TABLES = {}


def write_table(name, header, rows, description, keys=None):
    """Write one CSV and remember it for the SQLite build and the dictionary."""
    path = OUT / f"{name}.csv"
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
            n += 1
    TABLES[name] = {
        "header": header,
        "rows": n,
        "bytes": path.stat().st_size,
        "description": description,
        "keys": keys or [],
    }
    print(f"  {name+'.csv':<38} {n:>9,} rows  {path.stat().st_size/1e6:>7.2f} MB")
    return path


# --------------------------------------------------------------- reference


def build_reference():
    print("reference")
    write_table(
        "states",
        ["state_fips", "state_abbr", "state_name"],
        sorted((f, a, n) for a, (f, n) in STATES.items()),
        "US state / DC / Puerto Rico lookup: FIPS code, postal abbreviation, name.",
        keys=["state_fips"],
    )


# --------------------------------------------------------------- Hacker News


ROLE_LABELS = {}
TECH_LABELS = {}


def build_hn():
    print("hacker news")
    tax = read_json(DATA / "hn_taxonomy.json")
    techs = {k: v for k, v in tax.items() if not k.startswith("_")}
    roles = tax.get("_roles", {})
    for k, v in techs.items():
        TECH_LABELS[k] = (v.get("label", k), v.get("category", ""))
    for k, v in roles.items():
        ROLE_LABELS[k] = v.get("label", k)

    write_table(
        "hn_technologies",
        ["technology_id", "label", "category", "n_match_patterns"],
        sorted((k, v.get("label", k), v.get("category", ""), len(v.get("patterns", [])))
               for k, v in techs.items()),
        "The 112 technologies tracked in the Hacker News corpus, with the category "
        "used to group them and how many regex patterns define each one.",
        keys=["technology_id"],
    )
    write_table(
        "hn_roles",
        ["role_id", "label", "n_match_patterns"],
        sorted((k, v.get("label", k), len(v.get("patterns", [])))
               for k, v in roles.items()),
        "The role taxonomy applied to each posting. A posting can carry several roles.",
        keys=["role_id"],
    )

    posting_rows, tech_rows, role_rows = [], [], []
    with open(DATA / "hn_postings.jsonl", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            pid, month = d["id"], d["m"]
            posting_rows.append((
                pid, month, int(month[:4]), int(month[5:7]),
                d.get("a"), d.get("co"), d.get("t"), d.get("loc"),
                d["rc"], d.get("rs"), d.get("sr"),
                d.get("smin"), d.get("smax"), d.get("cur"),
                1 if d.get("v") else (0 if "v" in d else None),
                1 if d.get("rel") else (0 if "rel" in d else None),
                len(d.get("tc", [])), len(d.get("r", [])),
                d.get("x"),
                f"https://news.ycombinator.com/item?id={pid}",
            ))
            for t in d.get("tc", []):
                tech_rows.append((pid, month, t))
            for r in d.get("r", []):
                role_rows.append((pid, month, r))

    write_table(
        "hn_postings",
        ["posting_id", "thread_month", "year", "month", "author", "company",
         "role_title", "location_raw", "remote_class", "remote_scope", "seniority",
         "salary_min", "salary_max", "salary_currency", "visa_sponsorship",
         "relocation", "n_technologies", "n_roles", "excerpt", "hn_url"],
        posting_rows,
        "One row per job posting: every top-level comment on a monthly "
        "'Ask HN: Who is hiring?' thread, 2011-04 to 2026-09, parsed into fields. "
        "remote_class is remote | hybrid | onsite | unknown. excerpt is the first "
        "160 characters of the cleaned posting text; hn_url resolves to the full "
        "original comment.",
        keys=["posting_id"],
    )
    write_table(
        "hn_posting_technologies",
        ["posting_id", "thread_month", "technology_id"],
        tech_rows,
        "Bridge table: which technologies each posting mentions. Join to "
        "hn_technologies. A posting appears once per technology it names.",
        keys=["posting_id", "technology_id"],
    )
    write_table(
        "hn_posting_roles",
        ["posting_id", "thread_month", "role_id"],
        role_rows,
        "Bridge table: which roles each posting advertises. Join to hn_roles.",
        keys=["posting_id", "role_id"],
    )

    # ---- aggregates, taken from the already-built trend file
    tr = read_json(DATA / "hn_trends.json")
    months, years = tr["months"], tr["years"]
    partial = set(tr.get("meta", {}).get("partial_years", []) or ["2011", "2026"])

    tot_m = tr["totals"]["month"]
    rc_m = tr["remote"]["month_counts"]
    rows = []
    for i, m in enumerate(months):
        n = tot_m[i]
        rows.append((m, int(m[:4]), int(m[5:7]), n,
                     *[rc_m[k][i] for k in ("remote", "hybrid", "onsite", "unknown")],
                     *[round(rc_m[k][i] / n, 6) if n else None
                       for k in ("remote", "hybrid", "onsite", "unknown")]))
    write_table(
        "hn_monthly_summary",
        ["thread_month", "year", "month", "n_postings",
         "n_remote", "n_hybrid", "n_onsite", "n_unknown",
         "share_remote", "share_hybrid", "share_onsite", "share_unknown"],
        rows,
        "Monthly totals and work-arrangement mix. Shares are of that month's "
        "postings. One row per thread; all 186 months are present with no gaps.",
        keys=["thread_month"],
    )

    tot_y = tr["totals"]["year"]
    rc_y = tr["remote"]["year_counts"]
    rows = []
    for i, y in enumerate(years):
        n = tot_y[i]
        rows.append((y, 1 if y in partial else 0, n,
                     *[rc_y[k][i] for k in ("remote", "hybrid", "onsite", "unknown")],
                     *[round(rc_y[k][i] / n, 6) if n else None
                       for k in ("remote", "hybrid", "onsite", "unknown")]))
    write_table(
        "hn_yearly_summary",
        ["year", "is_partial_year", "n_postings",
         "n_remote", "n_hybrid", "n_onsite", "n_unknown",
         "share_remote", "share_hybrid", "share_onsite", "share_unknown"],
        rows,
        "Yearly rollup of the same figures. is_partial_year = 1 for 2011 (starts "
        "in April) and 2026 (9 of 12 months); never compare those against a full "
        "year without saying so.",
        keys=["year"],
    )

    def counts_table(block, id_col, label_map, name, desc, period, labels, idx_of):
        ids = block["ids"]
        cm = block[f"{period}_counts"]
        rows = []
        for j, tid in enumerate(ids):
            series = cm[tid] if isinstance(cm, dict) else cm[j]
            for i, p in enumerate(labels):
                n = series[i]
                if not n:
                    continue
                denom = idx_of[i]
                rows.append((p, tid, n, round(n / denom, 6) if denom else None))
        return rows

    for period, labels, denom, tname in (
        ("month", months, tot_m, "hn_technology_monthly"),
        ("year", years, tot_y, "hn_technology_yearly"),
    ):
        block = tr["tech"]
        ids = block["ids"]
        cm = block[f"{period}_counts"]
        rows = []
        for j, tid in enumerate(ids):
            series = cm[tid] if isinstance(cm, dict) else cm[j]
            for i, p in enumerate(labels):
                n = series[i]
                if not n:
                    continue
                d = denom[i]
                rows.append((p, tid, n, round(n / d, 6) if d else None))
        per = "thread_month" if period == "month" else "year"
        write_table(
            tname, [per, "technology_id", "n_postings", "share_of_postings"], rows,
            f"How often each technology is named, by {period}. share_of_postings is "
            f"n_postings divided by all postings in that {period} - a technology's "
            "share of job ads, which is not its share of jobs. Rows with a zero "
            "count are omitted.",
            keys=[per, "technology_id"],
        )

    for period, labels, denom, tname in (
        ("month", months, tot_m, "hn_role_monthly"),
        ("year", years, tot_y, "hn_role_yearly"),
    ):
        block = tr["role"]
        ids = block["ids"]
        cm = block[f"{period}_counts"]
        rows = []
        for j, rid in enumerate(ids):
            series = cm[rid] if isinstance(cm, dict) else cm[j]
            for i, p in enumerate(labels):
                n = series[i]
                if not n:
                    continue
                d = denom[i]
                rows.append((p, rid, n, round(n / d, 6) if d else None))
        per = "thread_month" if period == "month" else "year"
        write_table(
            tname, [per, "role_id", "n_postings", "share_of_postings"], rows,
            f"How often each role appears, by {period}, on the same basis as the "
            "technology tables.",
            keys=[per, "role_id"],
        )


# --------------------------------------------------------------- ACS


def build_acs():
    print("census acs")
    a = read_json(DATA / "acs_wfh_state.json")
    years, measures, geos = a["years"], a["measures"], a["geos"]
    rows = []
    for g in geos:
        code = g["code"]
        est, moe = a["e"].get(code), a["m"].get(code)
        for yi, y in enumerate(years):
            ev = est[yi] if est else None
            mv = moe[yi] if moe else None
            if ev is None:
                continue
            total = ev[measures.index("total")]
            for mi, meas in enumerate(measures):
                rows.append((
                    y, code, g["name"], g.get("fips"), g.get("kind"), meas,
                    ev[mi], mv[mi] if mv else None,
                    round(ev[mi] / total, 6) if total else None,
                ))
    write_table(
        "acs_commute_state",
        ["year", "geo_code", "geo_name", "geo_fips", "geo_kind", "measure",
         "estimate", "margin_of_error", "share_of_workers"],
        rows,
        "Census ACS 1-year table B08006, workers 16+ by means of transportation to "
        "work, by state. measure='wfh' is the worked-from-home line - the remote "
        "work series. margin_of_error is the published 90% MOE. There is NO 2020: "
        "the Census Bureau never released a standard 1-year file for it, so no row "
        "exists rather than a zero or an estimate.",
        keys=["year", "geo_code", "measure"],
    )

    o = read_json(DATA / "acs_wfh_occupation.json")
    labels = {x["key"]: x["label"] for x in o["occupations"]}
    keys = [x["key"] for x in o["occupations"]]
    rows = []
    for g in o["geos"]:
        code = g["code"]
        te, tm = o["total_e"].get(code), o["total_m"].get(code)
        we, wm = o["wfh_e"].get(code), o["wfh_m"].get(code)
        sh, sm = o["share"].get(code), o["share_moe"].get(code)
        for yi, y in enumerate(o["years"]):
            if not te or te[yi] is None:
                continue
            for oi, k in enumerate(keys):
                rows.append((
                    y, code, g["name"], k, labels[k],
                    te[yi][oi], tm[yi][oi] if tm and tm[yi] else None,
                    we[yi][oi] if we and we[yi] else None,
                    wm[yi][oi] if wm and wm[yi] else None,
                    sh[yi][oi] if sh and sh[yi] else None,
                    sm[yi][oi] if sm and sm[yi] else None,
                ))
    write_table(
        "acs_wfh_by_occupation",
        ["year", "geo_code", "geo_name", "occupation_group", "occupation_label",
         "total_workers", "total_moe", "wfh_workers", "wfh_moe",
         "wfh_share", "wfh_share_moe"],
        rows,
        "Census ACS 1-year table B08124: worked-from-home crossed with broad "
        "occupation group, by state. This is the table that shows which kinds of "
        "work actually went remote. Same 2020 gap as acs_commute_state.",
        keys=["year", "geo_code", "occupation_group"],
    )


# --------------------------------------------------------------- Indeed


def build_indeed():
    print("indeed hiring lab")
    r = read_json(ROOT / "site" / "explore" / "data" / "remote.json")["indeed"]
    cnames = {c["c"]: c["n"] for c in r["countries"]}
    months = r["months"]
    series = r["series"]
    blank = [None] * len(months)
    rows = []
    for c in cnames:
        for i, m in enumerate(months):
            # a country can be present with an explicit null series, not just absent
            vals = [(series[k].get(c) or blank)[i]
                    for k in ("remote_postings", "remote_searches", "ai_postings")]
            if all(v is None for v in vals):
                continue
            rows.append((m, c, cnames[c], *vals))
    write_table(
        "indeed_remote_monthly",
        ["month", "country_code", "country_name", "remote_share_postings",
         "remote_share_searches", "ai_share_postings"],
        rows,
        "Indeed Hiring Lab monthly trackers, 2019-01 to 2026-07. "
        "remote_share_postings = percent of job postings whose text mentions remote "
        "or hybrid work (employer demand). remote_share_searches = percent of job "
        "searches using a remote term (worker demand). ai_share_postings = percent "
        "of postings mentioning generative-AI terms. Values are PERCENTAGES (2.48 "
        "means 2.48%), not proportions.",
        keys=["month", "country_code"],
    )

    sectors = {s["code"]: s for s in r["sectors"]}
    last = r.get("sector_last_month", {})
    rows = []
    for c, per_sector in r["sector_series"].items():
        for sec, vals in per_sector.items():
            meta = sectors.get(sec, {})
            for i, m in enumerate(months):
                v = vals[i]
                if v is None:
                    continue
                rows.append((m, c, cnames.get(c, c), sec,
                             meta.get("label", sec),
                             1 if meta.get("tech") else 0, v))
    write_table(
        "indeed_remote_by_sector_monthly",
        ["month", "country_code", "country_name", "sector_code", "sector_label",
         "is_tech_sector", "remote_share_postings"],
        rows,
        "The same remote-share-of-postings measure, split by Indeed's 53 "
        "occupational sectors. is_tech_sector marks the 10 technical sectors - "
        "software development, IT systems, IT infrastructure and support, data and "
        "analytics, scientific R&D, and the five engineering and architecture "
        "sectors; for software work specifically filter to sector_code IN "
        "('techsoftware','techinfo','techhelp','math'). Percentages, not "
        "proportions. Not every sector is published for every country - the US has "
        "32 of the 53 - and a series simply stops where Indeed stopped it.",
        keys=["month", "country_code", "sector_code"],
    )
    write_table(
        "indeed_sectors",
        ["sector_code", "sector_label", "is_tech_sector", "last_month_us"],
        sorted((code, s.get("label", code), 1 if s.get("tech") else 0,
                (last.get("US") or {}).get(code))
               for code, s in sectors.items()),
        "Indeed's occupational sector codes with readable labels, and the last "
        "month each one has US data for.",
        keys=["sector_code"],
    )


# --------------------------------------------------------------- OEWS


def _flag_lookup(flags):
    """{field: {marker: [row idx]}} -> {(field, row): marker}"""
    out = {}
    for field, markers in (flags or {}).items():
        for marker, idxs in markers.items():
            for i in idxs:
                out[(field, i)] = marker
    return out


def build_oews():
    print("bls oews")
    s = read_json(DATA / "oews_state.json")
    areas, occs = s["areas"], s["occupations"]
    rows = []
    for y in s["years"]:
        v = s["values"][str(y)]
        fl = _flag_lookup(v.get("flags"))
        for i, oi in enumerate(v["o"]):
            o, a = occs[oi], areas[v["s"][i]]
            rows.append((
                y, a["fips"], a["title"], a.get("abbr"), a.get("kind"),
                o["code"], o["title"], o["group"],
                v["tot_emp"][i], v["jobs_1000"][i], v["loc_quotient"][i],
                v["a_median"][i], v["a_mean"][i], v["h_median"][i],
                fl.get(("tot_emp", i), ""), fl.get(("a_median", i), ""),
                fl.get(("a_mean", i), ""), fl.get(("h_median", i), ""),
            ))
    write_table(
        "oews_state_occupation",
        ["year", "area_fips", "area_title", "state_abbr", "area_kind",
         "occ_code", "occ_title", "occ_group", "total_employment",
         "jobs_per_1000", "location_quotient", "annual_median_wage",
         "annual_mean_wage", "hourly_median_wage",
         "flag_employment", "flag_annual_median", "flag_annual_mean",
         "flag_hourly_median"],
        rows,
        "BLS Occupational Employment and Wage Statistics by state and occupation, "
        "May 2018 to May 2024. occ_group is total (00-0000), major (XX-0000) or "
        "detailed - summing across levels double-counts. The flag_* columns carry "
        "the BLS marker for that cell: '**' estimate not released, '*' wage not "
        "available, '#' wage AT OR ABOVE the top code (a censored HIGH value, not "
        "missing - $208,000/yr for 2018-2021, $239,200/yr from 2022), '~' below "
        "0.005%. A flagged cell has a NULL value; treating '#' as missing biases "
        "high-wage occupations downward.",
        keys=["year", "area_fips", "occ_code", "occ_group"],
    )

    n = read_json(DATA / "oews_national.json")
    nocc = n["occupations"]
    rows = []
    for y in n["years"]:
        v = n["values"][str(y)]
        fl = _flag_lookup(v.get("flags"))
        for i, oi in enumerate(v["o"]):
            o = nocc[oi]
            rows.append((
                y, o["code"], o["title"], o["group"],
                v["tot_emp"][i], v["a_median"][i], v["a_mean"][i], v["h_median"][i],
                fl.get(("tot_emp", i), ""), fl.get(("a_median", i), ""),
                fl.get(("a_mean", i), ""), fl.get(("h_median", i), ""),
            ))
    write_table(
        "oews_national_occupation",
        ["year", "occ_code", "occ_title", "occ_group", "total_employment",
         "annual_median_wage", "annual_mean_wage", "hourly_median_wage",
         "flag_employment", "flag_annual_median", "flag_annual_mean",
         "flag_hourly_median"],
        rows,
        "The same OEWS measures for the United States as a whole, including the "
        "minor and broad SOC levels the state files omit. Use it as the "
        "denominator for national shares and location quotients.",
        keys=["year", "occ_code", "occ_group"],
    )

    m = read_json(DATA / "oews_metro.json")
    myears, mocc, metros = m["years"], m["occupations"], m["metros"]
    rows = []
    for mi, metro in enumerate(metros):
        ser = m["series"][mi]
        tc = {(a, b, c) for a, b, c in ser.get("tc", [])}
        for j, oi in enumerate(ser["o"]):
            o = mocc[oi]
            for yi, y in enumerate(myears):
                e = ser["e"][j][yi]
                med = ser["m"][j][yi]
                mean = ser["w"][j][yi]
                if e is None and med is None and mean is None:
                    continue
                rows.append((
                    y, metro["a"], metro["t"], ";".join(metro.get("s") or []),
                    metro.get("lat"), metro.get("lon"),
                    o["c"], o["t"], o["g"], 1 if o.get("it") else 0,
                    e, med, mean,
                    "#" if (j, yi, 0) in tc else "",
                    "#" if (j, yi, 1) in tc else "",
                ))
    write_table(
        "oews_metro_occupation",
        ["year", "cbsa_code", "metro_title", "component_states", "latitude",
         "longitude", "occ_code", "occ_title", "occ_group", "is_it_occupation",
         "total_employment", "annual_median_wage", "annual_mean_wage",
         "flag_annual_median", "flag_annual_mean"],
        rows,
        "OEWS for 219 metropolitan statistical areas: the largest 200 by 2024 "
        "employment, plus every metro that is top-25 nationally for any detailed "
        "computer/mathematical occupation, so tech hubs are never cut. "
        "is_it_occupation marks the 39 detailed 15-xxxx occupations. Coordinates "
        "come from the Census gazetteer.",
        keys=["year", "cbsa_code", "occ_code"],
    )

    write_table(
        "oews_occupations",
        ["occ_code", "occ_title", "occ_group", "soc_major_group", "years_published",
         "is_it_occupation"],
        sorted({
            (o["code"], o["title"], o["group"], o["code"][:2] + "-0000",
             ";".join(str(x) for x in o.get("years", [])),
             1 if o["code"].startswith("15-1") or o["code"] == "15-2051" else 0)
            for o in nocc
        }),
        "Every occupation code appearing in the OEWS files, with the years BLS "
        "published it. IMPORTANT: codes are not continuous across the SOC 2010 to "
        "SOC 2018 change. Software Developers is 15-1132 + 15-1133 in 2018, the "
        "combined 15-1256 in 2019-2020, and 15-1252 from 2021. Use years_published "
        "before drawing any multi-year line.",
        keys=["occ_code", "occ_group"],
    )


# --------------------------------------------------------------- SQLite


SQL_TYPES = {
    "year": "INTEGER", "month": "INTEGER", "posting_id": "INTEGER",
    "n_postings": "INTEGER", "n_remote": "INTEGER", "n_hybrid": "INTEGER",
    "n_onsite": "INTEGER", "n_unknown": "INTEGER", "n_technologies": "INTEGER",
    "n_roles": "INTEGER", "n_match_patterns": "INTEGER",
    "salary_min": "INTEGER", "salary_max": "INTEGER",
    "visa_sponsorship": "INTEGER", "relocation": "INTEGER",
    "total_employment": "INTEGER", "annual_median_wage": "INTEGER",
    "annual_mean_wage": "INTEGER", "estimate": "INTEGER",
    "margin_of_error": "INTEGER", "total_workers": "INTEGER",
    "total_moe": "INTEGER", "wfh_workers": "INTEGER", "wfh_moe": "INTEGER",
    "is_partial_year": "INTEGER", "is_it_occupation": "INTEGER",
}
REAL_HINTS = ("share", "quotient", "jobs_per_1000", "hourly", "lat", "lon", "moe")


def sql_type(col):
    if col in SQL_TYPES:
        return SQL_TYPES[col]
    if any(h in col for h in REAL_HINTS):
        return "REAL"
    return "TEXT"


def build_sqlite():
    print("sqlite")
    db = OUT / "us_remote_work.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    for name, meta in TABLES.items():
        cols = ", ".join(f'"{c}" {sql_type(c)}' for c in meta["header"])
        con.execute(f'CREATE TABLE "{name}" ({cols})')
        with open(OUT / f"{name}.csv", encoding="utf-8") as f:
            rd = csv.reader(f)
            next(rd)
            ph = ",".join("?" * len(meta["header"]))
            con.executemany(
                f'INSERT INTO "{name}" VALUES ({ph})',
                ([v if v != "" else None for v in row] for row in rd),
            )
        for k in meta["keys"]:
            con.execute(f'CREATE INDEX "ix_{name}_{k}" ON "{name}"("{k}")')
    # A few joins users would otherwise have to rediscover.
    con.executescript("""
    CREATE VIEW v_hn_posting_tech AS
      SELECT p.posting_id, p.thread_month, p.year, p.remote_class, p.seniority,
             t.technology_id, x.label AS technology, x.category
        FROM hn_postings p
        JOIN hn_posting_technologies t ON t.posting_id = p.posting_id
        JOIN hn_technologies x ON x.technology_id = t.technology_id;

    CREATE VIEW v_hn_tech_remote_share AS
      SELECT t.technology_id, x.label AS technology, p.year,
             COUNT(*) AS n_postings,
             SUM(CASE WHEN p.remote_class = 'remote' THEN 1 ELSE 0 END) AS n_remote,
             1.0 * SUM(CASE WHEN p.remote_class = 'remote' THEN 1 ELSE 0 END)
                 / COUNT(*) AS share_remote
        FROM hn_postings p
        JOIN hn_posting_technologies t ON t.posting_id = p.posting_id
        JOIN hn_technologies x ON x.technology_id = t.technology_id
       GROUP BY t.technology_id, p.year;

    CREATE VIEW v_acs_wfh_state AS
      SELECT year, geo_code, geo_name, estimate AS wfh_workers,
             margin_of_error AS wfh_moe, share_of_workers AS wfh_share
        FROM acs_commute_state WHERE measure = 'wfh';

    CREATE VIEW v_oews_it_state AS
      SELECT * FROM oews_state_occupation
       WHERE occ_group = 'detailed'
         AND (occ_code LIKE '15-1%' OR occ_code = '15-2051');
    """)
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"  {'us_remote_work.sqlite':<38} {'':>9}  {db.stat().st_size/1e6:>7.2f} MB")
    return db


# --------------------------------------------------------------- metadata


SLUG = "us-remote-work-and-tech-hiring-2011-2026"
# Kaggle owner for dataset-metadata.json. Override with KAGGLE_OWNER to publish
# under a different account; a rebuild must not silently reset a published id.
OWNER = os.environ.get("KAGGLE_OWNER", "tatralabs")


def build_metadata():
    slug = SLUG
    meta = {
        "title": "US Remote Work & Tech Hiring, 2011-2026",
        "subtitle": "94,548 tech job postings, Census work-from-home rates "
                    "and BLS employment",
        "description": (
            "How remote work spread across the United States and what it did to "
            "technology hiring: every 'Ask HN: Who is hiring?' posting since 2011 "
            "parsed into structured fields, set against Census ACS "
            "work-from-home rates and BLS employment and wages. See README.md for "
            "the full data dictionary and the caveats that matter."
        ),
        "id": f"{OWNER}/{slug}",
        "licenses": [{"name": "CC-BY-4.0"}],
        # Kaggle validates keywords against its own tag vocabulary and rejects the
        # upload on an unknown one, so this list stays to common, known-good tags.
        # Add more specific ones in the web UI after the dataset exists.
        "keywords": ["business", "economics", "employment", "computer science",
                     "internet"],
        "resources": [
            {"path": f"{name}.csv", "description": meta_["description"],
             "schema": {"fields": [{"name": c} for c in meta_["header"]]}}
            for name, meta_ in TABLES.items()
        ] + [{"path": "us_remote_work.sqlite",
              "description": "Every CSV in this dataset as one typed, indexed "
                             "SQLite database, plus four convenience views."}],
    }
    p = OUT / "dataset-metadata.json"
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  {'dataset-metadata.json':<38}")
    return p


def build_readme(db_path):
    db_bytes = db_path.stat().st_size
    slug = SLUG
    total = sum(t["bytes"] for t in TABLES.values()) + db_bytes
    lines = [
        "# US Remote Work & Tech Hiring, 2011-2026",
        "",
        "How remote work spread across the United States, and what it did to "
        "technology hiring - assembled from four public sources and published as "
        "tidy CSVs plus a ready-to-query SQLite database.",
        "",
        "Built for the "
        "[US Remote Job Explorer](https://github.com/tatra-labs/jobs), which "
        "visualises all of it.",
        "",
        "## What makes this different",
        "",
        "The headline table is **94,548 individual job postings** from every "
        "monthly *Ask HN: Who is hiring?* thread since April 2011, each parsed "
        "into structured fields: work arrangement (remote / hybrid / onsite), "
        "role, seniority, salary, and the technologies it names. That corpus is "
        "then set against official statistics - Census ACS work-from-home rates "
        "and BLS employment and wages - so a signal from job ads can be checked "
        "against how many people actually hold the job.",
        "",
        "## Tables",
        "",
        "| Table | Rows | Size | What it is |",
        "|---|---:|---:|---|",
    ]
    for name, t in TABLES.items():
        first = t["description"].split(". ")[0].rstrip(".")
        lines.append(f"| `{name}` | {t['rows']:,} | {t['bytes']/1e6:.1f} MB | {first} |")
    lines += [
        f"| `us_remote_work.sqlite` | - | {db_path.stat().st_size/1e6:.1f} MB | "
        "All of the above, typed and indexed, with four convenience views |",
        "",
        f"Total: {total/1e6:.0f} MB.",
        "",
        "## Data dictionary",
        "",
    ]
    for name, t in TABLES.items():
        lines += [f"### `{name}.csv`", "", t["description"], "",
                  "Columns: " + ", ".join(f"`{c}`" for c in t["header"]), ""]

    lines += [
        "## Reading this data honestly",
        "",
        "Most of the work in building this went into these distinctions. They are "
        "encoded in the data, not left to the reader:",
        "",
        "- **Hacker News is a biased sample.** YC-adjacent startups and "
        "remote-friendly software companies, not the US labour market. A "
        "technology's share of postings is its share of *these employers' ads*, "
        "not its share of jobs. Posting volume fell from 10,259 (2021) to 2,644 "
        "(2026 partial), so shares are informative and raw counts are sample "
        "support, never a hiring indicator.",
        "- **There is no 2020 ACS.** The Census Bureau never released a standard "
        "1-year file. No row exists for it. Do not interpolate across it.",
        "- **BLS suppression is not zero, and top-coding is not missing.** `#` "
        "means the wage is at or above the top code ($208,000/yr for 2018-2021, "
        "$239,200/yr from 2022) - a censored high value. `*` and `**` mean the "
        "estimate genuinely was not released. The `flag_*` columns keep these "
        "apart; the value column is NULL for all of them.",
        "- **Occupation codes break across SOC vintages.** Software Developers is "
        "15-1132 + 15-1133 in 2018, the combined 15-1256 in 2019-2020, and "
        "15-1252 from 2021. `oews_occupations.years_published` tells you which "
        "years each code exists. A seven-year line through them is wrong.",
        "- **Partial periods are flagged.** 2011 starts in April and 2026 has 9 of "
        "12 months; `is_partial_year` marks both.",
        "- **Survey estimates carry margins of error.** ACS margins are published "
        "alongside every estimate. For a difference between two years, combine "
        "them as sqrt(moe1^2 + moe2^2) before calling a change real - by that "
        "test 47 of 52 states fell between 2021 and 2024, not 50.",
        "- **Do not sum OEWS occupation levels.** total, major and detailed rows "
        "overlap by construction.",
        "",
        "## Sources and licence",
        "",
        "| Source | Licence |",
        "|---|---|",
        "| [BLS OEWS](https://www.bls.gov/oes/) | US Government work, public domain |",
        "| [Census ACS](https://www.census.gov/programs-surveys/acs/) | US Government work, public domain |",
        "| [Indeed Hiring Lab](https://github.com/hiring-lab) | CC BY 4.0 |",
        "| [Hacker News](https://news.ycombinator.com/) via the [Algolia API](https://hn.algolia.com/api) | Public API; see note below |",
        "",
        "Released as **CC BY 4.0**, matching the most restrictive input licence.",
        "",
        "Posting text is **not** redistributed in full: `hn_postings.excerpt` holds "
        "the first 160 characters for identification, and `hn_url` links to the "
        "original comment, whose text remains its author's. The structured fields "
        "are derived measurements.",
        "",
        "## Rebuilding",
        "",
        "Every file here is generated, not hand-edited:",
        "",
        "```bash",
        "uv run python pipeline/build_kaggle_dataset.py",
        "```",
        "",
        "## Publishing to Kaggle",
        "",
        "```bash",
        "pip install kaggle",
        "# 1. put your kaggle.json API token in ~/.kaggle/ (chmod 600)",
        "# 2. set your username in kaggle/dataset-metadata.json:",
        "kaggle datasets create -p kaggle/",
        "```",
        "",
        "To push an update later:",
        "",
        "```bash",
        "kaggle datasets version -p kaggle/ -m \"Refresh through <month>\"",
        "```",
        "",
        "Notes for the upload:",
        "",
        "- Kaggle validates `keywords` against its own tag vocabulary and rejects "
        "the whole upload on an unknown tag, so the metadata ships with a short "
        "known-good list. Add more specific tags in the web UI afterwards.",
        f"- `us_remote_work.sqlite` is {db_bytes/1e6:.0f} MB and duplicates the "
        "CSVs. Delete it before uploading if you would rather keep the dataset "
        "small; everything in it is derivable from the CSVs.",
        "- The largest CSV is well under Kaggle's per-file limit, and the whole "
        "directory is far below the 20 GB dataset cap.",
    ]
    p = OUT / "README.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {'README.md':<38}")


def main():
    build_reference()
    build_hn()
    build_acs()
    build_indeed()
    build_oews()
    db = build_sqlite()
    build_metadata()
    build_readme(db)
    total = sum(t["bytes"] for t in TABLES.values()) + db.stat().st_size
    print(f"\n{len(TABLES)} tables, "
          f"{sum(t['rows'] for t in TABLES.values()):,} rows, "
          f"{total/1e6:.1f} MB in {OUT}")


if __name__ == "__main__":
    main()
