# Dataset reference for the frontend

All files live in `data/`. Every file has a `meta` block; read it, it carries the caveats.


## oews_state

- `data/oews_state.json` — 10.89 MB — 259,798 (area,occ) rows across 7 years: 2018=36,897 2019=36,382 2020=36,085 2021=37,580 2022=37,569 2023=37,676 2024=37,609; 54 areas; 967 occupation entries
- `data/oews_national.json` — 0.51 MB — 9,648 occupation rows across 7 years: 2018=1,379 2019=1,329 2020=1,329 2021=1,403 2022=1,402 2023=1,403 2024=1,403; 1,540 occupation entries

### Schema

Both files are columnar. Row i of a year's arrays is one published OEWS row; join via the index arrays, never row position.

data/oews_state.json (top-level keys: meta, years, occupations, values, areas, title_history):
  years: [2018,2019,2020,2021,2022,2023,2024]
  areas: [{fips:"01", title:"Alabama", kind:"state"|"territory", abbr:"AL"}, ...] 54 entries, sorted by FIPS. kind=="territory" for 66/GU, 72/PR, 78/VI only; DC (11) is kind "state" (matches OEWS AREA_TYPE 2).
  occupations: [{code:"15-1252", title:"Software Developers", group:"total"|"major"|"detailed", years:[2021,2022,2023,2024]}, ...] 967 entries sorted by (code, group). title = MOST RECENT year's published title. years = exactly the years that code+group was published. IMPORTANT: the index key is (code, group), not code alone.
  values: { "<year>": {
      o: [int]  -> index into occupations[]
      s: [int]  -> index into areas[]
      tot_emp:      [int|null]
      jobs_1000:    [float|null]   (3 dp, as published)
      loc_quotient: [float|null]   (2 dp, as published)
      a_median:     [int|null]
      a_mean:       [int|null]
      h_median:     [float|null]   (2 dp, as published)
      flags: { "<field>": { "<marker>": [row indices] } }   markers: "**" "*" "#" "~"
    } }
    All arrays in a year have identical length. Every index in flags points at a row whose value in that field is null.
  title_history: {"<occ_code>": {"<year>": "<title>", ...}}  present only for the 55 state codes whose published title changed across years.

data/oews_national.json (top-level keys: meta, years, occupations, values, title_history, duplicate_codes):
  Same shape MINUS `areas`; values[year] has NO `s` column (single area, FIPS 99 "U.S.") and NO jobs_1000 / loc_quotient columns (BLS publishes those blank/absent in the national files — omitted rather than emitting all-null arrays).
  values[year] columns: o, tot_emp, a_median, a_mean, h_median, flags.
  occupations: 1,540 entries, groups total/major/minor/broad/detailed (state files carry only total/major/detailed).
  duplicate_codes: {"<code>": {"<group>": [years]}} — the 15 codes BLS publishes under more than one o_group. Do not sum both a broad and detailed entry for these.

meta (both files): source, level, urls{year->zip URL}, row_order, null_means, flag_legend{"**":"estimate not released","*":"wage estimate not available","#":"wage at or above the BLS top-code","~":"value less than 0.005 percent"}, precision{field->"integer"|"N decimals"}, covid_years:[2020], covid_note, soc_vintage{year->"SOC 2010"|"SOC 2018"}, soc_note, occupation_key.

### Coverage

Complete: all 7 requested years (2018-2024) x both levels (state + national) downloaded and parsed. No year failed, nothing was skipped, sampled, capped or truncated.

Geographies (state file): 54 areas every year — 50 states + DC (kind "state", 51) + Guam 66, Puerto Rico 72, Virgin Islands 78 (kind "territory", 3). American Samoa (60) and Northern Mariana Islands (69) are NOT published by OEWS; mapping exists in the script if BLS adds them.

Occupations: ALL rows kept, unfiltered — total (00-0000), major (XX-0000) and detailed in the state file; total/major/minor/broad/detailed in the national file. State: 1 total + 22 major + 944 detailed = 967 entries (39 of them detailed 15-xxxx Computer & Mathematical). National: 1 total + 22 major + 96 minor + 477 broad + 944 detailed = 1,540.

Size: oews_state.json is 10.39 MB, comfortably under the 25 MB threshold, so data/oews_state_major.json was NOT written and NOTHING was excluded from any output. The subset-emitting code path exists and is exercised automatically if a future year pushes the file over SIZE_LIMIT_MB (=25.0).

Raw cache: raw/oews/oesm{18..24}{st,nat}.zip, 14 files, ~50 MB total, gitignored, never re-downloaded (verified).

Cross-reference for downstream: of the 290 unique soc_code values in the repo's occupations.csv, 289 appear in oews_national.json and 247 in oews_state.json. The 43 missing from the state file are 36 broad-group + 6 minor-group codes that OEWS simply does not publish at state level, plus 45-3031 (Fishing and Hunting Workers) which appears in neither year of either OEWS file. This is an upstream coverage fact, not a parsing loss.

### Caveats

- OCCUPATION CODES ARE NOT COMPARABLE ACROSS YEARS WITHOUT CARE — the single biggest trap for a time-series view. OEWS uses SOC 2010 for May 2018 and SOC 2018 from May 2019. Worse, several SOC 2018 codes were published only in COMBINED form for 2019-2020 and split from 2021. Verified lineage: software developers = 15-1132 + 15-1133 (2018) -> 15-1256 combined (2019-2020) -> 15-1252 + 15-1253 (2021-2024). Web developers = 15-1257 (2019-2020) -> 15-1254 + 15-1255 (2021+). So 15-1252 has NO 2018/2019/2020 value and a naive 7-year line chart will show a gap, not a decline. occupations[].years states exactly which years each code exists; meta.soc_vintage and meta.soc_note carry the warning in-file.
- The occupation index is keyed by (occ_code, o_group), NOT by occ_code alone. The national files genuinely publish 7-13 codes twice per year — once as o_group "broad" and once as "detailed", with identical values (a broad group whose only detailed occupation shares its code, e.g. 13-1020 Buyers and Purchasing Agents, 31-1120 Home Health and Personal Care Aides). 15 such codes exist across the series; they are listed in oews_national.json.duplicate_codes. Summing all "detailed" rows plus all "broad" rows double-counts these. The parser raises rather than proceeding if (area, occ_code, o_group) is ever non-unique upstream. State files have no such duplicates.
- Suppression and top-coding are distinct and BOTH emit null — do not treat them the same. "**" = estimate not released (hits tot_emp, jobs_1000, loc_quotient together; 8,938 rows in the state file, i.e. ~3.4% of rows have no employment figure). "*" = wage estimate not available. "#" = wage AT OR ABOVE the BLS top-code (>= $115.00/hr / $239,200/yr recently) — this is a censored high value, NOT missing data, and treating it as missing biases wage distributions downward for high-paying occupations (3,119 a_median and 3,066 h_median cells in the state file). The per-field, per-marker row indices are in values[year].flags so the frontend can distinguish them. Nothing was ever coerced to 0; verified 0 zero-valued cells in both files.
- May 2020 (and to a lesser degree May 2021) reflects a COVID-affected reference period: national employment drops 5.3% from 2019 and California drops ~950K. Kept as published and flagged in meta.covid_years / meta.covid_note. Do not read 2019->2020->2021 as a structural shift.
- Numeric values are rounded to the precision BLS publishes at (tot_emp / a_median / a_mean = integer, jobs_1000 = 3 dp, loc_quotient = 2 dp, h_median = 2 dp) to strip Excel float noise. meta.precision records this. No other transformation is applied and nothing derived is stored — jobs_1000 and loc_quotient are carried as published because they are not recomputable from this file.
- State and national totals differ slightly (40 to 6,130 out of ~150M) because BLS rounds each estimate independently. Do not treat the state sum as exactly equal to the national figure.
- State-level OEWS publishes only total/major/detailed o_groups. Broad and minor groups (e.g. 15-1230, 13-1030) exist ONLY in oews_national.json. Any state treemap built on broad groups must aggregate detailed rows itself, using the occupations[].group field.
- 2024 national total employment is 154.19M, above the 144-152M sanity band given in the task. This is the value BLS publishes, confirmed cell-by-cell against oesm24nat.zip — flagging it rather than silently accepting it as in-range.
- SCOPE / COORDINATION NOTE: another agent working in parallel appended a fetch_bls() Playwright helper to pipeline/common.py whose comment asserts every plain HTTP client gets 403 from bls.gov. That was not my experience — all 14 zips at www.bls.gov/oes/special-requests/ downloaded fine over plain httpx with common.py's existing fetch() and its default UA, no retry needed. fetch_oews_state.py therefore uses fetch() (with an alternate-header retry as a fallback) and does NOT need a headed browser. Please do not 'upgrade' it to fetch_bls(). I did not modify common.py. Also note raw/oews/ is shared: the metro agent writes oesm{YY}ma.zip there alongside my oesm{YY}st.zip / oesm{YY}nat.zip; filenames do not collide.
- American Samoa (60) and Northern Mariana Islands (69) are absent because OEWS does not publish them, not because they were filtered. Guam/PR/VI are included and marked kind="territory" so they can be excluded from national aggregates.

