---
license: cc-by-4.0
pretty_name: US Remote Work & Tech Hiring, 2011-2026
language:
  - en
size_categories:
  - 1M<n<10M
task_categories:
  - tabular-classification
  - text-classification
  - feature-extraction
tags:
  - remote-work
  - labor-market
  - employment
  - job-postings
  - hacker-news
  - hiring
  - economics
  - technology-trends
  - bls
  - census
  - time-series
source_datasets:
  - original
annotations_creators:
  - machine-generated
configs:
  - config_name: hn_postings
    data_files:
      - split: train
        path: hn_postings.csv
  - config_name: hn_yearly_summary
    data_files:
      - split: train
        path: hn_yearly_summary.csv
  - config_name: hn_monthly_summary
    data_files:
      - split: train
        path: hn_monthly_summary.csv
  - config_name: hn_technology_yearly
    data_files:
      - split: train
        path: hn_technology_yearly.csv
  - config_name: hn_technology_monthly
    data_files:
      - split: train
        path: hn_technology_monthly.csv
  - config_name: hn_posting_technologies
    data_files:
      - split: train
        path: hn_posting_technologies.csv
  - config_name: hn_role_yearly
    data_files:
      - split: train
        path: hn_role_yearly.csv
  - config_name: hn_role_monthly
    data_files:
      - split: train
        path: hn_role_monthly.csv
  - config_name: hn_posting_roles
    data_files:
      - split: train
        path: hn_posting_roles.csv
  - config_name: hn_technologies
    data_files:
      - split: train
        path: hn_technologies.csv
  - config_name: hn_roles
    data_files:
      - split: train
        path: hn_roles.csv
  - config_name: acs_commute_state
    data_files:
      - split: train
        path: acs_commute_state.csv
  - config_name: acs_wfh_by_occupation
    data_files:
      - split: train
        path: acs_wfh_by_occupation.csv
  - config_name: indeed_remote_monthly
    data_files:
      - split: train
        path: indeed_remote_monthly.csv
  - config_name: indeed_remote_by_sector_monthly
    data_files:
      - split: train
        path: indeed_remote_by_sector_monthly.csv
  - config_name: indeed_sectors
    data_files:
      - split: train
        path: indeed_sectors.csv
  - config_name: oews_state_occupation
    data_files:
      - split: train
        path: oews_state_occupation.csv
  - config_name: oews_metro_occupation
    data_files:
      - split: train
        path: oews_metro_occupation.csv
  - config_name: oews_national_occupation
    data_files:
      - split: train
        path: oews_national_occupation.csv
  - config_name: oews_occupations
    data_files:
      - split: train
        path: oews_occupations.csv
  - config_name: states
    data_files:
      - split: train
        path: states.csv
---

# US Remote Work & Tech Hiring, 2011-2026

How remote work spread across the United States, and what it did to technology hiring - assembled from four public sources and published as tidy CSVs plus a ready-to-query SQLite database.

