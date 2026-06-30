"""
Finviz screener scraper.
Scrapes all pages of the screener, saves results to JSON,
and reports differences vs the previous run.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCREENER_BASE = (
    "https://finviz.com/screener"
    "?v=111"
    "&f=cap_largeunder,fa_debteq_u1,fa_eps5years_o10,"
    "fa_estltgrowth_o10,fa_netmargin_o5,fa_pe_u20,fa_roe_o10,fa_roi_o10"
    "&ft=4"
)

PAGE_SIZE = 20           # Finviz shows 20 rows per page
DELAY_BETWEEN_PAGES = 3  # seconds – be polite

DATA_FILE = Path("data/screener.json")

# Realistic browser headers.
# NOTE: Do NOT set Accept-Encoding manually – requests handles gzip/deflate
# automatically. Setting "br" (Brotli) would cause binary garbage because
# the requests library cannot decode Brotli without the optional brotli package.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

COLUMNS = [
    "no",
    "ticker",
    "company",
    "sector",
    "industry",
    "country",
    "market_cap",
    "pe",
    "price",
    "change",
    "volume",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session initialisation
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """
    Create a session that looks like a real browser.
    We visit the Finviz homepage first so that cookies are set before
    we hit the screener endpoint.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    log.info("Initialising session – visiting finviz.com homepage …")
    try:
        resp = session.get("https://finviz.com/", timeout=20)
        resp.raise_for_status()
        log.info("Homepage OK – cookies: %s", list(session.cookies.keys()))
    except Exception as exc:
        log.warning("Could not pre-load homepage: %s", exc)

    # Small human-like pause
    time.sleep(2)
    return session


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def fetch_page(session: requests.Session, start_row: int) -> BeautifulSoup:
    """Fetch one screener page and return its BeautifulSoup."""
    if start_row == 1:
        url = SCREENER_BASE
    else:
        url = f"{SCREENER_BASE}&r={start_row}"
        session.headers["Referer"] = SCREENER_BASE

    log.info("Fetching row %d → %s", start_row, url)
    response = session.get(url, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"HTTP {response.status_code} for row {start_row}. "
            "Finviz may be blocking the request."
        )

    soup = BeautifulSoup(response.text, "html.parser")

    # --- Sanity check: did we actually get the screener? ---
    table = soup.select_one("table.screener_table")
    if table is None:
        # Dump a snippet of the response for debugging
        snippet = response.text[:800].replace("\n", " ")
        log.error(
            "screener_table NOT found in response (row %d). "
            "Response snippet: %s",
            start_row,
            snippet,
        )
        raise RuntimeError(
            "screener_table not found – Finviz likely returned a login wall "
            "or CAPTCHA page. See the ERROR log above for the raw HTML snippet."
        )

    return soup


def parse_total_results(soup: BeautifulSoup) -> int:
    """
    Try several strategies to extract the total number of results.
    Returns 0 if it cannot be determined.
    """
    # Strategy 1 – the data-layer push embedded in a <script> tag
    # contains "result_count":60
    for script in soup.find_all("script"):
        text = script.string or ""
        if "result_count" in text:
            import re
            m = re.search(r'"result_count"\s*:\s*(\d+)', text)
            if m:
                return int(m.group(1))

    # Strategy 2 – pagination <td> text like "#1 - 20 / 60"
    for td in soup.find_all("td"):
        text = td.get_text(" ", strip=True)
        if " / " in text and "#" in text:
            import re
            m = re.search(r"/\s*(\d+)", text)
            if m:
                return int(m.group(1))

    return 0


def parse_rows(soup: BeautifulSoup) -> list[dict]:
    """Extract all data rows from the screener table on a single page."""
    table = soup.select_one("table.screener_table")
    if table is None:
        return []

    rows = []
    for tr in table.select("tr"):
        cells = tr.select("td")
        texts = [td.get_text(strip=True) for td in cells]

        if len(texts) == len(COLUMNS) + 1:
            # Layout with leading flag-icon cell (empty) – skip it
            data_texts = texts[1:]
        elif len(texts) == len(COLUMNS):
            # Live layout: data starts at cell 0 directly
            data_texts = texts
        else:
            # Header row or unrecognised layout – skip
            continue

        row = dict(zip(COLUMNS, data_texts))
        rows.append(row)

    return rows