## oews_metro

- `data/oews_metro.json` — 6.37 MB — 268,960 upstream MSA rows kept out of 1,021,525 read; flattened to 50,441 series rows (219 metros x up to 241 occupations) x 7 years x 3 metrics = 1,059,261 value slots, plus a 241-row national block

### Schema

Top-level: {meta, years[7], occupations[241], metros[219], series[219], national}.

years = [2018,2019,2020,2021,2022,2023,2024]. Every per-year array in the file is 7 long and aligned to it.

occupations[i] = {c: SOC code, t: title, g: "total"|"major"|"broad"|"detailed", y: [0|1 x7] per-year presence, it: 1 for detailed 15-xxxx}.

metros[i] = {a: 5-digit CBSA/NECTA code, t: current title, s: [state abbrs], ps: BLS primary state, lat, lon (floats, 4dp, never null - all 219 geocoded), g: "c"|"l"|"p" geo source, x?: [partner area codes across the 2024 re-delineation], yt?: {"YYYY": older title} only where the title changed}.

series[i] aligned to metros[i] = {o: [occupation indexes], e: [[7 ints|null]], m: [[7]] annual median USD, w: [[7]] annual mean USD, tc?: [[row, year_index, metric]] where metric 0=median 1=mean}. All values are int or null - no floats, no zeros, no leaked BLS markers.

national = {o, e, m, w, tc} - same shape as one series entry (tc is NEW this build), indexed into the same occupations[].

meta = {title, source, national_source, gazetteer_cbsa[], gazetteer_place, generated, years, reference_period, metro_rule, crosswalk, occ_rule, soc_warning, soc_breaks[6], suppression, topcode{...}, units{e,m,w,tc}, fields{}, geo{}, counts{}, per_year_rows_in{}, per_year_areas_in_file{}}.

NEW meta.topcode = {hourly: [100,100,100,100,115,115,115], annual: [208000,208000,208000,208000,239200,239200,239200], applies_to, derivation, mean_note}. Both arrays are aligned to years[], so a tc triple's year_index indexes them directly. metric 0 (median) means the true value is >= topcode.annual[year_index]; metric 1 (mean) asserts NO value bound.

NEW meta.soc_breaks[k] = {from, to, retired, added, retired_15xxxx[], added_15xxxx[]} - measured code churn per consecutive year pair.

NEW meta.counts keys: metros_kept_by_size_rule_m1 (200), metros_kept_retired_codes_m1b (13), metros_ranked_in_latest_year (393), metro_employment_cutoff_year (2024), topcoded_wage_cells_national (3).

### Coverage

Years 2018-2024 (May reference period), all seven present, no gaps.

Geography: 219 metro areas kept of 429 published across the seven MSA workbooks (was 207). Selection = M1 top 200 by 00-0000 employment in 2024 alone (cutoff 102,720 jobs, a true 2024 cutoff) + M1b 13 area codes retired before 2024 whose last published total clears that same cutoff + M2 5 rescued for being top-25 in a detailed 15-xxxx occupation in some year + M3 1 crosswalk half. Includes Puerto Rico (41980 San Juan) and, through 2023, the New England NECTAs. Kept metros = 121,973,540 jobs in 2024 = 79.1% of the US total. 204 of the 219 have 2024 data; 203 have 2018-2023 data; the gap is the 2024 OMB re-delineation, which retired every NECTA and issued new codes.

Occupations: 241 kept = 00-0000 + 22 major groups + all 40 15-xxxx codes the metro files publish in any SOC vintage (39 detailed) + the 150 largest detailed occupations of each year unioned (194). 725 metro-published codes are deliberately dropped for the size budget.

Missing on purpose: the BOS nonmetro workbook is never read; only AREA_TYPE==4 rows are kept (the column is absent in 2018, whose 395 areas were verified against the typed years - 0 unexplained). Wage percentiles other than the median/mean are not emitted.

### Caveats

