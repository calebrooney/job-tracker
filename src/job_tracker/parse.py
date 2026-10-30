"""Local JD capture + field inference (Option A POC).

Grab text via parse_link (HTML scrape) or ocr_image (Tesseract).
detect_format picks workday / greenhouse / icims from URL or OCR chrome;
extract_fields runs a label-anchored extractor for that family, else generic
regex. Best-effort — user always validates before save.
"""

import re
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Empty field dict returned when nothing is found.
EMPTY_FIELDS = {
    "role": None,
    "company": None,
    "location": None,
    "location_type": None,
    "compensation_type": None,
    "comp_value_salary": None,
    "comp_value_hourly": None,
    "comp_value": None,
    "pay": None,
    "job_id": None,
    "posted_date": None,
    "application_deadline": None,
    "department": None,
    "us_citizen_required": None,
}


def parse_link(url):
    """Fetch a URL and return visible text with page title prepended."""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    body = "\n".join(line for line in lines if line)
    return f"{title}\n{body}" if title else body


def ocr_image(path):
    """Run Tesseract OCR on an image file and return the detected text."""
    import pytesseract
    from PIL import Image

    return pytesseract.image_to_string(Image.open(path)).strip()


def pdf_text(path):
    """Extract text from a PDF (browser Cmd+P saves). Empty if scan-only."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def detect_format(url=None, text=None):
    """Return 'workday', 'greenhouse', 'icims', or 'generic' from URL/OCR chrome."""
    url = (url or "").lower()
    text = text or ""

    if "myworkdayjobs.com" in url or re.search(r"\bworkday\b", text, re.I):
        # Prefer URL; Workday word in body alone is weak, but req chrome helps.
        if "myworkdayjobs.com" in url:
            return "workday"
        if re.search(r"job\s+requisition\s+id|time\s+type", text, re.I):
            return "workday"

    if (
        "greenhouse.io" in url
        or "gh_jid=" in url
        or re.search(r"powered\s+by\s+greenhouse", text, re.I)
    ):
        return "greenhouse"

    if "icims.com" in url or re.search(r"powered\s+by\s+icims", text, re.I):
        return "icims"

    return "generic"


def _label_value(text, labels, max_chars=120):
    """Return text after a label (same line or next non-empty line)."""
    label_re = "|".join(re.escape(l) for l in labels)
    # Same line: "Label: value" or "Label  value"
    m = re.search(
        rf"(?:^|\n)\s*(?:{label_re})\s*[:\-]?\s*(.+?)(?:\n|$)",
        text,
        re.I,
    )
    if m:
        val = m.group(1).strip()
        if val and not re.match(rf"^(?:{label_re})$", val, re.I):
            return val[:max_chars].strip()
    return None


def _location_type(text):
    """Guess onsite / hybrid / remote from keywords (hybrid before remote)."""
    if re.search(r"\bhybrid\b", text, re.I):
        return "hybrid"
    if re.search(r"\bremote\b", text, re.I):
        return "remote"
    if re.search(r"\b(on-?site|in-?office|in-?person)\b", text, re.I):
        return "onsite"
    return None


def _pay(text):
    """Find a $-denominated pay snippet."""
    m = re.search(
        r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?"
        r"(?:\s?[-\u2013to]+\s?\$?\s?\d[\d,]*(?:\.\d+)?\s?[kK]?)?"
        r"(?:\s?(?:/|per\s+)?(?:hour|hr|year|yr|annually))?",
        text,
        re.I,
    )
    return m.group(0).strip() if m else None


# A number in a pay string: thousands separators, cents, optional "k" suffix.
_NUM_TOKEN = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)(\s*[kK])?")
# Text between two numbers that makes them a range ("-", "–", "to", "through"),
# allowing the currency symbol of the second figure ("$82,625 - $103,653").
_RANGE_BETWEEN = re.compile(r"^\s*(?:-|\u2013|\u2014|to|through)\s*\$?\s*$", re.I)
# A stored numrange literal, e.g. "[82625,103653]" or "[40320,)".
_RANGE_LITERAL = re.compile(r"^\s*[\[(]\s*([^,]*?)\s*,\s*([^\])]*?)\s*[\])]\s*$")


def parse_compensation_range(val):
    """Return (low, high) from a pay string, or None when it holds no number.

    A posted range keeps both endpoints -- "$82,625 - $103,653" gives
    (82625.0, 103653.0) -- instead of collapsing to an average. A single
    figure becomes a point range (low == high), and a "k" suffix is expanded
    ($140k -> 140000). Endpoints are returned low-first.
    """
    if val is None:
        return None
    if isinstance(val, (int, float, Decimal)):
        return float(val), float(val)
    text = str(val).strip()
    if not text:
        return None

    tokens = []
    for m in _NUM_TOKEN.finditer(text):
        num = float(m.group(1).replace(",", ""))
        if m.group(2):
            num *= 1000
        tokens.append((num, m))
    if not tokens:
        return None

    first = tokens[0][0]
    # Only the two numbers around an explicit separator are the range; other
    # numbers in the string ("2080 hours") are ignored rather than averaged in.
    if len(tokens) > 1 and _RANGE_BETWEEN.match(
        text[tokens[0][1].end():tokens[1][1].start()]
    ):
        low, high = first, tokens[1][0]
        return round(min(low, high), 2), round(max(low, high), 2)
    return round(first, 2), round(first, 2)


def _num_text(value):
    """Render a number for a numrange literal (no thousands separators)."""
    num = round(float(value), 2)
    return str(int(num)) if num.is_integer() else f"{num:.2f}"


def numrange_literal(low, high=None):
    """Build a Postgres numrange literal with inclusive bounds, e.g. "[82625,103653]"."""
    if low is None:
        return None
    high = low if high is None else high
    low, high = sorted((round(float(low), 2), round(float(high), 2)))
    return f"[{_num_text(low)},{_num_text(high)}]"


def range_endpoints(val):
    """Return (low, high) for a stored pay range, or None when there is none.

    Accepts psycopg2's NumericRange (what the DB hands back), the literal text
    form ("[82625,103653]") and plain numbers (legacy rows). Open-ended bounds
    stay None and an empty range returns None.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, Decimal)):
        return float(val), float(val)
    # Check the class before touching the instance so an unset value (Jinja's
    # Undefined in a template) returns None instead of raising. psycopg2 hands
    # back a NumericRange for numrange columns.
    cls = type(val)
    if hasattr(cls, "bounds") and hasattr(cls, "lower") and hasattr(cls, "upper"):
        if getattr(val, "isempty", False):
            return None
        low, high = val.lower, val.upper
        return (
            float(low) if low is not None else None,
            float(high) if high is not None else None,
        )
    match = _RANGE_LITERAL.match(str(val))
    if match:
        low, high = match.groups()
        return (float(low) if low else None, float(high) if high else None)
    try:  # plain number from an older schema
        num = float(str(val).replace(",", "").lstrip("$"))
    except ValueError:
        return None
    return num, num


