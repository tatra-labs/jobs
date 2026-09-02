"""
Indeed Hiring Lab job-postings series -> data/indeed_remote.json + data/indeed_postings.json

Labor DEMAND side of the project (complements the ACS/OEWS supply-side data).

Sources (all public, no auth, raw.githubusercontent.com). NOTE the default branch
differs per repo -- verified via the GitHub API on this run:
    hiring-lab/remote-tracker         -> main
    hiring-lab/ai-tracker             -> main
    hiring-lab/job_postings_tracker   -> master   (NOT main; raw URLs 404 on main)

Monthly aggregation rule: MONTH-MEAN (arithmetic mean of every daily observation
falling in the calendar month). Chosen over month-end because every Indeed series
is already a 7-day trailing average of a noisy daily panel, so a month-end snapshot
would inherit one day's idiosyncrasy. `month_days` records how many daily
observations went into each month, and `partial_months` names the incomplete ones.

Idempotent: raw downloads are disk-cached under raw/indeed/, outputs are overwritten.
"""

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.common import DATA, RAW, fetch, write_json  # noqa: E402

RAW_DIR = RAW / "indeed"
GH = "https://raw.githubusercontent.com/hiring-lab"

# Branch verified per-repo via https://api.github.com/repos/hiring-lab/<repo>
BRANCH = {"remote-tracker": "main", "ai-tracker": "main", "job_postings_tracker": "master"}

SOURCES = {
    "remote_postings.csv": f"{GH}/remote-tracker/main/remote_postings.csv",
    "remote_postings_sector.csv": f"{GH}/remote-tracker/main/remote_postings_sector.csv",
    "remote_searches.csv": f"{GH}/remote-tracker/main/remote_searches.csv",
    "AI_posting.csv": f"{GH}/ai-tracker/main/AI_posting.csv",
    "aggregate_job_postings_US.csv":
        f"{GH}/job_postings_tracker/master/US/aggregate_job_postings_US.csv",
    "job_postings_by_sector_US.csv":
        f"{GH}/job_postings_tracker/master/US/job_postings_by_sector_US.csv",
    "state_job_postings_us.csv":
        f"{GH}/job_postings_tracker/master/US/state_job_postings_us.csv",
    "sector-job-title-examples.csv":
        f"{GH}/job_postings_tracker/master/sector-job-title-examples.csv",
    "README_remote_tracker.md": f"{GH}/remote-tracker/main/README.md",
    "README_ai_tracker.md": f"{GH}/ai-tracker/main/README.md",
    "README_job_postings_tracker.md": f"{GH}/job_postings_tracker/master/README.md",
}

COUNTRY_NAMES = {
    "AU": "Australia", "CA": "Canada", "DE": "Germany", "FR": "France",
    "GB": "United Kingdom", "IE": "Ireland", "IT": "Italy", "JP": "Japan",
    "NL": "Netherlands", "US": "United States",
}

