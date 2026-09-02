"""
fetch_hn.py -- build the raw corpus of Hacker News "Ask HN: Who is hiring?" threads.

Source: the public Algolia HN API (no auth).
  thread discovery : https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
  full thread      : https://hn.algolia.com/api/v1/items/{objectID}

The whoishiring / _whoishiring accounts post three monthly threads:
  "Ask HN: Who is hiring?"          <- job postings           (KEPT)
  "Ask HN: Who wants to be hired?"  <- candidates             (dropped)
  "Freelancer? Seeking freelancer?" <- contract gigs          (dropped)
A handful of unrelated stories are also posted from those accounts (dropped, listed on stdout).

A few months are missing from the author index because that month's thread was posted by a
different account (2011-07 Aloisius, 2011-12 lpolovets, 2015-04/05 _whoishiring) or is simply
absent from the tag index (2016-04). Rather than hardcoding those ids we detect the gaps and
run a targeted Algolia title search for each one; the resolution is printed and recorded in
raw/hn/_threads.json with source="search-backfill".

Everything is cached on disk under raw/hn/ and never re-downloaded, so the script is
idempotent and cheap to re-run for the newest month.

Usage:  uv run python pipeline/fetch_hn.py [--min-year 2011] [--refresh-index] [--force]
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RAW, get_json, read_json, write_json  # noqa: E402

HN_RAW = RAW / "hn"
INDEX_RAW = HN_RAW / "_index_raw.json"      # every story by the whoishiring accounts
THREADS = HN_RAW / "_threads.json"          # resolved month -> thread manifest

SEARCH_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"
SEARCH = "https://hn.algolia.com/api/v1/search"
ITEM = "https://hn.algolia.com/api/v1/items/{}"

DELAY = 0.7          # polite gap between uncached requests
MIN_COMMENTS = 50    # below this a month's thread is treated as a dud and re-searched

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}

# "Ask HN: Who is Hiring? (April 2011)" / "Ask HN: Who is hiring? (September 2026)"
TITLE_RE = re.compile(r"who\s+is\s+hiring\??\s*\(\s*(" + "|".join(MONTHS) + r")\s+(\d{4})\s*\)", re.I)


def classify_title(title):
    """Bucket a story posted by the whoishiring accounts."""
    t = (title or "").lower()
    if "wants to be hired" in t or "want to be hired" in t:
        return "wants_to_be_hired"
    if "freelancer" in t:
        return "freelancer"
    if "who is hiring" in t or "who's hiring" in t or "whos hiring" in t:
        return "hiring"
    return "other"


def month_of(title):
    m = TITLE_RE.search(title or "")
    if not m:
        return None
    return "%04d-%02d" % (int(m.group(2)), MONTH_NUM[m.group(1).lower()])


def discover(refresh=False):
    """Page search_by_date until exhausted. Returns the raw hit list."""
    if INDEX_RAW.exists() and not refresh:
        hits = read_json(INDEX_RAW)
        print("index: %d stories (cached %s)" % (len(hits), INDEX_RAW))
        return hits
    hits, page, pages, d = [], 0, 1, {}
    while page < pages:
        d = get_json(SEARCH_BY_DATE, params={"tags": "story,author_whoishiring",
                                             "hitsPerPage": 100, "page": page})
        pages = d["nbPages"]
        hits.extend(d["hits"])
        page += 1
        time.sleep(DELAY)
    print("index: %d stories fetched from search_by_date (nbHits=%s)" % (len(hits), d.get("nbHits")))
    write_json(hits, INDEX_RAW)
    return hits


def search_month(ym):
    """Targeted title search for a month the author index does not cover."""
    year, mo = ym.split("-")
    label = "%s %s" % (MONTHS[int(mo) - 1].capitalize(), year)
    d = get_json(SEARCH, params={"query": "Ask HN: Who is hiring? (%s)" % label,
                                 "tags": "story", "hitsPerPage": 20})
    time.sleep(DELAY)
    cands = [h for h in d.get("hits", []) if month_of(h.get("title")) == ym]
    if not cands:
        return None
    return max(cands, key=lambda h: h.get("num_comments") or 0)


def all_months(lo, hi):
    y, m = int(lo[:4]), int(lo[5:])
    out = []
    while "%04d-%02d" % (y, m) <= hi:
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def build_manifest(hits, min_year):
    counts = {"hiring": 0, "wants_to_be_hired": 0, "freelancer": 0, "other": 0}
    others, unmonthed = [], []
    by_month = {}
    for h in hits:
        kind = classify_title(h.get("title"))
        counts[kind] += 1
        if kind == "other":
            others.append((h["objectID"], (h.get("created_at") or "")[:10], h.get("title")))
            continue
        if kind != "hiring":
            continue
        ym = month_of(h.get("title"))
        if ym is None:
            unmonthed.append((h["objectID"], (h.get("created_at") or "")[:10], h.get("title"),
                              h.get("num_comments")))
            continue
        rec = {"id": str(h["objectID"]), "month": ym, "title": h.get("title"),
               "author": h.get("author"), "num_comments": h.get("num_comments") or 0,
               "created_at": h.get("created_at"), "source": "author-index"}
        prev = by_month.get(ym)
        if prev is None or rec["num_comments"] > prev["num_comments"]:
            by_month[ym] = rec

    print("\nthread types seen on the whoishiring accounts:")
    for k, v in counts.items():
        print("  %-20s %4d" % (k, v))
    print("\n  dropped %d unrelated stories from those accounts:" % len(others))
    for oid, dt, ti in sorted(others, key=lambda x: x[1]):
        print("    %s  %s  %s" % (dt, oid, ti))
    print("  dropped %d 'who is hiring' stories with no (Month YYYY) in the title:" % len(unmonthed))
    for oid, dt, ti, nc in sorted(unmonthed, key=lambda x: x[1]):
        print("    %s  %s  (%s comments)  %s" % (dt, oid, nc, ti))

    lo, hi = min(by_month), max(by_month)
    if min_year:
        lo = max(lo, "%04d-01" % min_year)
    wanted = all_months(lo, hi)

    # backfill: months absent from the author index, or represented by a dud thread
    print("\nbackfill pass over %d expected months:" % len(wanted))
    for ym in wanted:
        cur = by_month.get(ym)
        if cur is not None and cur["num_comments"] >= MIN_COMMENTS:
            continue
        why = "missing from author index" if cur is None else "only %d comments" % cur["num_comments"]
        best = search_month(ym)
        if best is None:
            print("  %s: %s -> NO CANDIDATE FOUND" % (ym, why))
            continue
        if cur is not None and (best.get("num_comments") or 0) <= cur["num_comments"]:
            print("  %s: %s -> nothing better found, keeping %s" % (ym, why, cur["id"]))
            continue
        print("  %s: %s -> %s by %s (%s comments)" % (ym, why, best["objectID"],
                                                      best.get("author"), best.get("num_comments")))
        by_month[ym] = {"id": str(best["objectID"]), "month": ym, "title": best.get("title"),
                        "author": best.get("author"), "num_comments": best.get("num_comments") or 0,
                        "created_at": best.get("created_at"), "source": "search-backfill"}

    manifest = [by_month[m] for m in wanted if m in by_month]
    missing = [m for m in wanted if m not in by_month]
    return manifest, missing, counts, wanted


def fetch_thread(tid, force=False):
    """Cache https://hn.algolia.com/api/v1/items/{tid} to raw/hn/{tid}.json."""
    dest = HN_RAW / ("%s.json" % tid)
    if dest.exists() and not force:
        try:
            with open(dest, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("children") is not None:
                return dest, len(d["children"]), True
        except Exception:  # noqa: BLE001 - corrupt cache, refetch
            pass
    last = None
    for attempt in range(5):
        try:
            d = get_json(ITEM.format(tid), tries=2, timeout=180)
            if d.get("children") is None:
                raise ValueError("no children in payload")
            tmp = dest.with_suffix(".json.part")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            tmp.replace(dest)
            time.sleep(DELAY)
            return dest, len(d["children"]), False
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(4 * (attempt + 1))
    raise RuntimeError("failed to fetch thread %s: %s" % (tid, last))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-year", type=int, default=2011,
                    help="earliest year to fetch (default 2011; the project window is 2018+)")
    ap.add_argument("--refresh-index", action="store_true", help="re-page the story index")
    ap.add_argument("--force", action="store_true", help="re-download every thread")
    args = ap.parse_args()

    HN_RAW.mkdir(parents=True, exist_ok=True)
    hits = discover(refresh=args.refresh_index)
    manifest, missing, counts, wanted = build_manifest(hits, args.min_year)

    print("\nfetching %d 'Who is hiring?' threads (%s .. %s)"
          % (len(manifest), manifest[0]["month"], manifest[-1]["month"]))
    cached = fetched = total_children = 0
    for i, t in enumerate(manifest, 1):
        dest, n, was_cached = fetch_thread(t["id"], force=args.force)
        t["children"] = n
        t["bytes"] = dest.stat().st_size
        total_children += n
        cached += was_cached
        fetched += (not was_cached)
        if i % 20 == 0 or i == len(manifest):
            print("  [%3d/%d] %s  id=%9s  top-level=%4d  (%s)"
                  % (i, len(manifest), t["month"], t["id"], n, "cache" if was_cached else "net"))

    write_json({"generated_from": "hn.algolia.com/api/v1",
                "thread_type_counts": counts,
                "months_expected": wanted,
                "months_missing": missing,
                "threads": manifest}, THREADS, compact=False)

    mb = sum(t["bytes"] for t in manifest) / 1e6
    print("\n" + "=" * 72)
    print("threads in manifest : %d" % len(manifest))
    print("  from cache        : %d" % cached)
    print("  downloaded now    : %d" % fetched)
    print("month range         : %s .. %s (%d expected)"
          % (manifest[0]["month"], manifest[-1]["month"], len(wanted)))
    print("months missing      : %s" % (missing if missing else "none"))
    print("top-level comments  : %s" % format(total_children, ","))
    print("raw bytes on disk   : %.1f MB in %s" % (mb / 1.0, HN_RAW))
    print("=" * 72)


if __name__ == "__main__":
    main()