def scrape_all() -> list[dict]:
    """Scrape every page and return the full list of companies."""
    session = build_session()

    # --- First page ---
    soup = fetch_page(session, 1)
    first_page_rows = parse_rows(soup)
    log.info("Page 1: parsed %d rows", len(first_page_rows))
    all_rows = list(first_page_rows)

    total = parse_total_results(soup)
    log.info("Total results detected: %d", total)

    if total == 0:
        # We could not parse the total; paginate until we get an empty page
        log.warning(
            "Could not determine total – paginating until empty page."
        )
        start = PAGE_SIZE + 1
        while True:
            time.sleep(DELAY_BETWEEN_PAGES)
            try:
                page_soup = fetch_page(session, start)
            except RuntimeError as exc:
                log.warning("Stopping pagination: %s", exc)
                break
            page_rows = parse_rows(page_soup)
            log.info("Row %d: parsed %d rows", start, len(page_rows))
            if not page_rows:
                break
            all_rows.extend(page_rows)
            start += PAGE_SIZE
    else:
        # We know the total – paginate deterministically
        start = PAGE_SIZE + 1
        while start <= total:
            time.sleep(DELAY_BETWEEN_PAGES)
            page_soup = fetch_page(session, start)
            page_rows = parse_rows(page_soup)
            log.info("Row %d: parsed %d rows", start, len(page_rows))
            if not page_rows:
                log.warning("Empty page at row %d – stopping early.", start)
                break
            all_rows.extend(page_rows)
            start += PAGE_SIZE

    log.info("Total rows scraped: %d", len(all_rows))
    return all_rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_previous() -> dict:
    if not DATA_FILE.exists():
        return {"fetched_at": None, "companies": []}
    with DATA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(companies: list[dict]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "companies": companies,
    }
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    log.info("Saved %d companies to %s", len(companies), DATA_FILE)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def compute_diff(
    previous: list[dict], current: list[dict]
) -> tuple[list[dict], list[dict]]:
    prev_tickers = {c["ticker"]: c for c in previous}
    curr_tickers = {c["ticker"]: c for c in current}
    added   = [curr_tickers[t] for t in curr_tickers if t not in prev_tickers]
    removed = [prev_tickers[t] for t in prev_tickers if t not in curr_tickers]
    return added, removed


def format_diff_report(
    added: list[dict],
    removed: list[dict],
    previous_date: str | None,
    current_date: str,
) -> str:
    lines = [
        "=" * 60,
        "  FINVIZ SCREENER – DAILY DIFF REPORT",
        "=" * 60,
        f"  Run date  : {current_date}",
        f"  Previous  : {previous_date or 'n/a (first run)'}",
        "=" * 60,
    ]
    if not added and not removed:
        lines.append("  No changes detected.")
    else:
        if added:
            lines.append(f"\n  ✅ NEW entries ({len(added)}):")
            for c in sorted(added, key=lambda x: x["ticker"]):
                lines.append(
                    f"    + {c['ticker']:<8}  {c['company']:<35}  "
                    f"{c['sector']:<22}  {c['country']}"
                )
        if removed:
            lines.append(f"\n  ❌ REMOVED entries ({len(removed)}):")
            for c in sorted(removed, key=lambda x: x["ticker"]):
                lines.append(
                    f"    - {c['ticker']:<8}  {c['company']:<35}  "
                    f"{c['sector']:<22}  {c['country']}"
                )
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting Finviz screener scrape")

    previous_snapshot = load_previous()
    previous_companies = previous_snapshot.get("companies", [])
    previous_date = previous_snapshot.get("fetched_at")

    current_companies = scrape_all()
    current_date = datetime.now(timezone.utc).isoformat()

    added, removed = compute_diff(previous_companies, current_companies)

    report = format_diff_report(added, removed, previous_date, current_date)
    print(report)

    report_path = Path("data/diff_report.txt")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    log.info("Diff report written to %s", report_path)

    save_snapshot(current_companies)

    if added or removed:
        log.info("Changes: %d added, %d removed", len(added), len(removed))
        import sys; sys.exit(2)   # signals GitHub Actions: changes detected
    else:
        log.info("No changes detected.")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# NOTE: main() uses sys.exit(2) when changes are detected so that the
# GitHub Actions workflow can detect it via $? and open an Issue.
# Exit 0 = no changes. Exit 2 = changes found. Exit 1 = error.
# ---------------------------------------------------------------------------