# ---------------------------------------------------------------------------
# normtitlecategory_consistent code -> human label.
#
# There is NO crosswalk file anywhere in the hiring-lab org (all 6 repos checked:
# job_postings_tracker, remote-tracker, ai-tracker, indeed-wage-tracker,
# pay-transparency, pages -- every other product publishes display labels, only
# remote-tracker publishes raw codes). The label vocabulary below is the union of
# the two authoritative Indeed label lists we DO have:
#   * job_postings_by_sector_US.csv `display_name`   (47 CURRENT labels)
#   * sector-job-title-examples.csv `sector`         (47 labels, a STALE vintage)
# Codes are matched to that vocabulary by stem plus the published job-title examples.
#
# label_source:
#   "current"  -> label is verbatim from job_postings_by_sector_US.csv display_name
#   "legacy"   -> label is verbatim from sector-job-title-examples.csv only
#                 (sector has since been dropped from the US postings index)
#   "inferred" -> the code has no counterpart in either Indeed list, or the match is
#                 genuinely ambiguous. Flagged in the output; treat with caution.
SECTOR_LABELS = {
    "accounting":    ("Accounting", "current"),
    "admin":         ("Administrative Assistance", "current"),
    "agriculture":   ("Agriculture & Forestry", "inferred"),
    "arch":          ("Architecture", "current"),
    "arts":          ("Arts & Entertainment", "current"),
    "aviation":      ("Aviation", "current"),
    # `care` vs `personal`: both plausibly map to Personal Care & Home Health /
    # Beauty & Wellness. Split using the published examples ("caregiver, support
    # worker" vs "hair stylist, salon manager"); neither is corroborated by a code
    # list, so both are marked inferred.
    "care":          ("Personal Care & Home Health", "inferred"),
    "childcare":     ("Childcare", "current"),
    "construction":  ("Construction", "current"),
    "customer":      ("Customer Service", "current"),
    "driver":        ("Driving", "current"),
    "education":     ("Education & Instruction", "current"),
    "engchem":       ("Chemical Engineering", "inferred"),
    "engcivil":      ("Civil Engineering", "current"),
    "engelectric":   ("Electrical Engineering", "current"),
    "engid":         ("Industrial Engineering", "current"),
    "engmech":       ("Mechanical Engineering", "current"),
    "finance":       ("Banking & Finance", "current"),
    "food":          ("Food Preparation & Service", "current"),
    "hospitality":   ("Hospitality & Tourism", "current"),
    "hr":            ("Human Resources", "current"),
    "install":       ("Installation & Maintenance", "current"),
    "insurance":     ("Insurance", "current"),
    "legal":         ("Legal", "current"),
    "management":    ("Management", "current"),
    "manufacturing": ("Production & Manufacturing", "current"),
    "marketing":     ("Marketing", "current"),
    "math":          ("Data & Analytics", "current"),      # formerly "Mathematics"
    "meddental":     ("Dental", "current"),
    "meddr":         ("Physicians & Surgeons", "current"),
    "media":         ("Media & Communications", "current"),
    "medinfo":       ("Medical Information", "current"),
    "mednurse":      ("Nursing", "current"),
    "medtech":       ("Medical Technician", "current"),
    "personal":      ("Beauty & Wellness", "inferred"),
    "pharmacy":      ("Pharmacy", "current"),
    "project":       ("Project Management", "current"),
    "protective":    ("Security & Public Safety", "current"),
    "realestate":    ("Real Estate", "inferred"),
    "retail":        ("Retail", "current"),
    "sales":         ("Sales", "current"),
    "sanitation":    ("Cleaning & Sanitation", "current"),
    "science":       ("Scientific Research & Development", "current"),
    "service":       ("Community & Social Service", "inferred"),
    "socialscience": ("Social Science", "current"),
    "sports":        ("Sports", "legacy"),
    # renamed by Indeed: "IT Operations & Helpdesk" -> current label below
    "techhelp":      ("IT Infrastructure, Operations & Support", "current"),
    # renamed by Indeed: "Information Design & Documentation" -> current label below
    "techinfo":      ("IT Systems & Solutions", "current"),
    "techsoftware":  ("Software Development", "current"),
    "therapy":       ("Therapy", "current"),
    "transport":     ("Logistic Support", "inferred"),
    "veterinary":    ("Veterinary", "legacy"),
    "warehouse":     ("Loading & Stocking", "inferred"),
}

# IT / STEM sector codes, for question 3 of the project (remote IT roles).
TECH_CODES = {"techsoftware", "techinfo", "techhelp", "math", "science",
              "engelectric", "engid", "engmech", "engcivil", "engchem", "arch"}

# Explicit, documented size budget. Daily arrays are dropped in a fixed priority
# order if the file would exceed it; whatever is dropped is PRINTED, never silent.
REMOTE_JSON_BUDGET_MB = 3.0


def cached(name, force=False):
    """common.fetch plus a retry: Windows AV/indexer intermittently locks the .part file."""
    dest = RAW_DIR / name
    last = None
    for i in range(4):
        try:
            return fetch(SOURCES[name], dest, force=force)
        except PermissionError as e:  # transient os.replace lock on Windows
            last = e
            time.sleep(1.0 * (i + 1))
    raise last


def num(series, where):
    """Coerce to float. Anything non-numeric (BLS-style '*', '**', '#', '~', blanks,
    or a future upstream marker) becomes NaN and is reported loudly -- never 0, and
    NaN is excluded from means rather than poisoning them."""
    raw = series.astype(str).str.strip()
    out = pd.to_numeric(raw, errors="coerce")
    bad = raw[out.isna() & ~raw.isin(["", "nan", "NA", "NaN", "None", "<NA>"])]
    if len(bad):
        print(f"    !! {where}: {len(bad):,} non-numeric values -> null: "
              f"{sorted(bad.unique())[:10]}")
    return out