def _fmt_money(value):
    """Format one endpoint as "$82,625" (cents only when present)."""
    return f"${value:,.2f}" if round(value, 2) != int(value) else f"${int(value):,}"


def format_range(val, per="yr"):
    """Display text for a stored pay range, e.g. "$82,625 - $103,653/yr".

    `per` is the period suffix ("yr", "hr"), or None to leave it off.
    """
    span = range_endpoints(val)
    if span is None:
        return "-"
    low, high = span
    suffix = f"/{per}" if per else ""
    if low is None and high is None:
        return "-"
    if low is None:
        return f"up to {_fmt_money(high)}{suffix}"
    if high is None:
        return f"{_fmt_money(low)}+{suffix}"
    if low == high:
        return f"{_fmt_money(low)}{suffix}"
    return f"{_fmt_money(low)} - {_fmt_money(high)}{suffix}"


def range_text(val):
    """Plain "low - high" text for prefilling an input (None when empty)."""
    span = range_endpoints(val)
    if span is None:
        return None
    low, high = span
    if low is None or high is None:
        return _num_text(low if low is not None else high)
    if low == high:
        return _num_text(low)
    return f"{_num_text(low)} - {_num_text(high)}"


def process_compensation(raw_val, comp_type=None):
    """Convert raw input into (compensation_type, salary range, hourly range).

    compensation_type: True = salary, False = hourly.
    comp_value_salary / comp_value_hourly are Postgres numrange literals with
    inclusive bounds ("[82625,103653]"), so a posted range is stored as-is
    instead of being averaged into a single number.
    comp_value_salary is annualized (hourly * 2080) when the input is hourly,
    so salaries stay comparable; comp_value_hourly is None for salary input.
    """
    if raw_val is None or str(raw_val).strip() == "":
        return None, None, None

    # Parse the range endpoints (single values become point ranges).
    span = parse_compensation_range(raw_val)
    if span is None:
        return None, None, None
    low, high = span

    # Determine type if not explicitly specified
    raw_str = str(raw_val).lower()
    is_salary = True
    if comp_type is not None:
        if isinstance(comp_type, bool):
            is_salary = comp_type
        elif str(comp_type).lower() in ("hourly", "hour", "hr", "false", "0"):
            is_salary = False
        elif str(comp_type).lower() in ("salary", "annual", "year", "yr", "true", "1"):
            is_salary = True
    else:
        # Heuristic detection from string keywords or magnitude
        if re.search(r"\b(hour|hr|hourly|/hr)\b", raw_str):
            is_salary = False
        elif re.search(r"\b(year|yr|annually|annual|/yr|k)\b", raw_str):
            is_salary = True
        elif max(low, high) < 250:
            # Low numbers without k suffix are typically hourly wages
            is_salary = False

    if is_salary:
        return True, numrange_literal(low, high), None
    # Standard full-time heuristic: 40 hrs/wk * 52 wks = 2080 hrs/yr
    return (
        False,
        numrange_literal(low * 2080, high * 2080),
        numrange_literal(low, high),
    )


