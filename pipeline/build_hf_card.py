"""
Build the Hugging Face dataset card from the Kaggle release.

The CSVs and the SQLite file are shared with the Kaggle release in kaggle/;
only the card differs, because Hugging Face needs YAML frontmatter to drive the
dataset viewer and Kaggle would just show it as literal text.

Writes hf/README.md. Run after pipeline/build_kaggle_dataset.py:

    uv run python pipeline/build_hf_card.py

The `configs` block is the important part: each CSV becomes its own named
config, so all 20 tables are previewable and loadable with
`load_dataset(REPO, "hn_postings")` rather than only the first file found.
"""

import csv
import json
import os

from common import ROOT

SRC = ROOT / "kaggle"
OUT = ROOT / "hf"
OUT.mkdir(parents=True, exist_ok=True)

REPO = os.environ.get("HF_REPO", "tatra-labs/us-remote-work-and-tech-hiring")

# Order the viewer shows configs in: the headline table first, then the rest of
# the Hacker News corpus, then the official statistics, then reference tables.
PREFERRED = [
    "hn_postings",
    "hn_yearly_summary",
    "hn_monthly_summary",
    "hn_technology_yearly",
    "hn_technology_monthly",
    "hn_posting_technologies",
    "hn_role_yearly",
    "hn_role_monthly",
    "hn_posting_roles",
    "hn_technologies",
    "hn_roles",
    "acs_commute_state",
    "acs_wfh_by_occupation",
    "indeed_remote_monthly",
    "indeed_remote_by_sector_monthly",
    "indeed_sectors",
    "oews_state_occupation",
    "oews_metro_occupation",
    "oews_national_occupation",
    "oews_occupations",
    "states",
]


def csv_tables():
    names = sorted(p.stem for p in SRC.glob("*.csv"))
    ordered = [n for n in PREFERRED if n in names]
    ordered += [n for n in names if n not in set(ordered)]
    return ordered


def row_count(name):
    with open(SRC / f"{name}.csv", encoding="utf-8") as f:
        return max(sum(1 for _ in f) - 1, 0)


def size_category(n):
    for bound, label in [
        (1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"),
        (1_000_000, "100K<n<1M"), (10_000_000, "1M<n<10M"),
    ]:
        if n < bound:
            return label
    return "10M<n<100M"


def frontmatter(tables):
    total_rows = sum(row_count(t) for t in tables)
    lines = [
        "---",
        "license: cc-by-4.0",
        "pretty_name: US Remote Work & Tech Hiring, 2011-2026",
        "language:",
        "  - en",
        "size_categories:",
        f"  - {size_category(total_rows)}",
        "task_categories:",
        "  - tabular-classification",
        "  - text-classification",
        "  - feature-extraction",
        "tags:",
        "  - remote-work",
        "  - labor-market",
        "  - employment",
        "  - job-postings",
        "  - hacker-news",
        "  - hiring",
        "  - economics",
        "  - technology-trends",
        "  - bls",
        "  - census",
        "  - time-series",
        "source_datasets:",
        "  - original",
        "annotations_creators:",
        "  - machine-generated",
        "configs:",
    ]
    for t in tables:
        lines += [
            f"  - config_name: {t}",
            "    data_files:",
            "      - split: train",
            f"        path: {t}.csv",
        ]
    lines.append("---")
    return "\n".join(lines)


def usage_section(tables):
    first = "hn_postings" if "hn_postings" in tables else tables[0]
    return f"""
## Loading

Every table is its own config, so pick the one you want:

```python
from datasets import load_dataset

postings = load_dataset("{REPO}", "{first}", split="train")
print(postings[0])

yearly = load_dataset("{REPO}", "hn_yearly_summary", split="train")
```

Straight to pandas, without the `datasets` library:

```python
import pandas as pd

url = "https://huggingface.co/datasets/{REPO}/resolve/main/hn_postings.csv"
df = pd.read_csv(url)
```

The SQLite build carries the same tables typed and indexed, with convenience
views, and is the fastest way to run joins across sources:

```python
import sqlite3
from huggingface_hub import hf_hub_download

path = hf_hub_download("{REPO}", "us_remote_work.sqlite", repo_type="dataset")
con = sqlite3.connect(path)
con.execute("SELECT remote_class, COUNT(*) FROM hn_postings GROUP BY 1").fetchall()
```

### Available configs

{chr(10).join(f'- `{t}`' for t in tables)}
"""


def main():
    tables = csv_tables()
    body = (SRC / "README.md").read_text(encoding="utf-8")

    # The Kaggle card opens with an H1 that duplicates pretty_name on HF.
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    body = "\n".join(lines)

    card = frontmatter(tables) + "\n\n# US Remote Work & Tech Hiring, 2011-2026\n\n" \
        + body.rstrip() + "\n" + usage_section(tables)

    path = OUT / "README.md"
    path.write_text(card, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}  ({path.stat().st_size/1024:.1f} KB)")
    print(f"  repo    : {REPO}")
    print(f"  configs : {len(tables)}")
    print(f"  rows    : {sum(row_count(t) for t in tables):,}")


if __name__ == "__main__":
    main()