- ALL THREE VERIFIER FINDINGS WERE REAL. I confirmed each independently from the raw workbooks before changing anything, then fixed the root cause in pipeline/fetch_oews_metro.py and re-ran the whole script end to end.
- FINDING 1 (top code) - CONFIRMED and fixed. Max published hourly percentile per year: 2018 $99.98, 2019 $99.99, 2020 $99.99, 2021 $99.99, 2022 $114.93, 2023 $114.95, 2024 $114.97. So the cap is $100.00/hr = $208,000/yr for 2018-2021 and $115.00/hr = $239,200/yr for 2022-2024. 2022-2024 publish annual medians up to $239,130 outright (454-495 published medians per year exceed $208,000), so '#' there cannot have meant '>= 208,000'. FIX: the script now DERIVES the cap per year from the data (ceil of the max published hourly percentile, x2080) instead of hard-coding it, prints the derivation with two cross-checks, and ships meta.topcode.hourly/.annual as 7-element arrays aligned to years[]. 349 median tc cells in 2022-2024 now carry the correct $239,200 label.
- FINDING 1b (the mean) - CONFIRMED, and it is worse than a wrong number: the mean is not censored at the top code at all. Published annual means reach $291,360 (2018), $411,230 (2021), $1,805,790 (2023), $618,840 (2024) - far above either cap. So no lower bound is provable for a '#' mean. In six of seven years every mean '#' sits in a row whose entire percentile distribution (p10 through p90) is also '#', but 2022 has 21 rows with a '#' mean beside a PUBLISHED median of $211,610-$237,620, which breaks even that weaker story. FIX: metric-1 (mean) tc entries are now emitted as position flags with NO value bound, and meta.topcode.mean_note states this with the per-year evidence.
- FINDING 2 (metro ranking) - CONFIRMED and fixed, with one correction to the verifier's arithmetic. It was 12 retired codes occupying M1 slots, not 15: Manchester NH and Waterbury CT arrived via the crosswalk closure (M3) and California-Lexington Park via the IT rule (M2), not via the size rule. The displacement count of 12 was exactly right. FIX: M1 now ranks strictly on the latest year (only the 393 areas with a 2024 00-0000 value compete), giving a genuine 2024 cutoff of 102,720; retired codes moved to a new, separately counted rule M1b (kept when their last published total clears that same cutoff). All 12 displaced metros are now in the file and all 207 previously kept metros are still kept - nothing was traded away. The script now also asserts and prints that zero dropped areas sit above the cutoff.
- FINDING 3 (SOC vintage) - CONFIRMED and fixed. Measured code churn at metro level: 2018|2019 -98/+79, 2019|2020 -0/+2, 2020|2021 -33/+74, 2021|2022 -1/+3, 2022|2023 -2/+0, 2023|2024 -4/+1. Every 2010-SOC computer/math code (15-1111...15-1199, 15-2090) appears in 2018 and in no later year; 15-1211/15-1256/15-1245/15-2098 appear from 2019. So 2018 is the only 2010-SOC year, 2019-2020 are 2018 SOC with aggregated codes, 2021-2024 are 2018 SOC split, and 2019|2020 is not a vintage break at all. FIX: meta.soc_warning rewritten to describe the metro vintages, with the churn numbers built from the data at runtime, plus a new machine-readable meta.soc_breaks so the note cannot go stale.
- BONUS FIX (not flagged): meta.fields called the national block 'the same shape as series[i]' while it silently dropped its own top-code markers. The national medians for Physicians All Other are top-coded too. national now carries its own tc list (3 entries within the 241-code menu; 91 exist across the full national files, the rest on occupations outside the kept menu).
- The two halves of a re-delineated place (71650 Boston-Cambridge-Nashua 2018-2023 vs 14460 Boston-Cambridge-Newton 2024) are deliberately NOT spliced - the county sets differ, so a level shift across the join is real. metros[].x links them and both share one map dot.
- M1b keeps 13 retired codes; the other 23 retired codes fall below the cutoff and are left to the IT and crosswalk rules. This is printed line by line with kept/below for every one.
- 'Largest detailed occupations' is ranked on employment summed over all OEWS metro areas, suppressed cells skipped. That sum is a ranking input only and is never emitted.
- Output grew from 6.03 MB to 6.37 MB (12 more metros). Budget is ~9 MB, so no tightening was needed and the IT occupations were untouched.

## acs_wfh

- `data/acs_wfh_state.json` — 0.06 MB — 53 geos x 7 years = 371 geo-year slots; 318 populated (53 null = the 2020 gap). Each populated slot carries 8 estimates + 8 MOEs + share + share_moe.
- `data/acs_wfh_occupation.json` — 0.12 MB — 53 geos x 7 years x 6 occupation groups = 2,226 slots; 1,908 populated (318 null = the 2020 gap). Each populated slot carries total_e/total_m/wfh_e/wfh_m/share/share_moe, plus an all_occupations rollup per geo-year.

### Schema

BOTH FILES share: meta{...}, years:[2018,2019,2020,2021,2022,2023,2024] (fixed 7-slot axis matching common.YEARS; index 2 = the 2020 hole), geos:[{code,name,fips,kind}] with 53 entries, geos[0] = {code:"US",name:"United States",fips:"00",kind:"nation"} then 52 state-equivalents in FIPS order (50 states + DC + PR, kind:"state").

acs_wfh_state.json
  "measures": ["total","wfh","drove_alone","carpooled","public_transit","bicycle","walked","other"]  (8, fixed order)
  "e":         {GEOCODE: [ yearSlot x 7 ]}  where yearSlot is either null (year unavailable) or an 8-element array of ints/null parallel to `measures`.
  "m":         same shape, margins of error (90% MOE, ints).
  "share":     {GEOCODE: [ 7 floats-or-null ]}   = wfh/total, 6dp.
  "share_moe": {GEOCODE: [ 7 floats-or-null ]}   = derived-proportion MOE in share units (multiply by 100 for pp).
  Example: e["AL"][0] = [2068020,71400,1784690,162698,6983,1451,22416,18382]; e["AL"][2] = null.
  meta: {generated_utc, program, years, years_present:[2018,2019,2021,2022,2023,2024], years_missing:{"2020":"<reason>"}, year_sources:{"<year>":{status,source,reason?}}, source_urls:{table_based_sf:{...},legacy_sequence_sf:{...},api_census_gov:{}}, suppression, download_note, notes:[...], table:"B08006", table_title, measures:[{key,description,resolved_label_by_year,column_id_by_year,moe_column_id_by_year}], derived:{share,share_moe}, layout}

acs_wfh_occupation.json
  "occupations": [{key,label}] x6, fixed order:
     management_business_science_arts | service | sales_office |
     natural_resources_construction_maintenance | production_transportation_material_moving | military_specific
  "total_e","total_m","wfh_e","wfh_m","share","share_moe":
     {GEOCODE: [ yearSlot x 7 ]} where yearSlot is null or a 6-element array parallel to `occupations`.
     total_* = all workers in that occupation group (every commute mode); wfh_* = that group's "Worked from home" cell.
  "all_occupations": {GEOCODE: {total_e:[7], total_m:[7], wfh_e:[7], wfh_m:[7], share:[7], share_moe:[7]}}
     = B08124 line 1 (all-occupation total) and the "Worked from home:" block header, scalars per year.
  meta: same common block plus table:"B08124", occupation_groups:[{key,label,total_column_id_by_year,wfh_column_id_by_year}], wfh_block:{resolved_label_by_year,column_id_by_year}, all_occupations_total_column_by_year, derived, layout.

EXACT COLUMN IDS USED (also in meta, per year):
  2021-2024 (table-based SF): B08006_E001 total, _E017 wfh, _E003 drove alone, _E004 carpooled,
    _E008 public transportation, _E014 bicycle, _E015 walked, _E016 other; MOEs = same numbers with _M.
    B08124_E001 total, _E002.._E007 occupation-group totals, _E043 WFH block, _E044.._E049 WFH by group.
  2018 (legacy seq SF): B08006 seq 0029 start col 7 -> lines 1/17/3/4/8/14/15/16 = e-file cols 7/23/9/10/14/20/21/22;
    B08124 seq 0032 start col 42 -> lines 1-7 = cols 42-48, line 43 = col 84, lines 44-49 = cols 85-90.
  2019 (legacy seq SF): identical line numbers, B08006 seq 0028, B08124 seq 0031 (same start cols/offsets).
  Fallback path (only if a file load fails AND CENSUS_API_KEY exists): api.census.gov names B08006_017E / B08006_017M.

### Coverage