def _city_st(text):
    """Find a City, ST location."""
    m = re.search(r"([A-Z][A-Za-z.\- ]+,\s*[A-Z]{2})\b", text)
    return m.group(1).strip() if m else None


def _parse_date_snippet(raw):
    """Normalize a short date string to YYYY-MM-DD when possible, else raw text."""
    if not raw:
        return None
    raw = raw.strip()
    # Already ISO-ish
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    # MM/DD/YYYY or M/D/YY
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", raw)
    if m:
        mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        return f"{yr:04d}-{mo:02d}-{day:02d}"
    # Month DD, YYYY
    m = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})\b",
        raw,
        re.I,
    )
    if m:
        months = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        mo = months[m.group(1)[:3].lower()]
        return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    return raw[:40]


def _posted_date(text):
    """Extract posted date after common labels."""
    raw = _label_value(
        text,
        ["Posted Date", "Date Posted", "Posted On", "Posted", "Date posted"],
    )
    return _parse_date_snippet(raw) if raw else None


def _application_deadline(text):
    """Extract application deadline after common labels."""
    raw = _label_value(
        text,
        [
            "Application Deadline",
            "Application Close Date",
            "Apply By",
            "Apply by",
            "Closing Date",
            "Close Date",
            "Deadline",
        ],
    )
    return _parse_date_snippet(raw) if raw else None


def _department(text):
    """Extract department / team after common labels."""
    return _label_value(
        text,
        ["Department", "Team", "Organization", "Business Unit", "Job Family"],
        max_chars=80,
    )


def _us_citizen_required(text):
    """True if JD clearly requires US citizenship; False if it says not required; else None."""
    if re.search(
        r"u\.?s\.?\s+citizenship\s+(is\s+)?required|"
        r"must\s+be\s+a?\s*u\.?s\.?\s+citizen|"
        r"only\s+u\.?s\.?\s+citizens|"
        r"u\.?s\.?\s+citizen(?:ship)?\s+required",
        text,
        re.I,
    ):
        return True
    if re.search(
        r"u\.?s\.?\s+citizenship\s+(is\s+)?not\s+required|"
        r"citizenship\s+not\s+required|"
        r"do\s+not\s+require\s+u\.?s\.?\s+citizenship",
        text,
        re.I,
    ):
        return False
    return None


def _shared_from_text(text):
    """Fill fields that are shared across all formats."""
    raw_pay = _pay(text)
    comp_type, val_salary, val_hourly = process_compensation(raw_pay)
    # Prefill text for the form input: the range as typed in the posting.
    comp_val = range_text(val_hourly if comp_type is False else val_salary) or raw_pay

    return {
        "location_type": _location_type(text),
        "pay": raw_pay,
        "compensation_type": comp_type,
        "comp_value_salary": val_salary,
        "comp_value_hourly": val_hourly,
        "comp_value": comp_val,
        "location": _city_st(text),
        "posted_date": _posted_date(text),
        "application_deadline": _application_deadline(text),
        "department": _department(text),
        "us_citizen_required": _us_citizen_required(text),
    }


