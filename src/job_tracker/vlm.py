"""VLM field extraction (screenshot/PDF -> application fields).

Default live path: extract_fields_vlm() only. Batch eval (`applied --test`)
uses extract_both(), which runs OCR+regex and the VLM in parallel and returns
both outputs with timings so they can be scored against the same labels.

Backends (all OpenAI-compatible chat endpoints, called with `requests`):
  local - mlx-vlm server (model chosen from LOCAL_MODELS), free and private
  free  - OpenRouter free VLM
  paid  - OpenRouter cheap paid VLM
"""

import base64
import difflib
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from PIL import Image

from . import ROOT_DIR, parse

# Load OPENROUTER_API_KEY etc. from the project .env.
load_dotenv(os.path.join(ROOT_DIR, ".env"))

# Local MLX models under test (4-bit). Pick one with VLM_LOCAL_MODEL=<alias>
# (or a full HF repo id for anything not listed here).
LOCAL_MODELS = {
    "qwen25vl3b": "mlx-community/Qwen2.5-VL-3B-Instruct-4bit",  # baseline, ~2 GB
    "qwen25vl7b": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit",  # ~5.7 GB
    "qwen3vl8b": "mlx-community/Qwen3-VL-8B-Instruct-4bit",     # ~5-6 GB
    "qwen3vl4b": "mlx-community/Qwen3-VL-4B-Instruct-4bit",     # ~3 GB
}
DEFAULT_LOCAL_MODEL = "qwen3vl4b"
# Resolve the env value: an alias maps to its repo id, anything else passes through.
_local_choice = os.environ.get("VLM_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)
LOCAL_MODEL = LOCAL_MODELS.get(_local_choice, _local_choice)

# Pin model ids here (OpenRouter free models rotate often).
BACKENDS = {
    "local": {
        "url": os.environ.get("VLM_LOCAL_URL", "http://127.0.0.1:8080/v1/chat/completions"),
        "model": LOCAL_MODEL,
        "key_env": None,
    },
    "free": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": os.environ.get("VLM_FREE_MODEL", "google/gemma-4-26b-a4b-it:free"),
        "key_env": "OPENROUTER_API_KEY",
    },
    "paid": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": os.environ.get("VLM_PAID_MODEL", "qwen/qwen3-vl-8b-instruct"),
        "key_env": "OPENROUTER_API_KEY",
    },
}
DEFAULT_BACKEND = os.environ.get("VLM_BACKEND", "local")

# Image budget: smaller = fewer vision tokens = lower latency.
MAX_PIXELS = 1_048_576
# Fraction of the page height kept (top part holds role/company/location/id).
# 1.0 keeps the whole page; lower it (e.g. 0.6) to trade pay/footer info for speed.
CROP_TOP = 1.0
# Cap output tokens: decode time dominates latency.
MAX_TOKENS = 160
TIMEOUT_S = 60

# Render scale for full-page captures (~144 dpi): readable OCR + stored pages.
PDF_RENDER_SCALE = 2.0

# Compact schema: short keys keep the output (and decode time) small.
# OG prompt:
# PROMPT = (
#     "Read this job posting. Reply with ONLY one-line minified JSON (no spaces, "
#     "no newlines, flat string values) with keys: "
#     "role, company, location (one string like \"San Diego, CA\"), "
#     "loc_type (onsite|hybrid|remote), "
#     "pay (as written, e.g. $25/hr or $90,000-$110,000), job_id, "
#     "posted (YYYY-MM-DD), dept, "
#     "us_citizen (true if US citizenship is required, else false). "
#     "Use null for anything not visible. No explanation."
# )

# update v1:
PROMPT = (
    "Extract fields from this job posting image. Reply with ONLY one-line minified JSON "
    "with keys: role, company, location, job_id, pay, loc_type (onsite|hybrid|remote), "
    "posted (YYYY-MM-DD), dept, us_citizen (true/false). "
    "Copy role, company, location, job_id and pay EXACTLY as written in the image. "
    "company is the hiring company's name as shown. "
    "job_id is the requisition/job ID only if a label like Job ID, Req ID or Requisition is shown. "
    "pay is the compensation as written, only if the image shows it. "
    "If a value is not visible, use null. Never guess, never invent, never use placeholders. "
    "No explanation. If a location has multiple values and contains 'San Diego', use 'San Diego, CA' as the location."
)