YEARS: 2018, 2019, 2021, 2022, 2023, 2024 all fully present. 2020 is the only gap.
  - 2018, 2019 backfilled successfully via BACKFILL OPTION 1 (legacy sequence-based summary file). This is the option that worked; option 2 (api.census.gov) was never needed - no CENSUS_API_KEY exists in the environment or in a .env (there is no .env in the repo). The API path is implemented and its structure resolver was smoke-tested against the public groups endpoint (it resolves to the same lines 17/43), but no API data call was made.
  - 2020: documented gap. The Census Bureau published no standard ACS 1-year estimates for 2020 (pandemic data-collection failure). Verified directly: https://www2.census.gov/programs-surveys/acs/summary_file/2020/data/ contains only 5_year_* directories and 1_year_quality_measures, and .../2020/table-based-SF/data/1YRData/acsdt1y2020-b08006.dat returns 404. Emitted as null everywhere, never interpolated.

GEOGRAPHIES: 53 - the US total row plus all 52 state-equivalents (50 states + District of Columbia + Puerto Rico). Complete in every present year; no geography is missing in any year.

OCCUPATION GROUPS (verified from the actual shells, not assumed): SIX, not the five listed in the task. The shells carry a sixth group, "Military specific occupations", which is kept as published:
  management_business_science_arts, service, sales_office,
  natural_resources_construction_maintenance, production_transportation_material_moving, military_specific.

MEASURES: total workers 16+, worked from home, drove alone, carpooled, public transportation, bicycle, walked, other (taxi/motorcycle/other). MOE carried for every one of them, in every year and geography (the legacy m-files and the table-based _M columns are both complete).

DENSITY: 318/318 state geo-years and 1,908/1,908 occupation geo-year-groups are non-null. Zero cells were dropped, sampled, capped or truncated.

### Caveats

