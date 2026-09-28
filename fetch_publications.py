#!/usr/bin/env python3
"""
fetch_publications.py
---------------------
Updates publications.json by pulling the latest list of papers from alphaXiv
(https://www.alphaxiv.org/@sameera-horawalavithana) via its MCP API.

Google Scholar blocks GitHub Actions IPs, so it can't be scraped reliably in CI.

Usage:
    export ALPHAXIV_API_KEY=...   # alphaxiv.org → Settings → API Keys
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
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ALPHAXIV_SLUG = "sameera-horawalavithana"
ALPHAXIV_MCP_URL = "https://api.alphaxiv.org/mcp/v1"
MCP_PROTOCOL_VERSION = "2025-06-18"
HTTP_TIMEOUT = 60  # seconds per request
JSON_PATH = Path(__file__).parent / "publications.json"


def normalize(title: str) -> str:
    """Lower-case, strip punctuation – used for duplicate detection."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def with_retries(fn, what: str, attempts: int = 3):
    """Call fn(), retrying with exponential backoff on transient errors."""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if i == attempts:
                raise
            wait = 10 * 2 ** (i - 1)
            print(f"  {what} failed ({type(e).__name__}: {e}); retry {i}/{attempts - 1} in {wait}s")
            time.sleep(wait)


class AlphaXivMCP:
    """Minimal client for alphaXiv's MCP server (Streamable HTTP, JSON-RPC).

    Uses only the standard library so the workflow needs no pip installs, and
    every request has a timeout so a run can never hang.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session_id = None
        self.next_id = 1

    def _post(self, payload: dict):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            # Cloudflare rejects the default "Python-urllib" user agent with 403.
            "User-Agent": "profile-publications/1.0 (+https://github.com/sameeravithana/_profile)",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            ALPHAXIV_MCP_URL, data=json.dumps(payload).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            self.session_id = resp.headers.get("Mcp-Session-Id") or self.session_id
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
        if "id" not in payload or not body.strip():
            return None  # notification
        if "text/event-stream" in ctype:
            # Pick the JSON-RPC response that answers this request.
            for line in body.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == payload["id"]:
                        return msg
            raise RuntimeError(f"No response for request {payload['id']} in SSE stream")
        return json.loads(body)

    def _request(self, method: str, params: dict) -> dict:
        payload = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.next_id += 1
        msg = self._post(payload)
        if "error" in msg:
            raise RuntimeError(f"{method} error: {msg['error']}")
        return msg["result"]

    def connect(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "profile-publications", "version": "1.0"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict):
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"{name} failed: {result.get('content')}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        text = "\n".join(texts)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


# get_researcher_papers returns Markdown, one paper per line, e.g.
#   - [ID=2311.12289] **ATLANTIC: …** (2023-11-21). 31 citations, 61 views.
PAPER_LINE_RE = re.compile(
    r"^\s*[-*]\s*\[ID=(?P<id>[^\]]*)\]\s*\*\*(?P<title>.+?)\*\*"
    r"\s*(?:\((?P<date>[^)]*)\))?[.,]?\s*(?P<cites>[\d,]+)\s+citations?",
    re.I,
)


def extract_papers(data) -> list[dict]:
    """Find paper records in a tool result: Markdown lines or JSON dicts with a title and citations."""
    if isinstance(data, str):
        return [
            {
                "title": m["title"],
                "arxiv_id": m["id"],
                "publication_date": m["date"] or "",
                "citations": int(m["cites"].replace(",", "")),
            }
            for m in map(PAPER_LINE_RE.match, data.splitlines())
            if m
        ]

    found = []

    def walk(node):
        if isinstance(node, dict):
            title = _first(node, "title", "paper_title")
            cites = _first(node, "citations", "citation_count", "citationCount", "cited_by_count")
            if isinstance(title, str) and cites is not None:
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found


def to_entry(p: dict) -> dict:
    arxiv_id = _first(p, "arxiv_id", "arxivId", "universal_paper_id", "id") or ""
    arxiv_id = re.sub(r"^(arxiv:|https?://arxiv\.org/abs/)", "", str(arxiv_id), flags=re.I)
    date_str = str(_first(p, "publication_date", "published", "date", "publishedDate") or "")
    year = _first(p, "year") or (date_str[:4] if re.match(r"\d{4}", date_str) else None)
    authors = _first(p, "authors") or ""
    if isinstance(authors, list):
        authors = ", ".join(a if isinstance(a, str) else a.get("name", "") for a in authors)
    return {
        "title": " ".join(str(p.get("title") or p.get("paper_title")).split()),
        "authors": authors,
        "venue": _first(p, "venue", "journal") or ("arXiv" if arxiv_id else ""),
        "year": int(year) if year else None,
        "citations": int(_first(p, "citations", "citation_count", "citationCount", "cited_by_count") or 0),
        "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
        "pdfUrl": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
    }


def arxiv_authors(arxiv_ids: list[str]) -> dict[str, str]:
    """Look up author lists on the arXiv API ({id: "A, B, C"}); best effort."""
    if not arxiv_ids:
        return {}
    import xml.etree.ElementTree as ET

    ns = {"a": "http://www.w3.org/2005/Atom"}
    url = "https://export.arxiv.org/api/query?max_results=100&id_list=" + ",".join(arxiv_ids)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "profile-publications/1.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"  Warning: arXiv author lookup failed — {e}")
        return {}
    out = {}
    for entry in root.findall("a:entry", ns):
        m = re.search(r"(\d{4}\.\d{4,5})", entry.findtext("a:id", "", ns))
        names = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
        if m and names:
            out[m.group(1)] = ", ".join(names)
    return out


def fetch_from_alphaxiv(slug: str) -> list[dict]:
    """Fetch the researcher's papers from alphaXiv.

    get_researcher_papers returns at most 25 papers per call, so we union the
    top 25 by citations (covers nearly all citation changes), by recency
    (catches new papers) and by views. Papers outside all three keep their
    existing counts — merge() never removes anything.
    """
    api_key = os.environ.get("ALPHAXIV_API_KEY")
    if not api_key:
        sys.exit("ALPHAXIV_API_KEY is not set (create one at alphaxiv.org → Settings → API Keys)")

    client = AlphaXivMCP(api_key)
    print(f"Connecting to alphaXiv MCP ({ALPHAXIV_MCP_URL}) …")
    try:
        client.connect()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f"alphaXiv rejected the API key (HTTP {e.code}). Check the ALPHAXIV_API_KEY secret.")
        with_retries(client.connect, "connect")

    by_title: dict[str, dict] = {}
    for sort in ("cited", "recent", "viewed"):
        data = with_retries(
            lambda: client.call_tool(
                "get_researcher_papers",
                {"researchers": [slug], "sort": sort, "limit_per_researcher": 25},
            ),
            f"get_researcher_papers(sort={sort})",
        )
        papers = extract_papers(data)
        if not papers:
            preview = data if isinstance(data, str) else json.dumps(data)[:2000]
            sys.exit(f"Could not find papers in alphaXiv response (sort={sort}):\n{preview[:2000]}")
        print(f"  sort={sort}: {len(papers)} papers")
        for p in papers:
            e = to_entry(p)
            by_title.setdefault(normalize(e["title"]), e)

    results = list(by_title.values())
    print(f"Fetched {len(results)} unique papers from alphaXiv")
    for r in results:
        print(f"  [{r['year'] or '?'}] ({r['citations']:>3}) {r['title'][:70]}")
    return results


def arxiv_id_of(pub: dict):
    """arXiv id (without version) from a publication's url/pdfUrl, if any."""
    for field in ("url", "pdfUrl"):
        m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", pub.get(field) or "")
        if m:
            return m.group(1)
    return None


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
    existing_by_arxiv = {}
    for p in existing_pubs:
        aid = arxiv_id_of(p)
        if aid:
            existing_by_arxiv.setdefault(aid, p)

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

        ep = existing_by_norm.get(key) or existing_by_arxiv.get(arxiv_id_of(fp) or "")
        if ep is not None:

            # Only update citation count, and never lower it: sources count
            # citations differently (e.g. a preprint vs. its journal version).
            if fp["citations"] > ep.get("citations", 0):
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

    # alphaXiv doesn't return authors; fill them in from arXiv for new papers.
    authors = arxiv_authors([aid for aid in map(arxiv_id_of, new_entries) if aid])
    for p in new_entries:
        if not p["authors"]:
            p["authors"] = authors.get(arxiv_id_of(p) or "", "")
        if p["venue"] == "arXiv":
            p["venue"] = "arXiv preprint"

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

    fresh = fetch_from_alphaxiv(ALPHAXIV_SLUG)

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