def _extract_workday(text, url=None):
    """Label-anchored Workday extractor; req id also from URL trailing _CODE."""
    fields = dict(EMPTY_FIELDS)
    fields.update(_shared_from_text(text))

    # Title is usually the biggest early heading; Workday page title is often the role.
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    # Strip "Job Details" / site chrome if present.
    if first and not re.search(r"myworkday|job details|careers", first, re.I):
        fields["role"] = first
    else:
        for line in text.splitlines()[1:8]:
            line = line.strip()
            if len(line) > 8 and not re.search(
                r"requisition|time type|posted|locations?|apply", line, re.I
            ):
                fields["role"] = line
                break

    loc = _label_value(text, ["Locations", "Location", "Primary Location"])
    if loc:
        fields["location"] = loc
    fields["job_id"] = _label_value(
        text, ["Job Requisition ID", "Requisition ID", "Job Id", "Job ID"]
    )
    # URL: ..._R000137235 or ..._JR3587
    if url:
        m = re.search(r"_([A-Z]{1,3}\d{3,})\s*(?:/|$|\?)", url, re.I)
        if m and not fields["job_id"]:
            fields["job_id"] = m.group(1)
        # Tenant subdomain often encodes company: axos.wd5.myworkdayjobs.com
        host = urlparse(url).hostname or ""
        m = re.match(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", host, re.I)
        if m:
            fields["company"] = m.group(1).replace("-", " ").title()

    dept = _label_value(text, ["Job Family", "Department", "Worker Sub-Type"])
    if dept:
        fields["department"] = dept
    return fields


def _extract_greenhouse(text, url=None):
    """Greenhouse extractor; gh_jid and board slug from URL when present."""
    fields = dict(EMPTY_FIELDS)
    fields.update(_shared_from_text(text))

    first = text.splitlines()[0].strip() if text.splitlines() else ""
    # Titles like "Role at Company" or "Role - Company | Greenhouse"
    clean = re.sub(r"\s*[|\u2013\-]\s*Greenhouse.*$", "", first, flags=re.I).strip()
    split = re.split(r"\s+[-|\u2013\u2014]\s+|\s+at\s+", clean, maxsplit=1)
    if len(split) == 2:
        fields["role"], fields["company"] = split[0].strip(), split[1].strip()
    elif clean:
        fields["role"] = clean

    loc = _label_value(text, ["Location", "Locations"])
    if loc:
        fields["location"] = loc

    if url:
        qs = parse_qs(urlparse(url).query)
        if "gh_jid" in qs:
            fields["job_id"] = qs["gh_jid"][0]
        path = urlparse(url).path or ""
        # boards.greenhouse.io/stripe/jobs/123
        m = re.search(r"greenhouse\.io/([^/]+)", url, re.I)
        if m and not fields["company"]:
            fields["company"] = m.group(1).replace("-", " ").title()
        m = re.search(r"/jobs/(\d+)", path)
        if m and not fields["job_id"]:
            fields["job_id"] = m.group(1)

    if not fields["job_id"]:
        m = re.search(r"(?:job|opening)\s*(?:id|#)?\s*[:#]?\s*(\d{4,})", text, re.I)
        if m:
            fields["job_id"] = m.group(1)
    return fields


def _extract_icims(text, url=None):
    """iCIMS extractor; numeric job id from /jobs/{id}/ path."""
    fields = dict(EMPTY_FIELDS)
    fields.update(_shared_from_text(text))

    first = text.splitlines()[0].strip() if text.splitlines() else ""
    clean = re.sub(r"\s*[|\u2013\-]\s*.*icims.*$", "", first, flags=re.I).strip()
    # "Role in City | Careers at Company"
    m = re.search(r"careers\s+at\s+(.+)$", clean, re.I)
    if m:
        fields["company"] = m.group(1).strip()
        clean = re.sub(r"\s*[|\u2013\-]\s*careers\s+at\s+.+$", "", clean, flags=re.I)
    m = re.search(r"^(.+?)\s+in\s+[A-Z]", clean)
    if m and len(m.group(1)) > 3:
        fields["role"] = m.group(1).strip()
    elif clean:
        fields["role"] = clean

    loc = _label_value(text, ["Job Location", "Location", "Locations"])
    if loc:
        fields["location"] = loc
    fields["job_id"] = _label_value(text, ["Job ID", "Job Id", "ID"])
    if url:
        m = re.search(r"/jobs/(\d+)", url)
        if m:
            fields["job_id"] = fields["job_id"] or m.group(1)
        # careers-appliedsystems.icims.com → Appliedsystems guess
        host = urlparse(url).hostname or ""
        m = re.match(r"careers-([a-z0-9-]+)\.icims\.com", host, re.I)
        if m and not fields["company"]:
            fields["company"] = m.group(1).replace("-", " ").title()
    return fields


def _extract_generic(text):
    """Original regex heuristics for unknown formats."""
    fields = dict(EMPTY_FIELDS)
    fields.update(_shared_from_text(text))
    if not text:
        return fields

    first = text.splitlines()[0].strip() if text.splitlines() else ""
    split = re.split(r"\s+[-|\u2013\u2014]\s+|\s+at\s+", first, maxsplit=1)
    if len(split) == 2:
        fields["role"], fields["company"] = split[0].strip(), split[1].strip()
    elif first:
        fields["role"] = first

    job_id = re.search(
        r"(?:job|req(?:uisition)?)\s*(?:id|number|no\.?|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        text,
        re.I,
    )
    if job_id:
        fields["job_id"] = job_id.group(1).strip()
    return fields


def extract_fields(text, url=None):
    """Detect JD format and return inferred application fields.

    `url` improves detection and fills job_id / company from the path.
    Unknown formats fall back to generic regex.
    """
    fmt = detect_format(url=url, text=text)
    if fmt == "workday":
        return _extract_workday(text, url)
    if fmt == "greenhouse":
        return _extract_greenhouse(text, url)
    if fmt == "icims":
        return _extract_icims(text, url)
    return _extract_generic(text)