- 2020 has no ACS 1-year data and is emitted as an explicit null year-slot in both files (years array still lists it so the axis stays parallel to common.YEARS). The 2019 -> 2021 step therefore spans two survey years, not one - a chart must show a gap, not a straight line.
- Series break in the source product: 2018-2019 come from the legacy sequence-based summary file, 2021-2024 from the table-based summary file. The B08006/B08124 line structure was verified identical line-by-line across all six vintages, so the numbers are comparable, but the file format, sequence numbering and some sub-line wording differ.
- Upstream label drift is real and is why nothing is hardcoded: 2018 says 'Worked at home', 2019+ says 'Worked from home'; 2024 renamed 'Public transportation (excluding taxicab):' -> 'Public transportation:' and 'Taxicab, motorcycle, or other means' -> 'Taxi or ride-hailing services, motorcycle, or other means'; 2018's transit sub-lines are also worded differently ('Bus or trolley bus' vs 'Bus'). Every measure is resolved by label match against that year's own shells/lookup and asserted; the WFH assertion (label must contain 'worked from home' or 'worked at home') is hard-fail. The per-year resolved label and column id are recorded in meta.
- SUPPRESSION: the parser maps blank cells, the textual markers (* ** *** ***** # ~ . - N (X) NA) and the Census negative jam sentinels (<= -100000000, e.g. -555555555) to null, never 0, and null never enters a sum or a share. In this particular extract NOTHING was actually suppressed: 0 blank/marker cells and 0 jam cells across 34,961 upstream rows scanned. There are 54 zero cells in wfh_e, all in 'military_specific' in small states - these are genuine published zero estimates, not suppression.
- 'Military specific occupations' is a real B08124 group (the task listed only five). It is kept as published rather than dropped or folded in; its counts are tiny and often 0, so its share is noisy and its MOE frequently exceeds the estimate.
- ACS 'worked from home' is a means-of-transportation-to-work category: the respondent did not commute during the reference week because they worked at home. It is a PRIMARY-mode measure, not a count of hybrid or occasional remote workers, so it undercounts hybrid arrangements. Universe is workers 16 years and over. Geography is place of RESIDENCE, not place of work.
- share_moe is derived, not published: the ACS derived-proportion formula MOE_p = sqrt(MOE_wfh^2 - p^2*MOE_total^2)/total, falling back to the derived-ratio form (+ instead of -) when the radicand is negative. It is in share units - multiply by 100 for percentage points.
- Download quirk worth knowing before a re-run: www2.census.gov's WAF returns HTTP 200 with a 247-byte 'Request Rejected' HTML body for some requests. Two are DETERMINISTIC false positives on literal paths (.../NewYork/20181ny0029000.zip and .../Nebraska/20191ne0031000.zip) - they never succeed bare, but serve the correct zip when a '?r=N' query string is appended. The plain disk cache would have cached those 247-byte pages forever and silently lost 2018 and 2019, so fetch_verified() validates every payload (zip magic / non-HTML), deletes bad ones, and retries with the query-string form. This is documented in meta.download_note.
- raw/acs/ is 72 MB across 232 cached files (212 legacy per-state sequence zips + 4 years of table-based .dat/shells/geo + 2 lookups + 2 mini-geo xlsx). It is gitignored and never re-downloaded; a cold run takes a few minutes, a warm run ~4 seconds.
- The occupation groups are broad ACS/SOC major-group rollups. They do NOT map 1:1 to the 342 OOH occupations in occupations.csv - joining them to soc_code requires a major-group crosswalk and will be lossy.
- PR is included as a state-equivalent. It is excluded from ACS national controls, so US totals equal the sum of the 50 states + DC exactly (verified: diff = 0 in every year) and PR must be excluded from any 'sum the states' aggregation.

## indeed

- `data/indeed_remote.json` — 1.18 MB — 180,207 numeric/null leaf values; 91 months x (24 country-series + 249 country-sector series) monthly + 2,769-day daily for 24 country-series and 32 US sectors
- `data/indeed_postings.json` — 0.19 MB — 21,460 numeric/null leaf values; 79 months x (94 sector-variable series + 51 state series + 4 national series) + 2,401-day national daily

### Schema

Both files are columnar/indexed: index arrays (`months`, `dates`, `sectors`, `states`, `countries`) plus value arrays positionally aligned to them. All values are numbers or null; null means "series absent upstream", never zero.

=== data/indeed_remote.json (unit: percent of postings/searches, 0-100; 3 dp) ===
{
 "meta": {dataset, unit, aggregation:"month_mean",
          sources:{remote_postings,remote_postings_sector,remote_searches,ai_postings,readme_remote_tracker,readme_ai_tracker} (raw.githubusercontent URLs),
          min_date:{remote_postings,remote_searches,ai_postings,remote_postings_sector},
          max_date:{same 4 keys},           // max date present per series, in lieu of a retrieval date
          notes:[str,...],                   // 5 data-shape caveats
          methodology:{publisher,license,smoothing,remote_definition,remote_searches_definition,
                       remote_methodology_note,ai_definition,postings_index_base,
                       postings_index_seasonal_adjustment,METHODOLOGY_CHANGE,sector_taxonomy,
                       refresh_cadence,aggregation,precision,branches_used,changes_feed,faq},
          daily_included:["dates","daily","sector_daily"], excluded_for_size:[str], size_budget_mb:3.0},
 "countries": {"codes":["AU","CA","DE","FR","GB","IE","IT","JP","NL","US"],   // 10, union of all 3 series
               "names":["Australia",...,"United States"]},                    // parallel to codes
 "months":     ["2019-01",...,"2026-07"],        // 91
 "month_days": [31,28,...],                      // observed days averaged into each month
 "partial_months": [],                           // empty: no partial months in this file
 "sectors": [{"code":"accounting","label":"Accounting","label_source":"current","tech":false}, ...],  // 53
             // label_source ∈ "current" (43) | "legacy" (2) | "inferred" (8); tech=true for 11 IT/STEM codes
 "monthly": {"remote_postings":{"US":[91 floats],..., "IT":null,...},   // key = country code, ALL 10 present
             "remote_searches":{...}, "ai_postings":{...}},             // value null if country absent from series
 "sector_monthly": {"US":[53 entries],"AU":[...],...},   // 7 countries; array aligned to sectors[] index
                                                          // entry = [91 floats] or null (sector absent for country)
 "dates": ["2019-01-01",...,"2026-07-31"],       // 2769, contiguous, no gaps
 "daily": {"remote_postings":{"US":[2769 floats],...,"IT":null}, "remote_searches":{...}, "ai_postings":{...}},
 "sector_daily": {"US":[53 entries]}             // US only (size budget); entry = [2769 floats] or null (32 non-null)
}

=== data/indeed_postings.json (unit: index, Feb 1 2020 = 100; 2 dp) ===
{
 "meta": {dataset, unit, aggregation:"month_mean",
          sources:{aggregate,by_sector,by_state,sector_job_title_examples,readme},
          min_date:{aggregate,by_sector,by_state}, max_date:{same},
          notes:[str,...],                  // 8 caveats incl. the master-branch gotcha
          methodology:{...same 17-key object as above...}},
 "months":     ["2020-02",...,"2026-08"],   // 79
 "month_days": [29,31,...,28],              // last month = 28 of 31 days
 "partial_months": ["2026-08"],
 "variables": {"total":"total postings (all live postings)",
               "new":"new postings (on Indeed for 7 days or fewer)"},
 "sectors": [{"label":"Accounting","job_title_examples":"accountant, financial analyst"}, ...],  // 47
             // job_title_examples is null for the 6 labels the stale examples CSV lacks
 "states":  ["AK","AL",...,"WY"],           // 51 = 50 states + DC, uppercased (source is lowercase)
 "national_monthly": {"total":{"sa":[79 floats],"nsa":[79 floats]}, "new":{"sa":[...],"nsa":[...]}},
 "national_daily":   {"dates":["2020-02-01",...,"2026-08-28"],   // 2401, contiguous
                      "total":{"sa":[2401],"nsa":[2401]}, "new":{"sa":[...],"nsa":[...]}},
 "sector_monthly": {"total":[47 arrays of 79], "new":[47 arrays of 79]},  // aligned to sectors[] index
 "state_monthly":  [51 arrays of 79]                                      // aligned to states[] index
}

### Coverage

SOURCES / BRANCHES (verified via GitHub API):
- hiring-lab/remote-tracker -> main
- hiring-lab/ai-tracker -> main
- hiring-lab/job_postings_tracker -> **master, NOT main** (the task brief's `main` 404s). This is the single most important deviation from the brief.

ROWS IN: 1,108,884 upstream CSV data rows across 7 data files (+4 README/label files).
  remote_postings 19,383 | remote_searches 22,152 | AI_posting 24,921 |
  remote_postings_sector 689,481 | aggregate_US 4,802 | by_sector_US 225,694 | state_US 122,451

GRID A (remote-tracker + ai-tracker), monthly 2019-01..2026-07 (91 months), daily 2019-01-01..2026-07-31:
  2,769 days, contiguous, ZERO gaps, ZERO partial months.
  Country coverage DIFFERS per series (brief said "7 countries" for all — only remote_postings is 7):
    remote_share_postings : 7  = AU CA DE FR GB IE US
    remote_share_searches : 8  = the above + JP
    AI_share_postings     : 9  = AU CA DE FR GB IE US + IT + NL (no JP)
  Union of 10 country codes is emitted; a country absent from a series is null there.

GRID B (job_postings_tracker), monthly 2020-02..2026-08 (79 months), daily 2020-02-01..2026-08-28:
  2,401 days, contiguous, ZERO gaps. 2026-08 is PARTIAL (28 of 31 days) and flagged in partial_months.
  47 occupational sectors x {total, new}; 51 state units (50 + DC; no Puerto Rico, no national row).
  Series starts at the Feb 1 2020 index base — there is no pre-2020 postings history upstream.

PER-YEAR COVERAGE — 100% non-null in every year, every series, no holes:
  remote country  2019..2025: 288/288 each; 2026: 168/168
  remote sector   2019..2025: 2,988/2,988 each; 2026: 1,743/1,743
  postings sector 2020: 1,034/1,034; 2021-25: 1,128/1,128 each; 2026: 752/752
  postings state  2020: 561/561; 2021-25: 612/612 each; 2026: 408/408

MAX DATE PRESENT (recorded in meta.max_date, since no clock is available):
  remote_postings / remote_searches / ai_postings / remote_postings_sector = 2026-07-31
  aggregate / by_sector / by_state = 2026-08-28

SECTOR COVERAGE: 53 distinct remote-tracker codes across the 7 countries; per country
  AU 44, CA 46, DE 42, FR 26, GB 36, IE 23, US 32. Every present (country, sector) pair has all
  2,769 days — presence is strictly all-or-nothing, so no partial sector histories exist.

DELIBERATELY NOT INCLUDED: metro_job_postings_us.csv (62 MB, CBSA level) — out of scope, not downloaded.
GenAI_posting.csv — documented in the ai-tracker README but ABSENT from the repo, so no GenAI series exists.

### Caveats

- BRIEF CORRECTION — job_postings_tracker's default branch is `master`, not `main`. The brief's base URL pattern 404s for all four of its files. Verified via https://api.github.com/repos/hiring-lab/job_postings_tracker. remote-tracker and ai-tracker do use `main`. Branches used are recorded in meta.methodology.branches_used.
- BRIEF CORRECTION — remote_postings_sector.csv is NOT discontinued at 2023-05-26. It runs the full 2019-01-01..2026-07-31 daily span (689,481 rows, 2,769 days for every present country-sector pair), identical to the country-level files. There is no truncation to record; the brief's stated end date does not hold for this retrieval.
- BRIEF CORRECTION — the 53 sector codes are the union across all 7 countries. The US carries only 32 of them. There is no country with 53.
- SECTOR LABEL MAPPING IS PARTLY INFERRED. No code->label crosswalk exists anywhere in the hiring-lab org (all 6 repos checked: job_postings_tracker, remote-tracker, ai-tracker, indeed-wage-tracker, pay-transparency, pages — every other product publishes display labels, only remote-tracker publishes raw codes). I attempted a rigorous data-driven crosswalk by matching per-country presence signatures against pay-transparency-sector.csv, but that file is US-only, so the signature trick yields nothing. Labels were therefore matched by stem plus published job-title examples against the union of Indeed's two real label lists. Every sector carries a `label_source` field: 43 "current" (verbatim from job_postings_by_sector_US.csv display_name), 2 "legacy" (sports, veterinary — verbatim from the examples CSV, since dropped from the US index), and 8 "inferred" — agriculture, care, engchem, personal, realestate, service, transport, warehouse. The inferred 8 have no counterpart in either Indeed list or are genuinely ambiguous (notably `care` vs `personal`, split as Personal Care & Home Health vs Beauty & Wellness on the strength of the job-title examples alone). Consumers should not present the 8 inferred labels as authoritative.
- INDEED'S TAXONOMY WAS RENAMED. sector-job-title-examples.csv is a stale vintage: 6 of its labels are gone from the current index (Beauty & Wellness, IT Operations & Helpdesk, Information Design & Documentation, Mathematics, Sports, Veterinary) and 6 current labels are missing from it (Aviation, Data & Analytics, IT Infrastructure Operations & Support, IT Systems & Solutions, Mechanical Engineering, Social Science), for which job_title_examples is null. Three are pure renames that matter for continuity: math = Mathematics -> "Data & Analytics"; techhelp = IT Operations & Helpdesk -> "IT Infrastructure, Operations & Support"; techinfo = Information Design & Documentation -> "IT Systems & Solutions".
- METHODOLOGY CHANGE (recorded in meta.methodology.METHODOLOGY_CHANGE): Indeed adopted a new daily seasonal-adjustment method (Deutsche Bundesbank daily-series methodology) in NOVEMBER 2024 and REVISED HISTORY. Pre-Nov-2024 values may differ from what was published at the time, so this index is not comparable with figures quoted in older Hiring Lab pieces. Projected seasonal factors for the current calendar year come from the preceding three years, so the most recent months are the most revision-prone.
- WHAT "REMOTE" MEANS: Indeed first identifies the job location as remote, then keyword-searches the posting TEXT for remote/hybrid/flexible terms. remote_share is the share of postings that MENTION remote or hybrid work — it is NOT a measure of work actually performed remotely, and it lumps hybrid together with fully remote. It is therefore not directly comparable to the ACS work-from-home supply-side measure. Denominator is all postings (remote and non-remote).
- remote_searches is JOB-SEEKER search text on Indeed, not employer postings. It is a worker-side signal and must not be read as the same quantity as remote_postings.
- EVERY series is a 7-day trailing average applied upstream by Indeed. This pipeline applies no smoothing of its own; the month-mean is taken over already-smoothed daily values.
- The postings index is an INDEX (Feb 1 2020 = 100), not a count. Levels are NOT comparable across sectors or states — only trends are. Each series (national, sector, state) is seasonally adjusted SEPARATELY, so sector and state series do not aggregate to the national one.
- Sectors are an Indeed categorisation built on normalised job titles, NOT SOC or NAICS. They cannot be joined on code to the OOH/OEWS occupations used elsewhere in this project.
- AGGREGATION IS MONTH-MEAN (stated in meta.aggregation on both files), chosen over month-end because the daily panel is already a 7-day trailing average and a month-end snapshot would inherit one day's idiosyncrasy. Missing days are excluded from the mean, never counted as zero; a month with no observed day is null. month_days gives the day count behind each month and partial_months names the incomplete ones (only 2026-08, at 28 of 31 days).
- NO DATA WAS DROPPED, SAMPLED OR CAPPED. Monthly series are complete for every file. The only size-driven exclusion is DAILY detail, applied by an explicit documented ladder against a 3.0 MB budget (REMOTE_JSON_BUDGET_MB) and PRINTED by the script: the full build was 4.80 MB, so per-sector daily series for the 6 NON-US countries were excluded. US per-sector daily (32 sectors x 2,769 days) is retained, as is all country-level daily, landing at 1.13 MB. meta.excluded_for_size and meta.daily_included record this inside the file. Per-sector MONTHLY series are retained in full for all 7 countries — nothing monthly was lost.
- Suppression handling: Indeed publishes no suppression markers and this retrieval contains ZERO non-numeric values and ZERO nulls in all 1,108,884 source rows. The loader nonetheless coerces any non-numeric token (BLS-style *, **, #, ~, blanks) to null — never 0 — excludes it from means, and PRINTS it, so a future upstream change fails loudly rather than silently zeroing a series.
- ROUNDING: shares 3 dp, index 2 dp (the source precision), using Python round() on the true IEEE-754 double. Recomputing month-means with numpy's np.round differs by 0.01 in ~0.5% of index cells at apparent half-way values — e.g. a mean printing as 86.955 is stored as 86.95499999999999829, so 86.95 is correct and np.round's 86.96 is a scale-multiply artifact. Documented in meta.methodology.precision so a re-derivation disagreeing in the last digit is not mistaken for a bug.
- The ai-tracker README documents a GenAI_posting.csv (Generative AI / LLM / ChatGPT keywords), but that file is NOT in the repository as retrieved. No GenAI series is emitted; recorded in meta.methodology.ai_definition.
- The two date grids END ON DIFFERENT DAYS (2026-07-31 vs 2026-08-28) because remote-tracker and ai-tracker refresh monthly while job_postings_tracker refreshes weekly. Any chart overlaying the two must handle the ragged right edge. This is also why they live in two files with separate month indexes rather than one shared grid.
- No retrieval timestamp is recorded (no clock available). meta.max_date on each file gives the maximum date present per series instead, as instructed.
- pipeline/common.py was NOT modified by this task. It shows as modified in git only because a parallel agent appended a BLS/Playwright helper to it.

## hn_jobs

- `data/hn_postings.jsonl` — 37.24 MB — 94548 postings (one JSON object per line), up from 92585 before the fix
- `data/hn_trends.json` — 0.41 MB — 1 object; 186 monthly + 16 yearly buckets across totals, 4 remote classes, 112 techs, 20 roles, 9 seniority tiers, 11 remote scopes, salary median, visa
- `data/hn_taxonomy.json` — 0.04 MB — 112 technology entries + _roles (20) + _qualifier_pattern + _notes (unchanged by this fix)

### Schema

hn_postings.jsonl — one object per line, keys omitted when empty: id (int HN comment id), m ("YYYY-MM"), a (author), rc ("remote"|"hybrid"|"onsite"|"unknown"), rs (remote scope: us|global|eu|same-timezone|... , absent if none), co (company), t (title/role line), loc (location string, <=120 chars), sr (seniority: intern|junior|mid|senior|staff|principal|manager|exec; absent when unknown), r (array of role ids; absent when only "other"), tc (array of technology ids), smin/smax (numbers), cur (currency code), v (bool visa sponsorship), rel (bool relocation), x (first 160 chars of cleaned text; length set by the printed size ladder).

hn_trends.json — {meta, months[186 "YYYY-MM"], years[16 "YYYY"], totals:{month[186],year[16]}, remote:{month_counts:{remote|hybrid|onsite|unknown:[186]}, month_shares:{...}, year_counts:{...}, year_shares:{...}}, remote_scope:{month_counts:{scope:[186]},year_counts:{...}}, seniority:{month_counts:{tier:[186]},year_counts:{...}}, tech:{ids[112], labels{}, categories{}, month_counts{tech:[186]}, month_shares{}, year_counts{tech:[16]}, year_shares{}, month_counts_remote{}, year_counts_remote{}, year_shares_remote{}}, role:{ids[20], labels{}, month_counts{}, month_shares{}, year_counts{}, year_shares{}, month_counts_by_remote{role:{rc:[186]}}, year_counts_by_remote{}}, remote_totals:{month[186],year[16]}, salary_usd_median:{month[186]}, visa:{month_yes[186],month_stated[186]}}. Shares are null (never 0) at a zero denominator. meta now carries threads_fetched, thread_type_counts_seen, months_expected, months_missing (=[]), comments_in, postings_parsed, postings_deleted_or_empty, postings_dropped_unconfirmed (445; RENAMED from postings_non_posting_dropped), postings_dropped_unconfirmed_by_year {YYYY:{comments,dropped,rate}} (NEW), remote_decision_tier, role_source, remote_classifier_accuracy, technology_disambiguation_audit, postings_jsonl_schema, postings_excerpt_chars (160), postings_rows, postings_size_ladder, notes[].

hn_taxonomy.json — {tech_id:{label,category,patterns[],anti_patterns[],ambiguous,requires}} plus reserved keys _roles{role_id:{label,patterns[]}}, _qualifier_pattern, _notes[].

### Coverage

186 monthly "Ask HN: Who is hiring?" threads, 2011-04 .. 2026-09 inclusive, with no gaps (meta.months_missing = []). 94,995 top-level comments read; 2 deleted/empty; 445 unconfirmed by the posting heuristic; 94,548 postings shipped (was 92,585 before this fix). Per-year postings: 2011 1930 (9 months, threads start 2011-04), 2012 2570, 2013 3411, 2014 4304, 2015 7230, 2016 8003, 2017 9406, 2018 9657, 2019 8427, 2020 7074, 2021 10259, 2022 7572, 2023 4151, 2024 3892, 2025 4018, 2026 2644 (9 months, 2026-09 partial). Thread-type split seen in the Algolia author index: hiring 186 / wants_to_be_hired 147 / freelancer 173 / other 4 = 510; only the 186 "Who is hiring?" threads are parsed. 112 technologies, 20 roles, 4 remote classes, 11 remote scopes, 9 seniority tiers. No geography beyond the free-text location string; no salary normalisation beyond USD min/max where parseable.

### Caveats

- FIXED (was the verifier's major finding): the old is_posting() regex had a trailing \b that killed every plural ("engineers", "openings", "roles") and made its "opportunit" alternative unmatchable, and it scanned only text[:600]. It dropped 2,408 comments of which ~87% were ordinary job ads, biased toward onsite. is_posting() is rewritten to scan the whole comment and accept on any of four signals (>=2 pipes / >=2 labelled fields / hiring vocabulary / jobs-careers-ATS URL). Drops fell 2,408 -> 445 (2.5% -> 0.5% of comments); 1,963 real postings recovered; 2011 drop rate 13.5% -> 1.5%, 2014 8.9% -> 0.3%, 2015 5.2% -> 0.5%.
- The 445 remaining drops are NOT called 'non-postings'. meta.postings_dropped_unconfirmed is the count is_posting() could not confirm as a job ad; I hand-read 55 of them and roughly 1/3 are still real ads (~150 corpus-wide, 0.16% of comments) - typically a bare 'City / Company / tech-stack list' with no hiring word at all. Per-year posting counts are therefore a FLOOR, tightest in 2011 (1.5% unconfirmed) and 2026 (1.3%). Per-year rates ship in meta.postings_dropped_unconfirmed_by_year; reproduce with `uv run python pipeline/parse_hn.py --filter-audit 25`.
- The new filter is deliberately recall-tuned, so it admits some non-ads: of 65 hand-read comments that it keeps and the old filter dropped, 6 (9%) are not job ads (thread-tooling plugs, interview complaints). That is ~0.2% of the corpus. This is a conscious trade: at a 97.5% base rate of real ads, a strict filter costs far more true postings than it removes noise.
- Residual bias from the 445 drops is now negligible. Running the shipped remote classifier on them gives remote 2.9% / hybrid 0.7% / onsite 20.7% / unknown 75.7% (vs kept 17.8/24.5/55.8/1.9) - i.e. they are mostly text with no location signal, not onsite ads. Worst case, assuming ALL 445 were postings, 2011 remote+hybrid moves 9.02% -> 8.93% (-0.08pp) and the largest move anywhere is -0.77pp (2024); the old filter moved 2011 by -1.3pp.
- Analysis windows (unchanged by this fix): seniority reads header + full[:1500]; salary reads header + full[:2500] (previously undisclosed); the header line is capped at 400 chars and the header zone at 3 lines / 600 chars; loc and title are truncated to 120 chars. Technology matching runs on the full cleaned text with URLs stripped and no cap.
- Excerpt field 'x' is 160 chars, chosen by the printed size ladder to stay under the 40 MB brief budget (400 chars would be 59.3 MB). Full text stays reproducible from raw/hn/{thread_id}.json via the comment id. Row count grew 2.1%, file 36.6 -> 37.2 MB, still under budget.
- 2026-09 is a live, partial thread (207 top-level comments at fetch time; it has since grown by at least one). 2011 starts at 2011-04, the first thread that exists. Four months' threads come from search backfill rather than the whoishiring author index (authors: _whoishiring x2, Aloisius, lpolovets) - recorded in raw/hn/_threads.json.
- Remote-classifier accuracy is unchanged and re-printed each run: 35/35 on the curated hard-case set, 39/42 = 92.9% on the blind held-out set (the honest generalisation figure). Ambiguous-token false-positive rate after tuning: 1/379 sampled matches (~0.3%), residual concentrated in 'spark' (Spark Java the web framework, ~5% for that token alone).
- Technology and role shares are share-of-postings-that-month, not share-of-mentions: a posting counts once per technology however many times it names it. Shares are null, never 0, where the denominator is 0.

## geo_crosswalk

- `data/geo_states.json` — 0.15 MB — 52 shapes (50 states + DC + PR)
- `data/soc_structure.json` — 0.15 MB — 1447 SOC 2018 codes (23 major / 98 minor / 459 broad / 867 detailed)
- `data/soc_crosswalk.json` — 0.44 MB — 341 resolved OOH occupations + 1 unresolved; 1525 OEWS codes; 900 SOC 2010->2018 code pairs; 18 IT occupations

### Schema

geo_states.json (UNCHANGED by this fix): {viewBox:"0 0 975 610", source:{...}, states:[{fips,abbr,name,path:"M...Z",centroid:[x,y],label_pos:[x,y],bbox:[x0,y0,x1,y1],area_px}]}.

soc_structure.json (UNCHANGED): {vintage:"SOC 2018", source:{url,note}, levels:["major","minor","broad","detailed"], codes:[{code,title,level,parent|null}], major_group:{"11":{code,title},...}}.

soc_crosswalk.json top-level keys: sources, years, notes, counts, occupations, unresolved, soc_to_ooh, category_major_groups, oews_universe, soc_vintage (NEW), oews_only_codes, ooh_without_csv_soc_code, ooh_soc_codes_never_in_oews, ooh_soc_codes_not_in_soc2018, ooh_aggregate_csv_soc_codes, soc_detailed_never_in_oews, it_occupations.

occupations[i] = {slug, title, category, csv_soc_code|null, soc_codes:[SOC], soc_levels:[level|null], method, kind:"direct_detailed"|"direct_aggregate"|"multi_code"|"needs_child_summing", children_detailed:{aggcode:[detailed...]}, oews:{"2018":[codes],...,"2024":[codes]}, oews_gaps:{year:[codes]}, oews_partial:{year:{code:[absent children]}}, oews_years_complete:[int],
  NEW oews_duplicate_rows:{year:[codes]}  -- codes OEWS prints TWICE that year (identical estimates); take ONE row per code, never both;
  NEW soc_vintage_2018:{code:{status,reason,detail?}}  -- only codes whose status != "comparable";
  NEW soc_vintage_break_2018:[codes]      -- subset whose May-2018 value is genuinely not comparable with 2019+}.

oews_universe = {_schema, years:[7 ints], code:[1525], level:[1525], title:[1525], present:[1525 bitmask strings over years],
  CHANGED duplicate_codes:{_schema, codes:{code:{"broad":[years],"detailed":[years]}}}  -- 15 codes; listed when at least one year carries both labels;
  CHANGED level_drift:{_schema, codes:{code:{levels:[...],by_year:{year:level}}}}  -- GENUINE cross-year drift only; currently {} (was 13 bogus entries)}.
level[i] is now the BROADER o_group when OEWS printed a code under two labels in the same year (e.g. 13-1020 is now "broad", was "detailed").

soc_vintage (NEW) = {_schema, sources:{crosswalk,deleted_2010_codes}, n_code_pairs:900, n_pairs_with_a_different_2018_number:148, n_2010_codes:840, n_2018_codes:867, n_2010_codes_renumbered_or_split:106, n_2018_codes_fed_by_multiple_2010:31, crosswalk_2010_to_2018:{c2010:[c2018...]}, deleted_2010_codes:{code:{level,title}} (116), deleted_2010_codes_used_in_may_2018_sums:[] , status_counts:{comparable:448, group_membership_changed:2, oews_combined_code:2, split:1}, n_codes_used_in_may_2018:453, codes_used_in_may_2018:[{code,title,soc_level,used_by:[slugs],status,reason,detail?}] (non-comparable only, 5 entries), oews_title_changes:[{code,title_2018,title_latest,latest_year,used_by:[slugs]}] (39)}.
status vocabulary: "comparable" | "split" | "absorbed" | "renumbered" | "group_membership_changed" | "oews_combined_code" | "no_2010_counterpart". Only the first four (minus "comparable") set soc_vintage_break_2018.

### Coverage

Years 2018-2024 (May OEWS), all 7 national files. Geography: 50 states + DC + Puerto Rico (52 shapes) in a 975x610 Albers-USA viewBox; AS/GU/MP/VI excluded as non-OEWS state-equivalents. Occupations: all 342 rows of occupations.csv; 341 resolved (238 direct_detailed, 51 direct_aggregate, 50 multi_code, 2 needs_child_summing), 1 unresolved (military-careers: the OOH page carries no projections table). All 25 OOH category slugs mapped to SOC major groups. SOC 2018: complete 1447-code hierarchy. OEWS universe: 1525 distinct codes across the 7 years (1373/1316/1316/1396/1395/1396/1396 per year). SOC 2010->2018 bridge: complete 900-pair crosswalk plus all 116 deleted 2010 codes. Per-year OOH joinability unchanged: 2018 298 complete/11 partial/32 none; 2019 and 2020 321/5/15; 2021-2024 340/0/1.

### Caveats

- FIXED (major #1): build_oews_universe() previously read each OEWS file into a {code: row} dict, so the 15 codes BLS publishes twice in the same year (one o_group=broad row and one o_group=detailed row with byte-identical tot_emp) were silently collapsed last-wins. It now records every o_group per (code, year). Consequences fixed: oews_universe.level is deterministic (the broader label; 13-1020 is now 'broad', was arbitrarily 'detailed'); the 15 duplicated codes including 15-2090 and 39-1010 (duplicated only in May 2018 and previously invisible) are listed in oews_universe.duplicate_codes with per-o_group year lists; level_drift no longer mislabels duplication as cross-year drift and is now genuinely empty; the 10 affected OOH occupations carry oews_duplicate_rows[year]. Verified against the raw zip: home-health-aides 2024 = 3,988,140 taking one row per code vs 7,976,280 summing every matching row.
- FIXED (major #2): the note claiming 'BLS publishes no machine-readable 2010->2018 crosswalk on bls.gov/soc/2018/' was false. soc_2010_to_2018_crosswalk.xlsx (46,630 B, md5 8a358b553965e3eb2bde6701d10e6d9e) and soc_2010_codes_deleted_in_2018.xlsx (13,300 B, md5 d8bd2078d6935fca67dc4233ec896f42) are both live at that path and are now downloaded to raw/geo/ and parsed. The note is rewritten; two new notes cover duplicate rows and title changes.
- The 2010->2018 crosswalk is used ONLY to LABEL series. No number is bridged, restated or back-cast across the 2018<->2019 vintage break; the emitted oews[year] sets are byte-for-byte what they were before this fix (independently re-derived: 0 mismatches on oews, oews_gaps, oews_partial, children_detailed, soc_levels for all 341 occupations x 7 years).
- 3 OOH occupations have a May-2018 point that is not comparable with 2019+ and are flagged per-occupation: financial-analysts (13-2051 split into 2018's 13-2051 + 13-2054, so May 2018 also contains risk specialists), broadcast-and-sound-engineering-technicians (group 27-4010 gains 27-4015 fed from 2010's 27-4099 and loses 27-4013 to 43-2099), assemblers-and-fabricators (group 51-2090: May 2018 also contains what 2019+ reports as 51-2051 and 51-2061).
- 21-1018 and 51-2028 are classified 'oews_combined_code', NOT as vintage breaks. They are OEWS publication codes present in neither SOC vintage, but OEWS keeps the same number and title in all 7 years and their enclosing broad groups (21-1010, 51-2020) are vintage-stable, so the series is internally consistent. Their SOC content is defined only in the OEWS technical notes and is deliberately not inferred here.
- 39 codes used in May-2018 sums were merely RENAMED by 2024 (e.g. 43-5031 'Police, Fire, and Ambulance Dispatchers' -> 'Public Safety Telecommunicators'). These are listed in soc_vintage.oews_title_changes and are explicitly NOT breaks: the crosswalk, not the title, decides comparability. oews_universe.title is the latest-year title, so a UI must consult this list before labelling a 2018 point.
- Group membership on the 2010 side is derived by the SOC zero-padded prefix rule applied to the crosswalk's 900-code 2010 list, because no machine-readable 2010 structure file is parseable without adding an xlrd dependency (bls.gov/soc/soc_structure_2010.xls is legacy .xls). The rule is now SELF-VERIFIED at build time against the May-2018 OEWS file's own running hierarchy: 802 detailed 2010 codes, 0 contradicting their OEWS parent (assert fires otherwise). On the 2018 side the real SOC tree is always used, because the prefix rule fails for exactly one group (29-1210 Physicians, whose detailed members overflow into 29-122x) - reported at build time.
- Pre-existing, unchanged: physicians-and-surgeons and broadcast-and-sound-engineering-technicians have INCOMPLETE child expansions in 2019/2020 (oews_partial), so those sums understate. 45-3031 Fishing and Hunting Workers exists in SOC but never in OEWS (self-employed dominated). May 2019/2020 use hybrid codes 15-1245/15-1256/15-1257 that do not exist in SOC 2018.
- bls.gov sits behind a bot manager; all BLS files go through common.fetch_bls (headed Chromium, disk-cached under raw/). Confirmed the two new crosswalk downloads work through that path from scratch and are byte-identical to a plain-httpx fetch.
- soc_crosswalk.json grew from 372 KB to 432 KB, mostly the full 900-pair crosswalk_2010_to_2018 map (kept so a consumer can audit the labelling without re-downloading BLS).