def render_image(path):
    """Return a PIL image of an image file or page 1 of a PDF, sized for the VLM."""
    from PIL import Image

    if path.lower().endswith(".pdf"):
        import pypdfium2 as pdfium

        page = pdfium.PdfDocument(path)[0]
        img = page.render(scale=1.5).to_pil()  # ~108 dpi is plenty for headers
    else:
        img = Image.open(path)
    img = img.convert("RGB")

    # Optional crop to the top of the page.
    if CROP_TOP < 1.0:
        img = img.crop((0, 0, img.width, int(img.height * CROP_TOP)))

    # Downscale so total pixels stay under the budget (never upscale).
    scale = (MAX_PIXELS / (img.width * img.height)) ** 0.5
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    return img


def render_pages(path):
    """Render EVERY page of a PDF (or a single image file) as PIL RGB images.

    Unlike render_image (page 1, sized for the VLM budget), this keeps full
    pages at capture quality — used for the multi-page JD text fallback and
    the per-page PNG store in Postgres.
    """
    if path.lower().endswith(".pdf"):
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(path)
        return [p.render(scale=PDF_RENDER_SCALE).to_pil().convert("RGB") for p in doc]
    return [Image.open(path).convert("RGB")]


def png_pages_bytes(path):
    """Every page of path as a list of (page_number, png_bytes) for DB storage."""
    pages = []
    for i, img in enumerate(render_pages(path), start=1):
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        pages.append((i, buf.getvalue()))
    return pages


