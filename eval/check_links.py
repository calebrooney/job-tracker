"""Verify eval posting links and pull pre-label fields from schema.org JSON-LD.

Usage:
  python eval/check_links.py URL [URL ...]
  python eval/check_links.py --file eval/postings.md      # every URL in the doc
  python eval/check_links.py --file eval/postings.md --json eval/link_check.jsonl

For each URL: HTTP status, <title>, and (when present) the page's
schema.org/JobPosting block: title, company, every location, job id, pay,
posted date. Workday pages are JS-rendered and LinkedIn/Handshake often gate
behind auth, so those come back with no JSON-LD -- label them from the
screenshot instead (that is the real deployment path anyway).
"""

import argparse
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT_S = 25
URL_RE = re.compile(r"https?://[^\s|)\"'<>]+")


def _walk(node):
    """Yield every dict inside a JSON-LD structure (handles @graph nesting)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _location_name(loc):
    """Flatten a schema.org Place into a readable location string."""
    if not isinstance(loc, dict):
        return str(loc)
    addr = loc.get("address") or loc
    if isinstance(addr, str):
        return addr
    parts = [
        addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry"),
    ]
    parts = [p for p in parts if isinstance(p, str)]
    if parts:
        return ", ".join(parts)
    return loc.get("name") or ""


def _salary(salary):
    """Flatten baseSalary into the pay text as written on the page."""
    if not isinstance(salary, dict):
        return None
    value = salary.get("value") or salary
    if isinstance(value, dict):
        lo, hi = value.get("minValue"), value.get("maxValue")
        unit = value.get("unitText")
        if lo and hi:
            return f"{lo}-{hi}" + (f" {unit}" if unit else "")
        if lo or hi:
            return f"{lo or hi}" + (f" {unit}" if unit else "")
    return None


def jobposting(url):
    """Fetch a URL and return {status, title, ...JobPosting fields}."""
    out = {"url": url, "status": None, "title": None, "company": None,
           "locations": [], "job_id": None, "pay": None, "posted": None,
           "employment_type": None, "error": None}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_S)
        out["status"] = resp.status_code
        soup = BeautifulSoup(resp.text, "html.parser")
        if soup.title:
            out["title"] = soup.title.get_text(strip=True)

        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for node in _walk(data):
                if node.get("@type") != "JobPosting":
                    continue
                org = node.get("hiringOrganization") or {}
                out["company"] = org.get("name") if isinstance(org, dict) else org
                out["title"] = node.get("title") or out["title"]
                locs = node.get("jobLocation")
                if locs:
                    out["locations"] = [
                        _location_name(l) for l in (locs if isinstance(locs, list) else [locs])
                    ]
                if node.get("jobLocationType"):
                    out["locations"].append(node["jobLocationType"])
                ident = node.get("identifier") or {}
                if isinstance(ident, dict):
                    out["job_id"] = ident.get("value")
                elif ident:
                    out["job_id"] = str(ident)
                out["pay"] = _salary(node.get("baseSalary"))
                out["posted"] = node.get("datePosted")
                out["employment_type"] = node.get("employmentType")
                return out
    except requests.RequestException as err:
        out["error"] = type(err).__name__
    return out


def main():
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("urls", nargs="*", help="URLs to check")
    ap.add_argument("--file", help="markdown/text file to scan for URLs")
    ap.add_argument("--json", help="write full results as JSON lines to this path")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file) as f:
            seen, ordered = set(), []
            for u in URL_RE.findall(f.read()):
                u = u.rstrip(".,")
                if u not in seen:
                    seen.add(u)
                    ordered.append(u)
            urls += ordered
    if not urls:
        ap.error("give at least one URL, or --file")

    results = []
    for i, url in enumerate(urls, start=1):
        rec = jobposting(url)
        results.append(rec)
        locs = "; ".join(filter(None, rec["locations"])) or "-"
        sd = "SD" if re.search(r"san diego", locs, re.I) else "  "
        print(f"[{i}/{len(urls)}] {rec['status']} {sd} "
              f"{(rec['title'] or rec['error'] or 'no title')[:60]:60} {locs[:60]}")

    if args.json:
        with open(args.json, "w") as f:
            for rec in results:
                f.write(json.dumps(rec) + "\n")
        print(f"\nWrote {args.json}")

    ok = sum(1 for r in results if r["status"] == 200)
    with_sd = sum(1 for r in results if re.search(r"san diego", " ".join(r["locations"]), re.I))
    print(f"\n{ok}/{len(results)} returned 200; {with_sd} list San Diego")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