def month_grid(dates):
    """Contiguous YYYY-MM list spanning `dates`, observed-day counts, partial months."""
    d = pd.to_datetime(pd.Series(sorted(set(dates))))
    per = d.dt.to_period("M")
    counts = per.value_counts()
    full = pd.period_range(per.min(), per.max(), freq="M")
    months = [str(p) for p in full]
    days = [int(counts.get(p, 0)) for p in full]
    partial = [m for m, got, p in zip(months, days, full) if got != p.days_in_month]
    return months, days, partial


def monthly_mean(df, key_col, months, ndp):
    """{key -> [month-mean or None per month]} on the shared month grid.

    NaN-safe: missing/suppressed days are excluded from the mean, never counted as
    zero. A month with no observed value for a key yields None."""
    g = df[[key_col, "_v"]].copy()
    g["_m"] = df["_d"].dt.to_period("M").astype(str)
    piv = g.groupby([key_col, "_m"])["_v"].mean().unstack("_m").reindex(columns=months)
    return {k: [None if pd.isna(x) else round(float(x), ndp) for x in row]
            for k, row in zip(piv.index, piv.to_numpy())}


def daily_series(df, key_col, dates, ndp):
    """{key -> [value or None per date]} on the shared daily date grid."""
    g = df[[key_col, "_v"]].copy()
    g["_dt"] = df["_d"].dt.strftime("%Y-%m-%d")
    piv = g.pivot(index=key_col, columns="_dt", values="_v").reindex(columns=dates)
    return {k: [None if pd.isna(x) else round(float(x), ndp) for x in row]
            for k, row in zip(piv.index, piv.to_numpy())}


def load(name, value_col):
    df = pd.read_csv(cached(name), dtype=str)
    df["_d"] = pd.to_datetime(df["date"])
    df["_v"] = num(df[value_col], name)
    return df


def count_values(o):
    if isinstance(o, list):
        return sum(count_values(x) for x in o)
    if isinstance(o, dict):
        return sum(count_values(x) for x in o.values())
    return 1 if (o is None or isinstance(o, (int, float))) else 0


