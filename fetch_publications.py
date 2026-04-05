#!/usr/bin/env python3
"""
fetch_publications.py
---------------------
Updates publications.json by pulling the latest list of papers from Google Scholar
using the `scholarly` library.

Usage:
    pip install scholarly
    python fetch_publications.py

The script ONLY updates citation counts and adds genuinely new papers.
It NEVER:
  - removes papers that already exist in publications.json
  - overwrites authors, venue, or pdfUrl for existing papers
  - re-adds papers listed in the "removed_titles" array in publications.json

New papers that are not yet in publications.json are appended to the end of
the list with empty topics/tags so you can categorize them yourself.

Run locally or via the GitHub Actions workflow (.github/workflows/update_publications.yml).
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

SCHOLAR_ID = "5PvG8ugAAAAJ"
JSON_PATH = Path(__file__).parent / "publications.json"


def normalize(title: str) -> str:
    """Lower-case, strip punctuation – used for duplicate detection."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def fetch_from_scholar(scholar_id: str) -> list[dict]:
    try:
        from scholarly import scholarly  # type: ignore
    except ImportError:
        sys.exit("scholarly is not installed. Run: pip install scholarly")

    print(f"Fetching author profile for {scholar_id} …")
    author = scholarly.search_author_id(scholar_id)
    author = scholarly.fill(author, sections=["basics", "publications"])

    results = []
    pubs = author.get("publications", [])
    print(f"Found {len(pubs)} publications on Google Scholar")

    for pub in pubs:
        try:
            filled = scholarly.fill(pub)
        except Exception as e:
            print(f"  Warning: could not fill pub — {e}")
            filled = pub

        bib = filled.get("bib", {})
        results.append(
            {
                "title": bib.get("title", ""),
                "authors": bib.get("author", ""),
                "venue": (
                    bib.get("venue")
                    or bib.get("journal")
                    or bib.get("booktitle")
                    or ""
                ),
                "year": bib.get("pub_year"),
                "citations": filled.get("num_citations", 0),
                "url": filled.get("pub_url") or filled.get("eprint_url") or "",
                "pdfUrl": filled.get("eprint_url") or "",
            }
        )
        print(f"  [{bib.get('pub_year', '?')}] {bib.get('title', '')[:70]}")

    return results


def merge(existing: dict, fresh: list[dict]) -> dict:
    """
    Merge freshly-fetched papers into the existing publications.json structure.

    Rules:
    - Updates citation counts for known papers.
    - Updates URL only if the existing entry has none.
    - NEVER overwrites: authors, venue, pdfUrl for existing papers.
    - Appends genuinely new papers (unmatched by title) with empty topics/tags.
    - Skips papers whose normalized title appears in removed_titles.
    - Preserves all other hand-authored fields (topics, tags, awards, etc.).
    """
    existing_pubs = existing.get("publications", [])
    existing_by_norm = {normalize(p["title"]): p for p in existing_pubs}

    # Build a set of normalized titles that have been manually removed
    removed_titles_raw = existing.get("removed_titles", [])
    removed_norm = {normalize(t) for t in removed_titles_raw}

    updated = 0
    added = 0
    skipped_removed = 0
    new_entries = []

    for fp in fresh:
        key = normalize(fp["title"])

        # Skip papers that were manually removed
        if key in removed_norm:
            skipped_removed += 1
            continue

        if key in existing_by_norm:
            ep = existing_by_norm[key]

            # Only update citation count
            if ep.get("citations", 0) != fp["citations"]:
                ep["citations"] = fp["citations"]
                updated += 1

            # Update URL only if the existing one is empty
            if not ep.get("url") and fp.get("url"):
                ep["url"] = fp["url"]

            # NEVER overwrite: authors, venue, pdfUrl — these are curated manually

        else:
            # New paper — append with empty topics/tags for manual categorization
            new_entries.append(
                {
                    "title": fp["title"],
                    "authors": fp["authors"],
                    "venue": fp["venue"],
                    "year": fp["year"],
                    "citations": fp["citations"],
                    "url": fp.get("url", ""),
                    "pdfUrl": fp.get("pdfUrl", ""),
                    "topics": [],   # ← fill these in manually
                    "tags": [],     # ← fill these in manually
                }
            )
            added += 1

    existing["publications"] = existing_pubs + new_entries
    existing["lastUpdated"] = str(date.today())

    print(f"\nMerge complete:")
    print(f"  {updated} citation(s) updated")
    print(f"  {added} new paper(s) added")
    if skipped_removed:
        print(f"  {skipped_removed} manually-removed paper(s) skipped")

    if new_entries:
        print("\nNew papers (please add topics/tags in publications.json):")
        for p in new_entries:
            print(f"  [{p['year']}] {p['title']}")

    return existing


def main():
    if not JSON_PATH.exists():
        sys.exit(f"publications.json not found at {JSON_PATH}")

    with open(JSON_PATH, encoding="utf-8") as f:
        existing = json.load(f)

    fresh = fetch_from_scholar(SCHOLAR_ID)

    merged = merge(existing, fresh)

    # Write publications.json with readable indentation
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"\npublications.json written ({JSON_PATH})")

    # Re-embed the data into publications.js using compact JSON (no extra whitespace)
    js_path = JSON_PATH.parent / "publications.js"
    if js_path.exists():
        js_text = js_path.read_text(encoding="utf-8")
        data_json = json.dumps(merged, separators=(",", ":"), ensure_ascii=False)
        data_line = "var PUBLICATIONS_DATA = " + data_json + ";\n\n"
        # Replace the existing data block — everything before the first (function block
        iife_start = js_text.find("(function")
        if iife_start != -1:
            js_text = data_line + js_text[iife_start:]
            js_path.write_text(js_text, encoding="utf-8")
            print(f"publications.js updated with embedded data ({js_path})")
        else:
            print("Warning: could not find IIFE in publications.js — skipping JS update")


if __name__ == "__main__":
    main()