def _image_data_url(path):
    """Encode the rendered image as a base64 JPEG data URL."""
    buf = io.BytesIO()
    render_image(path).save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _parse_json(text):
    """Pull the first JSON object out of a model reply (tolerates code fences)."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError(f"No JSON in model reply: {text!r:.200}")
    return json.loads(match.group(0))


def _clean(value):
    """Normalize empty / 'null' strings to None."""
    if value is None:
        return None
    # Models sometimes return an object for location: flatten to "a, b".
    if isinstance(value, dict):
        value = ", ".join(str(v) for v in value.values() if v)
    value = str(value).strip()
    return None if value.lower() in ("", "null", "none", "n/a") else value


def _to_fields(raw):
    """Map the compact VLM JSON onto the application field dict."""
    fields = dict(parse.EMPTY_FIELDS)
    fields["role"] = _clean(raw.get("role"))
    fields["company"] = _clean(raw.get("company"))
    fields["location"] = _clean(raw.get("location"))
    loc_type = (_clean(raw.get("loc_type")) or "").lower()
    fields["location_type"] = loc_type if loc_type in ("onsite", "hybrid", "remote") else None
    fields["job_id"] = _clean(raw.get("job_id"))
    fields["posted_date"] = _clean(raw.get("posted"))
    fields["department"] = _clean(raw.get("dept"))
    us = raw.get("us_citizen")
    fields["us_citizen_required"] = us if isinstance(us, bool) else None

    # Pay: keep the raw string, derive the salary/hourly ranges like the regex
    # path (a posted range keeps both endpoints).
    pay = _clean(raw.get("pay"))
    fields["pay"] = pay
    c_type, c_sal, c_hour = parse.process_compensation(pay)
    fields["compensation_type"] = c_type
    fields["comp_value_salary"] = c_sal
    fields["comp_value_hourly"] = c_hour
    fields["comp_value"] = parse.range_text(c_hour if c_type is False else c_sal) or pay
    return fields


def ensure_local_server():
    """Start the warm mlx-vlm server (separate .venv-vlm) if nothing is listening.

    The model stays loaded between captures, so the load cost is paid once.
    Logs go to /tmp/vlm-server.log. Raises if it cannot start in time.
    """
    import socket
    import subprocess
    from urllib.parse import urlparse

    cfg = BACKENDS["local"]
    parsed = urlparse(cfg["url"])
    addr = (parsed.hostname, parsed.port or 8080)

    def listening():
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(addr) == 0

    if listening():
        return
    py = os.path.join(ROOT_DIR, ".venv-vlm", "bin", "python")
    if not os.path.exists(py):
        raise RuntimeError("Local VLM not installed: create .venv-vlm and pip install mlx-vlm.")
    print("Starting local VLM server (first run downloads the model)...")
    log = open("/tmp/vlm-server.log", "a")
    subprocess.Popen(
        [py, "-m", "mlx_vlm.server", "--model", cfg["model"], "--port", str(addr[1])],
        stdout=log, stderr=log, start_new_session=True,  # outlives this process
    )
    deadline = time.time() + 300
    while time.time() < deadline:
        if listening():
            return
        time.sleep(1)
    raise RuntimeError("Local VLM server did not start; see /tmp/vlm-server.log")


def extract_fields_vlm(path, backend=None):
    """Send an image/PDF to the VLM and return (fields dict, model id).

    Raises on network/model/JSON errors so callers can fall back to regex.
    """
    backend = backend or DEFAULT_BACKEND
    if backend == "local":
        ensure_local_server()
    cfg = BACKENDS[backend]
    headers = {}
    if cfg["key_env"]:
        key = os.environ.get(cfg["key_env"])
        if not key:
            raise RuntimeError(f"Set {cfg['key_env']} in .env to use this backend.")
        headers["Authorization"] = f"Bearer {key}"

    body = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
            ],
        }],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }
    # OpenRouter reasoning models: skip thinking tokens for speed.
    if cfg["key_env"]:
        body["reasoning"] = {"enabled": False}

    resp = requests.post(cfg["url"], json=body, headers=headers, timeout=TIMEOUT_S)
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"]
    return _to_fields(_parse_json(reply)), cfg["model"]


def jd_text(path):
    """Full JD text from an image (Tesseract) or PDF (embedded text, else OCR)."""
    if path.lower().endswith(".pdf"):
        text = parse.pdf_text(path)
        if text:
            return text
        # Scanned PDF: OCR every rendered page (page breaks keep pages apart).
        import pytesseract

        return "\n\n".join(
            pytesseract.image_to_string(img).strip() for img in render_pages(path)
        ).strip()
    return parse.ocr_image(path)


def _in_text(value, text):
    """True if a VLM value appears (fuzzily) in the OCR text: hallucination check."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()
    value, text = norm(value), norm(text)
    if not value:
        return None
    if value in text:
        return True
    # Fuzzy: best-matching window of the same length (handles OCR typos).
    n = len(value)
    best = 0.0
    for i in range(0, max(1, len(text) - n + 1), max(1, n // 4)):
        best = max(best, difflib.SequenceMatcher(None, value, text[i:i + n]).ratio())
    return best >= 0.85


def _regex_path(path):
    """OCR/PDF text -> regex fields, with timing."""
    start = time.perf_counter()
    text = jd_text(path)
    fields = parse.extract_fields(text)
    return {"fields": fields, "text": text, "latency_s": time.perf_counter() - start}


def _vlm_path(path, backend):
    """VLM fields with timing; errors are captured so one failure doesn't stop a batch."""
    start = time.perf_counter()
    try:
        fields, model = extract_fields_vlm(path, backend)
        err = None
    except Exception as exc:  # network, JSON, auth, etc.
        fields, model, err = None, BACKENDS[backend]["model"], str(exc)
    return {"fields": fields, "model": model, "error": err,
            "latency_s": time.perf_counter() - start}


def extract_both(path, backend=None):
    """Run OCR+regex and the VLM in parallel; return both outputs plus OCR check."""
    backend = backend or DEFAULT_BACKEND
    with ThreadPoolExecutor(max_workers=2) as pool:
        regex_f = pool.submit(_regex_path, path)
        vlm_f = pool.submit(_vlm_path, path, backend)
        regex, vlm = regex_f.result(), vlm_f.result()

    # Per-field: does the VLM value show up in the OCR text?
    check = {}
    if vlm["fields"]:
        for key in ("role", "company", "location", "job_id", "pay", "department"):
            if vlm["fields"].get(key):
                check[key] = _in_text(vlm["fields"][key], regex["text"])
    return {"regex": regex, "vlm": vlm, "vlm_in_ocr": check}