def main():
    print("=" * 78)
    print("Indeed Hiring Lab -> data/indeed_remote.json, data/indeed_postings.json")
    print("=" * 78)

    print("\n[1/5] downloading (disk-cached under raw/indeed/)")
    for name in SOURCES:
        p = cached(name)
        print(f"    {p.stat().st_size:>12,}  {name}")

    rows_in = {}

    # ------------- country-level share series --------------------------------
    print("\n[2/5] country-level share series (remote postings, remote searches, AI)")
    cty = {}
    for tag, fn, col in [("remote_postings", "remote_postings.csv", "remote_share_postings"),
                         ("remote_searches", "remote_searches.csv", "remote_share_searches"),
                         ("ai_postings", "AI_posting.csv", "AI_share_postings")]:
        df = load(fn, col)
        rows_in[fn] = len(df)
        assert not df.duplicated(["date", "jobcountry"]).any(), f"{fn}: duplicate (date,country)"
        cty[tag] = df
        print(f"    {tag:<16} rows={len(df):>7,}  n_countries={df.jobcountry.nunique()} "
              f"{sorted(df.jobcountry.unique())}  {df._d.min().date()}..{df._d.max().date()} "
              f" nulls={int(df._v.isna().sum())}")

    grid_a = sorted(set().union(*[set(d["date"]) for d in cty.values()]))
    months_a, days_a, partial_a = month_grid(grid_a)
    expected_a = pd.date_range(grid_a[0], grid_a[-1], freq="D")
    print(f"    grid A: {len(grid_a)} days {grid_a[0]}..{grid_a[-1]} "
          f"(expected {len(expected_a)}, gaps {len(expected_a) - len(grid_a)})")
    print(f"            {len(months_a)} months {months_a[0]}..{months_a[-1]}, "
          f"partial: {partial_a or 'none'}")
    countries_a = sorted(set().union(*[set(d.jobcountry.unique()) for d in cty.values()]))

    # ------------- remote share by occupational sector -----------------------
    print("\n[3/5] remote share by occupational sector")
    sec = load("remote_postings_sector.csv", "remote_share_postings")
    rows_in["remote_postings_sector.csv"] = len(sec)
    assert not sec.duplicated(["date", "jobcountry", "normtitlecategory_consistent"]).any()
    codes = sorted(sec.normtitlecategory_consistent.unique())
    unknown = [c for c in codes if c not in SECTOR_LABELS]
    if unknown:
        print(f"    !! {len(unknown)} sector codes have NO label mapping "
              f"(emitted with the raw code as the label): {unknown}")
    sec_countries = sorted(sec.jobcountry.unique())
    print(f"    rows={len(sec):,}  distinct codes={len(codes)}  countries={sec_countries}")
    print(f"    span {sec._d.min().date()}..{sec._d.max().date()}  "
          f"nulls={int(sec._v.isna().sum())}")
    for c in sec_countries:
        g = sec[sec.jobcountry == c]
        n = g.normtitlecategory_consistent.nunique()
        per = g.groupby("normtitlecategory_consistent")["_d"].count()
        print(f"      {c}: {n:>2} sectors, days/sector min={per.min()} max={per.max()}")

    sectors_meta = [{"code": c,
                     "label": SECTOR_LABELS.get(c, (c, "inferred"))[0],
                     "label_source": SECTOR_LABELS.get(c, (c, "inferred"))[1],
                     "tech": c in TECH_CODES} for c in codes]
    cidx = {c: i for i, c in enumerate(codes)}

    sector_monthly = {}
    for c in sec_countries:
        sub = sec[sec.jobcountry == c]
        mm = monthly_mean(sub, "normtitlecategory_consistent", months_a, 3)
        sector_monthly[c] = [mm.get(code) for code in codes]

    # ------------- postings index --------------------------------------------
    print("\n[4/5] job postings index (national / sector / state)")
    ag = pd.read_csv(cached("aggregate_job_postings_US.csv"), dtype=str)
    ag["_d"] = pd.to_datetime(ag["date"])
    rows_in["aggregate_job_postings_US.csv"] = len(ag)
    assert not ag.duplicated(["date", "variable"]).any()
    ag_sa = num(ag["indeed_job_postings_index_SA"], "aggregate SA")
    ag_nsa = num(ag["indeed_job_postings_index_NSA"], "aggregate NSA")
    print(f"    aggregate rows={len(ag):,}  variables={sorted(ag.variable.unique())}  "
          f"{ag._d.min().date()}..{ag._d.max().date()}  "
          f"nulls sa={int(ag_sa.isna().sum())} nsa={int(ag_nsa.isna().sum())}")

    sx = load("job_postings_by_sector_US.csv", "indeed_job_postings_index")
    rows_in["job_postings_by_sector_US.csv"] = len(sx)
    assert not sx.duplicated(["date", "display_name", "variable"]).any()
    disp = sorted(sx.display_name.unique())
    print(f"    sector    rows={len(sx):,}  display_names={len(disp)}  "
          f"{sx._d.min().date()}..{sx._d.max().date()}  nulls={int(sx._v.isna().sum())}")

    stt = load("state_job_postings_us.csv", "indeed_job_postings_index")
    rows_in["state_job_postings_us.csv"] = len(stt)
    assert not stt.duplicated(["date", "state"]).any()
    stt["ST"] = stt.state.str.upper()
    states = sorted(stt.ST.unique())
    print(f"    state     rows={len(stt):,}  states={len(states)} (50 + DC, no PR)  "
          f"{stt._d.min().date()}..{stt._d.max().date()}  nulls={int(stt._v.isna().sum())}")

    grid_b = sorted(set(ag["date"]) | set(sx["date"]) | set(stt["date"]))
    months_b, days_b, partial_b = month_grid(grid_b)
    expected_b = pd.date_range(grid_b[0], grid_b[-1], freq="D")
    print(f"    grid B: {len(grid_b)} days {grid_b[0]}..{grid_b[-1]} "
          f"(expected {len(expected_b)}, gaps {len(expected_b) - len(grid_b)})")
    print(f"            {len(months_b)} months {months_b[0]}..{months_b[-1]}, "
          f"partial: {partial_b or 'none'}")

    VAR = {"total postings": "total", "new postings": "new"}
    national_monthly, national_daily = {}, {}
    for src, short in VAR.items():
        mask = ag.variable == src
        national_monthly[short], national_daily[short] = {}, {}
        for lbl, col in [("sa", ag_sa), ("nsa", ag_nsa)]:
            t = ag.loc[mask, ["_d"]].copy()
            t["_v"] = col.loc[mask].to_numpy()
            t["_k"] = "x"
            national_monthly[short][lbl] = monthly_mean(t, "_k", months_b, 2)["x"]
            national_daily[short][lbl] = daily_series(t, "_k", grid_b, 2)["x"]

    sector_index = {}
    for src, short in VAR.items():
        mm = monthly_mean(sx[sx.variable == src], "display_name", months_b, 2)
        sector_index[short] = [mm.get(d) for d in disp]

    state_mm = monthly_mean(stt, "ST", months_b, 2)
    state_monthly = [state_mm.get(s) for s in states]

    ex = pd.read_csv(cached("sector-job-title-examples.csv"))
    ex_map = dict(zip(ex.sector, ex.job_titles))
    disp_meta = [{"label": d, "job_title_examples": ex_map.get(d)} for d in disp]
    stale_only = sorted(set(ex_map) - set(disp))
    no_examples = sorted(set(disp) - set(ex_map))
    print(f"    sector-job-title-examples.csv is a STALE taxonomy vintage:")
    print(f"      {len(stale_only)} of its labels are gone from the current index: {stale_only}")
    print(f"      {len(no_examples)} current labels have no examples (null): {no_examples}")

    # ------------- assemble + write ------------------------------------------
    print("\n[5/5] writing output")

    methodology = {
        "publisher": "Indeed Hiring Lab",
        "license": "CC BY 4.0 - Indeed Hiring Lab must be cited as the source.",
        "smoothing": "Every daily series is a SEVEN-DAY TRAILING AVERAGE, applied upstream "
                     "by Indeed (not by this pipeline).",
        "remote_definition":
            "Indeed first identifies the job location as remote, then keyword-searches the "
            "posting TEXT for terms specifically associated with remote, hybrid and flexible "
            "arrangements (e.g. 'remote work', 'flexible work', 'hybrid role'). remote_share "
            "is therefore the share of postings that MENTION remote/hybrid work - it is NOT a "
            "measure of jobs actually performed remotely, and it lumps hybrid together with "
            "fully remote. Denominator is all postings (remote AND non-remote).",
        "remote_searches_definition":
            "The same keyword set applied to job SEEKER search text on the Indeed platform: "
            "the share of searches containing remote/hybrid keywords. A worker-side demand "
            "signal, not an employer-side one.",
        "remote_methodology_note":
            "https://www.hiringlab.org/wp-content/uploads/2023/06/Hybrid-Remote-Methodology.pdf",
        "ai_definition":
            "Share of postings containing AI keywords (e.g. 'Machine Learning', 'Data "
            "Science', 'Artificial Intelligence'). The ai-tracker README also documents a "
            "GenAI_posting.csv ('Generative AI', 'Large Language Models', 'Chat GPT'), but "
            "that file is NOT present in the repository as retrieved, so no GenAI series is "
            "emitted here.",
        "postings_index_base":
            "February 1, 2020 = 100 (pre-pandemic baseline). A reading of 101 means the level "
            "of postings is 1% above the February 1, 2020 level. It is an INDEX, not a count: "
            "levels are not comparable across sectors or states, only trends are.",
        "postings_index_seasonal_adjustment":
            "Seasonally adjusted daily using the Deutsche Bundesbank methodology for daily "
            "time series (https://www.bundesbank.de/resource/blob/763892/"
            "f5cd282cc57e55aca1eb0d521d3aa0da/mL/2018-10-17-dkp-41-data.pdf). Each series - "
            "national, occupational sector, and sub-national geography - is seasonally "
            "adjusted SEPARATELY, so sector and state series do not aggregate to the "
            "national one.",
        "METHODOLOGY_CHANGE":
            "job_postings_tracker README: Indeed ADOPTED THE NEW DAILY SEASONAL-ADJUSTMENT "
            "METHODOLOGY IN NOVEMBER 2024 AND REVISED HISTORY. Values before Nov 2024 may "
            "differ from what was published at the time, so this index is not comparable "
            "with figures quoted in older Hiring Lab publications. Projected seasonal factors "
            "for the latest calendar year are estimated from the preceding three years, so "
            "the most recent months are the most likely to be revised again.",
        "sector_taxonomy":
            "Occupational sectors are an Indeed categorisation built on normalised job "
            "titles, NOT SOC or NAICS. They do not map cleanly onto the OOH/OEWS occupations "
            "used elsewhere in this project and should not be joined to them on code.",
        "refresh_cadence":
            "remote-tracker and ai-tracker refresh MONTHLY; job_postings_tracker refreshes "
            "WEEKLY. That is why the two date grids end on different days.",
        "aggregation":
            "MONTH-MEAN: the arithmetic mean of every daily observation in the calendar "
            "month. Missing days are excluded from the mean rather than counted as zero; a "
            "month with no observed day is null. See month_days / partial_months.",
        "precision":
            "Share series are rounded to 3 decimal places, index series to 2 (the source "
            "precision of the index). Rounding uses Python's round(), which rounds the true "
            "IEEE-754 double. Recomputing the month-means with numpy's np.round can differ "
            "by 0.01 in roughly 0.5% of index cells at apparent half-way values (e.g. a mean "
            "that prints as 86.955 is stored as 86.95499999999999829, so 86.95 is correct "
            "and np.round's 86.96 is a scale-multiply artifact). Immaterial for charting; "
            "noted so a re-derivation that disagrees in the last digit is not read as a bug.",
        "branches_used": BRANCH,
        "changes_feed": "https://data.indeed.com/#/whats-new",
        "faq": "https://www.hiringlab.org/indeed-data-faq/",
    }

    remote = {
        "meta": {
            "dataset": "Indeed Hiring Lab remote-work and AI mention shares",
            "unit": "percent of job postings (or searches), 0-100",
            "aggregation": "month_mean",
            "sources": {
                "remote_postings": SOURCES["remote_postings.csv"],
                "remote_postings_sector": SOURCES["remote_postings_sector.csv"],
                "remote_searches": SOURCES["remote_searches.csv"],
                "ai_postings": SOURCES["AI_posting.csv"],
                "readme_remote_tracker": SOURCES["README_remote_tracker.md"],
                "readme_ai_tracker": SOURCES["README_ai_tracker.md"],
            },
            "min_date": {t: str(d._d.min().date()) for t, d in cty.items()} |
                        {"remote_postings_sector": str(sec._d.min().date())},
            "max_date": {t: str(d._d.max().date()) for t, d in cty.items()} |
                        {"remote_postings_sector": str(sec._d.max().date())},
            "notes": [
                f"The per-sector series is NOT discontinued. remote_postings_sector.csv runs "
                f"the full {sec._d.min().date()}..{sec._d.max().date()} daily span for every "
                f"(country, sector) pair present, the same span as the country-level files. "
                f"An earlier note that it stopped at 2023-05-26 does not hold for this "
                f"retrieval.",
                f"{len(codes)} distinct sector codes exist across the {len(sec_countries)} "
                f"countries; the US carries "
                f"{sec[sec.jobcountry == 'US'].normtitlecategory_consistent.nunique()} of "
                f"them. A code is either fully present (all days) or fully absent for a given "
                f"country - there are no partial sector histories, so an absent series is "
                f"emitted as null rather than as an array of nulls.",
                "Country coverage differs per series: remote postings 7 countries, remote "
                "searches 8 (adds JP), AI 9 (adds IT and NL, but not JP). Countries are "
                "unioned into one index; a country missing from a series is null there.",
                "No suppression or missing-data markers occur anywhere in these files on this "
                "retrieval; every value parsed as numeric. The loader still maps any "
                "non-numeric token to null (never 0) and prints it.",
                "remote_searches is job-SEEKER interest, remote_postings is EMPLOYER offer - "
                "do not read them as the same quantity.",
            ],
            "methodology": methodology,
        },
        "countries": {"codes": countries_a,
                      "names": [COUNTRY_NAMES.get(c, c) for c in countries_a]},
        "months": months_a,
        "month_days": days_a,
        "partial_months": partial_a,
        "sectors": sectors_meta,
        "monthly": {tag: {c: monthly_mean(df, "jobcountry", months_a, 3).get(c)
                          for c in countries_a}
                    for tag, df in cty.items()},
        "sector_monthly": sector_monthly,
    }

    # Daily arrays, added under an explicit documented size budget.
    remote["dates"] = grid_a
    remote["daily"] = {tag: {c: daily_series(df, "jobcountry", grid_a, 3).get(c)
                             for c in countries_a}
                       for tag, df in cty.items()}
    remote["sector_daily"] = {
        c: [daily_series(sec[sec.jobcountry == c], "normtitlecategory_consistent",
                         grid_a, 3).get(code) for code in codes]
        for c in sec_countries}

    p_remote = DATA / "indeed_remote.json"
    dropped = []

    def drop_nonus_sector_daily(o):
        kept = {c: v for c, v in o["sector_daily"].items() if c == "US"}
        o["sector_daily"] = kept

    # Explicit degradation ladder, applied only while the file exceeds the budget.
    # Each step PRINTS what it removed; nothing is ever dropped silently, and only
    # DAILY detail is ever dropped -- the monthly series are always complete.
    DROP_ORDER = [
        (drop_nonus_sector_daily,
         "per-sector DAILY series for the 6 non-US countries "
         "(US per-sector daily kept; monthly per-sector kept in full for all 7)"),
        (lambda o: o.pop("sector_daily", None),
         "per-sector DAILY series for the US as well "
         "(monthly per-sector series retained in full)"),
        (lambda o: o.pop("daily", None),
         "country-level DAILY series (monthly retained in full)"),
        (lambda o: o.pop("dates", None), "the daily date index"),
    ]
    for step, why in DROP_ORDER:
        write_json(remote, p_remote)
        mb = p_remote.stat().st_size / 1024 / 1024
        if mb <= REMOTE_JSON_BUDGET_MB:
            break
        step(remote)
        dropped.append(why)
        print(f"    OVER the {REMOTE_JSON_BUDGET_MB} MB budget at {mb:.2f} MB "
              f"-> EXCLUDED {why}")
    remote["meta"]["daily_included"] = [k for k in ("dates", "daily", "sector_daily")
                                        if k in remote]
    remote["meta"]["excluded_for_size"] = dropped
    remote["meta"]["size_budget_mb"] = REMOTE_JSON_BUDGET_MB
    write_json(remote, p_remote)

    postings = {
        "meta": {
            "dataset": "Indeed Hiring Lab Job Postings Index (United States)",
            "unit": "index, February 1 2020 = 100 (percent of the pre-pandemic level)",
            "aggregation": "month_mean",
            "sources": {
                "aggregate": SOURCES["aggregate_job_postings_US.csv"],
                "by_sector": SOURCES["job_postings_by_sector_US.csv"],
                "by_state": SOURCES["state_job_postings_us.csv"],
                "sector_job_title_examples": SOURCES["sector-job-title-examples.csv"],
                "readme": SOURCES["README_job_postings_tracker.md"],
            },
            "min_date": {"aggregate": str(ag._d.min().date()),
                         "by_sector": str(sx._d.min().date()),
                         "by_state": str(stt._d.min().date())},
            "max_date": {"aggregate": str(ag._d.max().date()),
                         "by_sector": str(sx._d.max().date()),
                         "by_state": str(stt._d.max().date())},
            "notes": [
                "The job_postings_tracker repo's default branch is MASTER, not main - "
                "raw.githubusercontent.com URLs 404 on main. remote-tracker and ai-tracker "
                "do use main.",
                "'total postings' = all live postings; 'new postings' = postings that have "
                "been on Indeed for 7 days or fewer.",
                "The national file carries both seasonally adjusted (sa) and non-seasonally "
                "adjusted (nsa) series; the sector and state files carry SA only.",
                f"{len(states)} state units: 50 states + DC. No Puerto Rico and no national "
                f"row in the state file.",
                (f"The last month ({months_b[-1]}) is PARTIAL: the weekly refresh had only "
                 f"reached {ag._d.max().date()}, so its month-mean covers "
                 f"{days_b[-1]} days. See month_days / partial_months.")
                if partial_b else "No partial months.",
                "sector-job-title-examples.csv is a stale vintage of the taxonomy: it still "
                f"lists {stale_only}, which the current index does not carry, and lacks "
                f"{no_examples}, which it does. job_title_examples is null for those.",
                "metro_job_postings_us.csv (62 MB, CBSA level) exists upstream but is out of "
                "scope for this output and was deliberately not downloaded.",
                "No suppression or missing-data markers occur anywhere in these files on this "
                "retrieval; every value parsed as numeric.",
            ],
            "methodology": methodology,
        },
        "months": months_b,
        "month_days": days_b,
        "partial_months": partial_b,
        "variables": {"total": "total postings (all live postings)",
                      "new": "new postings (on Indeed for 7 days or fewer)"},
        "sectors": disp_meta,
        "states": states,
        "national_monthly": national_monthly,
        "national_daily": {"dates": grid_b, **national_daily},
        "sector_monthly": sector_index,
        "state_monthly": state_monthly,
    }
    p_post = DATA / "indeed_postings.json"
    write_json(postings, p_post)

    # ------------- summary ----------------------------------------------------
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print("rows in (upstream CSV data rows, header excluded):")
    for k, v in rows_in.items():
        print(f"    {v:>9,}  {k}")
    print(f"    {sum(rows_in.values()):>9,}  TOTAL")

    print("\nindeed_remote.json")
    print(f"    months             {len(months_a)}  {months_a[0]}..{months_a[-1]}")
    print(f"    partial months     {partial_a or 'none'}")
    print(f"    countries          {len(countries_a)}  {countries_a}")
    for tag in cty:
        n = sum(1 for v in remote["monthly"][tag].values() if v is not None)
        print(f"    monthly.{tag:<16} {n} country series x {len(months_a)} months")
    ns = sum(1 for c in sec_countries for v in sector_monthly[c] if v is not None)
    print(f"    sector_monthly     {ns} (country,sector) series x {len(months_a)} months "
          f"over {len(codes)} codes")
    inf = [s["code"] for s in sectors_meta if s["label_source"] == "inferred"]
    leg = [s["code"] for s in sectors_meta if s["label_source"] == "legacy"]
    print(f"    sector labels      {len(sectors_meta) - len(inf) - len(leg)} from Indeed's "
          f"current list, {len(leg)} legacy {leg},")
    print(f"                       {len(inf)} INFERRED {inf}")
    print(f"    daily included     {remote['meta']['daily_included']}")
    print(f"    excluded for size  {dropped or 'nothing'}")
    print(f"    values             {count_values(remote):,}")
    print(f"    size               {p_remote.stat().st_size / 1024 / 1024:.2f} MB")

    print("\nindeed_postings.json")
    print(f"    months             {len(months_b)}  {months_b[0]}..{months_b[-1]}")
    print(f"    partial months     {partial_b or 'none'}")
    print(f"    sectors            {len(disp)}    states {len(states)}    "
          f"variables {list(VAR.values())}")
    print(f"    national           monthly sa+nsa x 2 variables; "
          f"daily {len(grid_b)} days x sa+nsa x 2 variables")
    print(f"    values             {count_values(postings):,}")
    print(f"    size               {p_post.stat().st_size / 1024 / 1024:.2f} MB")

    print("\nper-year coverage (non-null monthly observations / slots)")

    def cov(pairs):
        yr = {}
        for m, v in pairs:
            a, b = yr.get(m[:4], (0, 0))
            yr[m[:4]] = (a + (v is not None), b + 1)
        return "  ".join(f"{y}:{a:,}/{b:,}" for y, (a, b) in sorted(yr.items()))

    print("    remote country  " + cov([(m, v) for tag in cty
                                        for arr in remote["monthly"][tag].values() if arr
                                        for m, v in zip(months_a, arr)]))
    print("    remote sector   " + cov([(m, v) for c in sec_countries
                                        for arr in sector_monthly[c] if arr
                                        for m, v in zip(months_a, arr)]))
    print("    postings sector " + cov([(m, v) for arrs in sector_index.values()
                                        for arr in arrs if arr
                                        for m, v in zip(months_b, arr)]))
    print("    postings state  " + cov([(m, v) for arr in state_monthly if arr
                                        for m, v in zip(months_b, arr)]))
    print()


if __name__ == "__main__":
    main()