Built for the [US Remote Job Explorer](https://github.com/tatra-labs/jobs), which visualises all of it.

## What makes this different

The headline table is **94,548 individual job postings** from every monthly *Ask HN: Who is hiring?* thread since April 2011, each parsed into structured fields: work arrangement (remote / hybrid / onsite), role, seniority, salary, and the technologies it names. That corpus is then set against official statistics - Census ACS work-from-home rates and BLS employment and wages - so a signal from job ads can be checked against how many people actually hold the job.

## Tables

| Table | Rows | Size | What it is |
|---|---:|---:|---|
| `states` | 52 | 0.0 MB | US state / DC / Puerto Rico lookup: FIPS code, postal abbreviation, name |
| `hn_technologies` | 112 | 0.0 MB | The 112 technologies tracked in the Hacker News corpus, with the category used to group them and how many regex patterns define each one |
| `hn_roles` | 20 | 0.0 MB | The role taxonomy applied to each posting |
| `hn_postings` | 94,548 | 31.5 MB | One row per job posting: every top-level comment on a monthly 'Ask HN: Who is hiring?' thread, 2011-04 to 2026-09, parsed into fields |
| `hn_posting_technologies` | 332,074 | 8.1 MB | Bridge table: which technologies each posting mentions |
| `hn_posting_roles` | 152,092 | 4.5 MB | Bridge table: which roles each posting advertises |
| `hn_monthly_summary` | 186 | 0.0 MB | Monthly totals and work-arrangement mix |
| `hn_yearly_summary` | 16 | 0.0 MB | Yearly rollup of the same figures |
| `hn_technology_monthly` | 15,626 | 0.4 MB | How often each technology is named, by month |
| `hn_technology_yearly` | 1,530 | 0.0 MB | How often each technology is named, by year |
| `hn_role_monthly` | 3,685 | 0.1 MB | How often each role appears, by month, on the same basis as the technology tables |
| `hn_role_yearly` | 320 | 0.0 MB | How often each role appears, by year, on the same basis as the technology tables |
| `acs_commute_state` | 2,544 | 0.1 MB | Census ACS 1-year table B08006, workers 16+ by means of transportation to work, by state |
| `acs_wfh_by_occupation` | 1,908 | 0.2 MB | Census ACS 1-year table B08124: worked-from-home crossed with broad occupation group, by state |
| `indeed_remote_monthly` | 910 | 0.0 MB | Indeed Hiring Lab monthly trackers, 2019-01 to 2026-07 |
| `indeed_remote_by_sector_monthly` | 22,659 | 1.3 MB | The same remote-share-of-postings measure, split by Indeed's 53 occupational sectors |
| `indeed_sectors` | 53 | 0.0 MB | Indeed's occupational sector codes with readable labels, and the last month each one has US data for |
| `oews_state_occupation` | 259,798 | 30.5 MB | BLS Occupational Employment and Wage Statistics by state and occupation, May 2018 to May 2024 |
| `oews_national_occupation` | 9,648 | 0.8 MB | The same OEWS measures for the United States as a whole, including the minor and broad SOC levels the state files omit |
| `oews_metro_occupation` | 268,959 | 35.2 MB | OEWS for 219 metropolitan statistical areas: the largest 200 by 2024 employment, plus every metro that is top-25 nationally for any detailed computer/mathematical occupation, so tech hubs are never cut |
| `oews_occupations` | 1,540 | 0.1 MB | Every occupation code appearing in the OEWS files, with the years BLS published it |
| `us_remote_work.sqlite` | - | 162.2 MB | All of the above, typed and indexed, with four convenience views |

Total: 275 MB.

## Data dictionary

### `states.csv`

US state / DC / Puerto Rico lookup: FIPS code, postal abbreviation, name.

Columns: `state_fips`, `state_abbr`, `state_name`

### `hn_technologies.csv`

The 112 technologies tracked in the Hacker News corpus, with the category used to group them and how many regex patterns define each one.

Columns: `technology_id`, `label`, `category`, `n_match_patterns`

### `hn_roles.csv`

The role taxonomy applied to each posting. A posting can carry several roles.

Columns: `role_id`, `label`, `n_match_patterns`

### `hn_postings.csv`

One row per job posting: every top-level comment on a monthly 'Ask HN: Who is hiring?' thread, 2011-04 to 2026-09, parsed into fields. remote_class is remote | hybrid | onsite | unknown. excerpt is the first 160 characters of the cleaned posting text; hn_url resolves to the full original comment.

Columns: `posting_id`, `thread_month`, `year`, `month`, `author`, `company`, `role_title`, `location_raw`, `remote_class`, `remote_scope`, `seniority`, `salary_min`, `salary_max`, `salary_currency`, `visa_sponsorship`, `relocation`, `n_technologies`, `n_roles`, `excerpt`, `hn_url`

### `hn_posting_technologies.csv`

Bridge table: which technologies each posting mentions. Join to hn_technologies. A posting appears once per technology it names.

Columns: `posting_id`, `thread_month`, `technology_id`

### `hn_posting_roles.csv`

Bridge table: which roles each posting advertises. Join to hn_roles.

Columns: `posting_id`, `thread_month`, `role_id`

### `hn_monthly_summary.csv`

Monthly totals and work-arrangement mix. Shares are of that month's postings. One row per thread; all 186 months are present with no gaps.

Columns: `thread_month`, `year`, `month`, `n_postings`, `n_remote`, `n_hybrid`, `n_onsite`, `n_unknown`, `share_remote`, `share_hybrid`, `share_onsite`, `share_unknown`

### `hn_yearly_summary.csv`

Yearly rollup of the same figures. is_partial_year = 1 for 2011 (starts in April) and 2026 (9 of 12 months); never compare those against a full year without saying so.

Columns: `year`, `is_partial_year`, `n_postings`, `n_remote`, `n_hybrid`, `n_onsite`, `n_unknown`, `share_remote`, `share_hybrid`, `share_onsite`, `share_unknown`

### `hn_technology_monthly.csv`

How often each technology is named, by month. share_of_postings is n_postings divided by all postings in that month - a technology's share of job ads, which is not its share of jobs. Rows with a zero count are omitted.

Columns: `thread_month`, `technology_id`, `n_postings`, `share_of_postings`

### `hn_technology_yearly.csv`

How often each technology is named, by year. share_of_postings is n_postings divided by all postings in that year - a technology's share of job ads, which is not its share of jobs. Rows with a zero count are omitted.

Columns: `year`, `technology_id`, `n_postings`, `share_of_postings`

### `hn_role_monthly.csv`

How often each role appears, by month, on the same basis as the technology tables.

Columns: `thread_month`, `role_id`, `n_postings`, `share_of_postings`

### `hn_role_yearly.csv`

How often each role appears, by year, on the same basis as the technology tables.

Columns: `year`, `role_id`, `n_postings`, `share_of_postings`

### `acs_commute_state.csv`

Census ACS 1-year table B08006, workers 16+ by means of transportation to work, by state. measure='wfh' is the worked-from-home line - the remote work series. margin_of_error is the published 90% MOE. There is NO 2020: the Census Bureau never released a standard 1-year file for it, so no row exists rather than a zero or an estimate.

Columns: `year`, `geo_code`, `geo_name`, `geo_fips`, `geo_kind`, `measure`, `estimate`, `margin_of_error`, `share_of_workers`

### `acs_wfh_by_occupation.csv`

Census ACS 1-year table B08124: worked-from-home crossed with broad occupation group, by state. This is the table that shows which kinds of work actually went remote. Same 2020 gap as acs_commute_state.

Columns: `year`, `geo_code`, `geo_name`, `occupation_group`, `occupation_label`, `total_workers`, `total_moe`, `wfh_workers`, `wfh_moe`, `wfh_share`, `wfh_share_moe`

### `indeed_remote_monthly.csv`

Indeed Hiring Lab monthly trackers, 2019-01 to 2026-07. remote_share_postings = percent of job postings whose text mentions remote or hybrid work (employer demand). remote_share_searches = percent of job searches using a remote term (worker demand). ai_share_postings = percent of postings mentioning generative-AI terms. Values are PERCENTAGES (2.48 means 2.48%), not proportions.

Columns: `month`, `country_code`, `country_name`, `remote_share_postings`, `remote_share_searches`, `ai_share_postings`

### `indeed_remote_by_sector_monthly.csv`

The same remote-share-of-postings measure, split by Indeed's 53 occupational sectors. is_tech_sector marks the 10 technical sectors - software development, IT systems, IT infrastructure and support, data and analytics, scientific R&D, and the five engineering and architecture sectors; for software work specifically filter to sector_code IN ('techsoftware','techinfo','techhelp','math'). Percentages, not proportions. Not every sector is published for every country - the US has 32 of the 53 - and a series simply stops where Indeed stopped it.

Columns: `month`, `country_code`, `country_name`, `sector_code`, `sector_label`, `is_tech_sector`, `remote_share_postings`

### `indeed_sectors.csv`

Indeed's occupational sector codes with readable labels, and the last month each one has US data for.

Columns: `sector_code`, `sector_label`, `is_tech_sector`, `last_month_us`

### `oews_state_occupation.csv`

BLS Occupational Employment and Wage Statistics by state and occupation, May 2018 to May 2024. occ_group is total (00-0000), major (XX-0000) or detailed - summing across levels double-counts. The flag_* columns carry the BLS marker for that cell: '**' estimate not released, '*' wage not available, '#' wage AT OR ABOVE the top code (a censored HIGH value, not missing - $208,000/yr for 2018-2021, $239,200/yr from 2022), '~' below 0.005%. A flagged cell has a NULL value; treating '#' as missing biases high-wage occupations downward.

Columns: `year`, `area_fips`, `area_title`, `state_abbr`, `area_kind`, `occ_code`, `occ_title`, `occ_group`, `total_employment`, `jobs_per_1000`, `location_quotient`, `annual_median_wage`, `annual_mean_wage`, `hourly_median_wage`, `flag_employment`, `flag_annual_median`, `flag_annual_mean`, `flag_hourly_median`

### `oews_national_occupation.csv`

The same OEWS measures for the United States as a whole, including the minor and broad SOC levels the state files omit. Use it as the denominator for national shares and location quotients.

Columns: `year`, `occ_code`, `occ_title`, `occ_group`, `total_employment`, `annual_median_wage`, `annual_mean_wage`, `hourly_median_wage`, `flag_employment`, `flag_annual_median`, `flag_annual_mean`, `flag_hourly_median`

### `oews_metro_occupation.csv`

OEWS for 219 metropolitan statistical areas: the largest 200 by 2024 employment, plus every metro that is top-25 nationally for any detailed computer/mathematical occupation, so tech hubs are never cut. is_it_occupation marks the 39 detailed 15-xxxx occupations. Coordinates come from the Census gazetteer.

Columns: `year`, `cbsa_code`, `metro_title`, `component_states`, `latitude`, `longitude`, `occ_code`, `occ_title`, `occ_group`, `is_it_occupation`, `total_employment`, `annual_median_wage`, `annual_mean_wage`, `flag_annual_median`, `flag_annual_mean`

### `oews_occupations.csv`

Every occupation code appearing in the OEWS files, with the years BLS published it. IMPORTANT: codes are not continuous across the SOC 2010 to SOC 2018 change. Software Developers is 15-1132 + 15-1133 in 2018, the combined 15-1256 in 2019-2020, and 15-1252 from 2021. Use years_published before drawing any multi-year line.

Columns: `occ_code`, `occ_title`, `occ_group`, `soc_major_group`, `years_published`, `is_it_occupation`

## Reading this data honestly

Most of the work in building this went into these distinctions. They are encoded in the data, not left to the reader:

- **Hacker News is a biased sample.** YC-adjacent startups and remote-friendly software companies, not the US labour market. A technology's share of postings is its share of *these employers' ads*, not its share of jobs. Posting volume fell from 10,259 (2021) to 2,644 (2026 partial), so shares are informative and raw counts are sample support, never a hiring indicator.
- **There is no 2020 ACS.** The Census Bureau never released a standard 1-year file. No row exists for it. Do not interpolate across it.
- **BLS suppression is not zero, and top-coding is not missing.** `#` means the wage is at or above the top code ($208,000/yr for 2018-2021, $239,200/yr from 2022) - a censored high value. `*` and `**` mean the estimate genuinely was not released. The `flag_*` columns keep these apart; the value column is NULL for all of them.
- **Occupation codes break across SOC vintages.** Software Developers is 15-1132 + 15-1133 in 2018, the combined 15-1256 in 2019-2020, and 15-1252 from 2021. `oews_occupations.years_published` tells you which years each code exists. A seven-year line through them is wrong.
- **Partial periods are flagged.** 2011 starts in April and 2026 has 9 of 12 months; `is_partial_year` marks both.
- **Survey estimates carry margins of error.** ACS margins are published alongside every estimate. For a difference between two years, combine them as sqrt(moe1^2 + moe2^2) before calling a change real - by that test 47 of 52 states fell between 2021 and 2024, not 50.
- **Do not sum OEWS occupation levels.** total, major and detailed rows overlap by construction.

## Sources and licence

| Source | Licence |
|---|---|
| [BLS OEWS](https://www.bls.gov/oes/) | US Government work, public domain |
| [Census ACS](https://www.census.gov/programs-surveys/acs/) | US Government work, public domain |
| [Indeed Hiring Lab](https://github.com/hiring-lab) | CC BY 4.0 |
| [Hacker News](https://news.ycombinator.com/) via the [Algolia API](https://hn.algolia.com/api) | Public API; see note below |

Released as **CC BY 4.0**, matching the most restrictive input licence.

Posting text is **not** redistributed in full: `hn_postings.excerpt` holds the first 160 characters for identification, and `hn_url` links to the original comment, whose text remains its author's. The structured fields are derived measurements.

## Rebuilding

Every file here is generated, not hand-edited:

```bash
uv run python pipeline/build_kaggle_dataset.py
```

## Publishing to Kaggle

```bash
pip install kaggle
# 1. put your kaggle.json API token in ~/.kaggle/ (chmod 600)
# 2. set your username in kaggle/dataset-metadata.json:
#      "id": "<your-username>/us-remote-work-and-tech-hiring-2011-2026"
kaggle datasets create -p kaggle/
```

To push an update later:

```bash
kaggle datasets version -p kaggle/ -m "Refresh through <month>"
```

Notes for the upload:

- Kaggle validates `keywords` against its own tag vocabulary and rejects the whole upload on an unknown tag, so the metadata ships with a short known-good list. Add more specific tags in the web UI afterwards.
- `us_remote_work.sqlite` is 162 MB and duplicates the CSVs. Delete it before uploading if you would rather keep the dataset small; everything in it is derivable from the CSVs.
- The largest CSV is well under Kaggle's per-file limit, and the whole directory is far below the 20 GB dataset cap.

## Loading

Every table is its own config, so pick the one you want:

```python
from datasets import load_dataset

postings = load_dataset("tatra-labs/us-remote-work-and-tech-hiring", "hn_postings", split="train")
print(postings[0])

yearly = load_dataset("tatra-labs/us-remote-work-and-tech-hiring", "hn_yearly_summary", split="train")
```

Straight to pandas, without the `datasets` library:

```python
import pandas as pd

url = "https://huggingface.co/datasets/tatra-labs/us-remote-work-and-tech-hiring/resolve/main/hn_postings.csv"
df = pd.read_csv(url)
```

The SQLite build carries the same tables typed and indexed, with convenience
views, and is the fastest way to run joins across sources:

```python
import sqlite3
from huggingface_hub import hf_hub_download

path = hf_hub_download("tatra-labs/us-remote-work-and-tech-hiring", "us_remote_work.sqlite", repo_type="dataset")
con = sqlite3.connect(path)
con.execute("SELECT remote_class, COUNT(*) FROM hn_postings GROUP BY 1").fetchall()
```

### Available configs

- `hn_postings`
- `hn_yearly_summary`
- `hn_monthly_summary`
- `hn_technology_yearly`
- `hn_technology_monthly`
- `hn_posting_technologies`
- `hn_role_yearly`
- `hn_role_monthly`
- `hn_posting_roles`
- `hn_technologies`
- `hn_roles`
- `acs_commute_state`
- `acs_wfh_by_occupation`
- `indeed_remote_monthly`
- `indeed_remote_by_sector_monthly`
- `indeed_sectors`
- `oews_state_occupation`
- `oews_metro_occupation`
- `oews_national_occupation`
- `oews_occupations`
- `states`
