"""CLI capture flow: paste a link and/or screenshot, confirm fields, save.

Run via `applied` (or `python cli.py`). No browser.
"""

import argparse
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import unquote, urlparse

import db
import parse
import vlm

# Screenshots copied here so paths stay project-local (same as the web app).
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# `applied --test` results (predictions + run metadata) live here.
EVAL_DIR = os.path.join(os.path.dirname(__file__), "eval")

# Fields compared between VLM prediction and user-confirmed value.
LOG_FIELDS = [
    "role", "company", "location", "location_type", "pay", "job_id",
    "posted_date", "application_deadline", "department", "us_citizen_required",
]


def _get_key():
    """Read a single keypress or escape sequence without requiring Enter."""
    if not sys.stdin.isatty():
        return sys.stdin.read(1)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        # Capture multi-byte escape sequences (e.g. arrow keys, delete).
        if ch == "\x1b":
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r:
                ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


def ask_text(label, default=None):
    """Prompt for text: Enter accepts default, x rejects/clears, Delete edits manually."""
    if default is None:
        value = input(f"{label}: ").strip()
        return value or None

    prompt = f"{label} [{default}]: "
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if not sys.stdin.isatty():
        raw = input().strip()
        if raw in ("x", "X", "-"):
            return None
        return raw or default

    while True:
        ch = _get_key()
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):  # Enter: accept inferred
            sys.stdout.write(f"\n")
            sys.stdout.flush()
            return default
        if ch in ("x", "X"):  # x: reject / clear and skip
            sys.stdout.write("(cleared)\n")
            sys.stdout.flush()
            return None
        if ch in ("\x7f", "\x08", "\x1b[3~", "-"):  # Delete/Backspace: manual entry
            sys.stdout.write(f"\r\033[K{label}: ")
            sys.stdout.flush()
            val = input().strip()
            return val or None
        if ch.isprintable():  # Direct typing: start manual entry with typed char
            sys.stdout.write(f"\r\033[K{label}: {ch}")
            sys.stdout.flush()
            rest = input()
            val = (ch + rest).strip()
            return val or None


def ask_choice(label, options, default=None):
    """Numbered menu: number is instant command, Enter keeps default (*), x clears."""
    print(f"\n{label}:")
    for i, opt in enumerate(options, start=1):
        mark = " *" if opt == default else ""
        print(f"  {i}) {opt}{mark}")

    hint = f" [{default}]" if default else ""
    prompt = f"Choice{hint} (1-{len(options)}, Enter accept, x clear): "
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if not sys.stdin.isatty():
        raw = input().strip()
        if not raw:
            return default
        if raw in ("x", "X", "-"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return default

    while True:
        ch = _get_key()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            sys.stdout.write(f"{default or '(none)'}\n")
            sys.stdout.flush()
            return default
        if ch in ("x", "X", "\x7f", "\x08", "\x1b[3~", "-"):
            sys.stdout.write("(cleared)\n")
            sys.stdout.flush()
            return None
        if ch.isdigit() and 1 <= int(ch) <= len(options):
            chosen = options[int(ch) - 1]
            sys.stdout.write(f"{chosen}\n")
            sys.stdout.flush()
            return chosen


def ask_yes_no(label, default=False):
    """Homebrew-style single keypress prompt [y/n] (Enter accepts default)."""
    suffix = "Y/n" if default else "y/N"
    sys.stdout.write(f"{label} [{suffix}]: ")
    sys.stdout.flush()

    if not sys.stdin.isatty():
        raw = input().strip().lower()
        if not raw:
            return default
        return raw.startswith("y")

    while True:
        ch = _get_key()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            sys.stdout.write("yes\n" if default else "no\n")
            sys.stdout.flush()
            return default
        if ch in ("y", "Y"):
            sys.stdout.write("yes\n")
            sys.stdout.flush()
            return True
        if ch in ("n", "N"):
            sys.stdout.write("no\n")
            sys.stdout.flush()
            return False


def ask_yes_no_optional(label, default=None):
    """Single keypress tri-state prompt: y -> True, n -> False, x/Delete -> clear, Enter -> keep."""
    hint = "yes" if default is True else "no" if default is False else "none"
    sys.stdout.write(f"{label} [{hint}] (y/n, Enter accept, x clear): ")
    sys.stdout.flush()

    if not sys.stdin.isatty():
        raw = input().strip().lower()
        if not raw:
            return default
        if raw in ("x", "-"):
            return None
        return raw.startswith("y")

    while True:
        ch = _get_key()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            sys.stdout.write(f"{hint}\n")
            sys.stdout.flush()
            return default
        if ch in ("y", "Y"):
            sys.stdout.write("yes\n")
            sys.stdout.flush()
            return True
        if ch in ("n", "N"):
            sys.stdout.write("no\n")
            sys.stdout.flush()
            return False
        if ch in ("x", "X", "\x7f", "\x08", "\x1b[3~", "-"):
            sys.stdout.write("(cleared)\n")
            sys.stdout.flush()
            return None


def normalize_path(raw):
    """Clean a pasted/drag-dropped path (quotes, ~, file://, escaped spaces)."""
    if not raw:
        return None
    path = raw.strip()
    # Finder/Terminal often wraps the path in matching quotes.
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
        path = path[1:-1]
    if path.lower().startswith("file:"):
        path = unquote(urlparse(path).path)
    path = os.path.expanduser(path)
    # Terminal drag-drop escapes spaces as "\ ".
    path = path.replace("\\ ", " ")
    return path


def save_clipboard_image():
    """Save a PNG from the macOS clipboard into uploads/. Return the path.

    Uses AppleScript («class PNGf»). Raises RuntimeError if the clipboard
    has no image (Cmd+V of a photo into the terminal never works as text).
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(UPLOAD_DIR, f"clipboard_{stamp}.png")
    # POSIX path escaped for AppleScript string literal
    posix = dest.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    try
        set pngData to the clipboard as «class PNGf»
    on error
        error "no image"
    end try
    set outFile to POSIX file "{posix}"
    set fileRef to open for access outFile with write permission
    try
        set eof of fileRef to 0
        write pngData to fileRef
    end try
    close access fileRef
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("no image")
    return dest


def save_clipboard_pdf():
    """Save a PDF from the macOS clipboard into uploads/. Return the path.

    Print → Copy PDF to Clipboard may use com.adobe.pdf or Apple's PDF type.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(UPLOAD_DIR, f"clipboard_{stamp}.pdf")
    # AppleScriptObjC: try both pasteboard types classic clipboard may set.
    script = f'''
use framework "AppKit"
use framework "Foundation"
use scripting additions
set dest to "{dest}"
set pb to current application's NSPasteboard's generalPasteboard()
set typeList to {{"com.adobe.pdf", "Apple PDF pasteboard type", "public.pdf"}}
repeat with t in typeList
  set d to pb's dataForType:t
  if d is not missing value then
    set ok to (d's writeToFile:dest atomically:true)
    if ok as boolean then return "ok"
  end if
end repeat
error "no pdf"
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("no pdf")
    return dest


def read_clipboard_capture():
    """Pull PDF or PNG from the clipboard into uploads/. Prefer PDF."""
    try:
        print("Reading PDF from clipboard...")
        return save_clipboard_pdf(), "pdf"
    except RuntimeError:
        pass
    try:
        print("Reading image from clipboard...")
        return save_clipboard_image(), "image"
    except RuntimeError:
        pass
    raise RuntimeError(
        "Clipboard has no PDF or image.\n"
        "  • PDF: Cmd+P → PDF → Copy PDF to Clipboard, then Enter here.\n"
        "  • Image: copy a screenshot, then Enter here."
    )


def text_from_file(path):
    """OCR an image or extract text from a PDF at path."""
    lower = path.lower()
    if lower.endswith(".pdf"):
        print("Extracting text from PDF...")
        text = parse.pdf_text(path)
        # Clipboard PDFs are ephemeral — drop the temp copy after extract.
        if os.path.basename(path).startswith("clipboard_"):
            try:
                os.remove(path)
            except OSError:
                pass
        if not text:
            raise RuntimeError(
                "PDF had no extractable text (likely a scan). "
                "Use a screenshot + OCR instead."
            )
        return text
    print("Running OCR...")
    return parse.ocr_image(path)


def resolve_capture(raw):
    """Path or empty Enter (clipboard PDF/image) → local file for parsing."""
    if raw:
        path = normalize_path(raw)
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path or raw}")
        dest = os.path.join(UPLOAD_DIR, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(dest):
            shutil.copy2(path, dest)
        return dest, os.path.basename(path)
    dest, _kind = read_clipboard_capture()
    return dest, os.path.basename(dest)


def capture(backend):
    """Read a link and/or screenshot/PDF, infer fields (VLM), confirm, and insert."""
    db.init_db()
    print("New application — paste a link and/or screenshot/PDF\n")
    print(
        "Tips:\n"
        "  • Cmd+V cannot paste a photo/PDF into the terminal.\n"
        "  • PDF: Chrome Cmd+Option+P → PDF → Copy PDF to Clipboard,\n"
        "    then Enter below (Chrome may flash an error; if the\n"
        "    notification appeared, the clipboard is fine).\n"
        "  • Image: copy a screenshot, then Enter (or paste a file path).\n"
    )

    link = ask_text("Job posting link")
    # Empty Enter with no link → read PDF/PNG from macOS clipboard.
    media = ask_text("Screenshot/PDF path (or Enter to use clipboard)")

    # Path pasted into the link field (common) → treat as media.
    if link:
        link_as_file = normalize_path(link)
        if link_as_file and os.path.isfile(link_as_file):
            if not media:
                media = link
            link = None

    parts = []  # JD text pieces (link text now, OCR text joined at save)
    sources = []
    path = None  # local image/PDF, if any
    try:
        if link:
            print("Reading link...")
            parts.append(parse.parse_link(link))
            sources.append(link)

        # Path given → use it. No path and no link → clipboard PDF/image.
        if media or not link:
            path, name = resolve_capture(media)
            sources.append(name)

        if not parts and not path:
            print("Need a link, screenshot, or PDF.")
            return
    except Exception as err:
        print(f"Could not read the posting: {err}")
        return

    source = " + ".join(sources)
    pool = ThreadPoolExecutor(max_workers=1)
    ocr_future = None  # full-JD OCR, started only after the VLM returns
    vlm_log = None  # (model, latency_ms, predicted) when the VLM was used

    if path:
        # Default live path: VLM only, so latency is just one model call.
        print(f"Reading with VLM ({backend})...")
        start = time.perf_counter()
        try:
            data, model = vlm.extract_fields_vlm(path, backend)
            latency_ms = int((time.perf_counter() - start) * 1000)
            print(f"VLM done in {latency_ms / 1000:.1f}s")
            vlm_log = (model, latency_ms, {k: data.get(k) for k in LOG_FIELDS})
            # Full-JD OCR runs while you review the fields (no VLM contention).
            ocr_future = pool.submit(vlm.jd_text, path)
        except Exception as err:
            # Fallback: OCR + regex like before.
            print(f"VLM failed ({err}); falling back to OCR + regex...")
            try:
                parts.append(vlm.jd_text(path))
            except Exception as ocr_err:
                print(f"Could not read the posting: {ocr_err}")
                return
            data = parse.extract_fields("\n\n".join(parts), url=link)
    else:
        data = parse.extract_fields("\n\n".join(parts), url=link)

    data["link"] = link
    data["job_description"] = "\n\n".join(parts) or None
    data["jd_source"] = source
    data["app_date"] = date.today().isoformat()
    data["status"] = "applied"
    data["energy_related"] = False
    data["due_date"] = None

    print(
        "\nReview inferred fields (Enter accept, x reject/clear, Delete edit manually):\n"
    )

    data["role"] = ask_text("Role", data.get("role"))
    data["company"] = ask_text("Company", data.get("company"))
    data["location"] = ask_text("Location", data.get("location"))
    data["location_type"] = ask_choice(
        "Location type", db.LOCATION_TYPES, data.get("location_type")
    )

    # Prompt for compensation: specify salary vs hourly and amount
    default_comp_type = "hourly" if data.get("compensation_type") is False else "salary"
    comp_type_choice = ask_choice(
        "Compensation type", ["salary", "hourly"], default_comp_type
    )

    default_val = None
    if comp_type_choice == "hourly":
        if data.get("comp_value_hourly") is not None:
            h_val = float(data["comp_value_hourly"])
            default_val = str(int(h_val) if h_val.is_integer() else h_val)
        elif data.get("comp_value"):
            default_val = str(data.get("comp_value"))
        elif data.get("pay"):
            default_val = str(data.get("pay"))
        val_prompt = "Hourly rate ($/hr)"
    else:
        if data.get("comp_value_salary") is not None:
            s_val = float(data["comp_value_salary"])
            default_val = str(int(s_val) if s_val.is_integer() else s_val)
        elif data.get("comp_value"):
            default_val = str(data.get("comp_value"))
        elif data.get("pay"):
            default_val = str(data.get("pay"))
        val_prompt = "Annual salary ($/yr)"

    comp_val_str = ask_text(val_prompt, default_val)
    if comp_val_str:
        c_type, c_sal, c_hour = parse.process_compensation(
            comp_val_str, comp_type=comp_type_choice
        )
        data["compensation_type"] = c_type
        data["comp_value_salary"] = c_sal
        data["comp_value_hourly"] = c_hour
        data["pay"] = comp_val_str
    else:
        data["compensation_type"] = None
        data["comp_value_salary"] = None
        data["comp_value_hourly"] = None
        data["pay"] = None

    data["department"] = ask_text("Department / team", data.get("department"))
    data["posted_date"] = ask_text(
        "Posted date (YYYY-MM-DD)", data.get("posted_date")
    )
    data["application_deadline"] = ask_text(
        "Application deadline (YYYY-MM-DD)", data.get("application_deadline")
    )
    data["app_date"] = ask_text("Application date (YYYY-MM-DD)", data["app_date"])
    data["due_date"] = ask_text("Due date (YYYY-MM-DD)", data.get("due_date"))
    data["job_id"] = ask_text("Job / req id", data.get("job_id"))
    data["us_citizen_required"] = ask_yes_no_optional(
        "US citizen required", data.get("us_citizen_required")
    )
    data["link"] = ask_text("Link", data.get("link"))
    data["energy_related"] = ask_yes_no("Energy related?", data["energy_related"])
    data["status"] = (
        ask_choice("Status", db.STATUSES, data["status"]) or "applied"
    )

    if not ask_yes_no("\nSave this application?", True):
        print("Cancelled.")
        return

    # Collect the background OCR text (usually already finished by now).
    if ocr_future is not None:
        try:
            parts.append(ocr_future.result())
        except Exception as err:
            print(f"(Could not OCR the job description: {err})")
        data["job_description"] = "\n\n".join(parts) or None
    # Clipboard captures are ephemeral: drop the temp copy once read.
    if path and os.path.basename(path).startswith("clipboard_"):
        try:
            os.remove(path)
        except OSError:
            pass

    app_id = db.add_application(data)
    if vlm_log:
        model, latency_ms, predicted = vlm_log
        db.log_extraction(
            backend, model, latency_ms, predicted,
            {k: data.get(k) for k in LOG_FIELDS},
        )
    role = data.get("role") or "(no role)"
    company = data.get("company") or "(no company)"
    print(f"\nLogged #{app_id}: {role} @ {company}")


def collect_test_files(path):
    """Return image/PDF files for a test run (a single file or a folder)."""
    path = normalize_path(path)
    if os.path.isdir(path):
        exts = (".png", ".jpg", ".jpeg", ".webp", ".pdf")
        return sorted(
            os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(exts)
        )
    if os.path.isfile(path):
        return [path]
    raise FileNotFoundError(f"Not found: {path}")


def git_commit():
    """Short git hash of the code under test (or None)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=os.path.dirname(__file__),
        )
        return out.stdout.strip() or None
    except OSError:
        return None


def run_test(path, backend, run_name=None):
    """Batch eval: run OCR+regex and the VLM in parallel on each file.

    Writes eval/runs/<run>/predictions.jsonl (+ meta.json). Never touches the
    database and asks no questions. Re-running with the same --run name skips
    files already done (resumable, e.g. for the OpenRouter free daily quota).
    """
    files = collect_test_files(path)
    run_name = run_name or f"{datetime.now():%Y%m%d_%H%M%S}_{backend}"
    run_dir = os.path.join(EVAL_DIR, "runs", run_name)
    os.makedirs(run_dir, exist_ok=True)
    pred_path = os.path.join(run_dir, "predictions.jsonl")

    # Record settings so runs are reproducible and comparable.
    meta_path = os.path.join(run_dir, "meta.json")
    if not os.path.exists(meta_path):
        with open(meta_path, "w") as f:
            json.dump({
                "backend": backend,
                "model": vlm.BACKENDS[backend]["model"],
                "git_commit": git_commit(),
                "prompt_sha1": hashlib.sha1(vlm.PROMPT.encode()).hexdigest()[:10],
                "max_pixels": vlm.MAX_PIXELS,
                "crop_top": vlm.CROP_TOP,
                "max_tokens": vlm.MAX_TOKENS,
                "started": datetime.now().isoformat(timespec="seconds"),
            }, f, indent=2)

    # Skip files already recorded in this run folder.
    done = set()
    if os.path.exists(pred_path):
        with open(pred_path) as f:
            done = {json.loads(line)["file"] for line in f if line.strip()}

    todo = [p for p in files if os.path.basename(p) not in done]
    print(f"Test run '{run_name}': {len(todo)} to do, {len(done)} already done")
    for i, p in enumerate(todo, start=1):
        name = os.path.basename(p)
        try:
            record = {"file": name, **vlm.extract_both(p, backend)}
        except Exception as err:  # keep the batch going
            record = {"file": name, "error": str(err)}
        with open(pred_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        v = record.get("vlm") or {}
        r = record.get("regex") or {}
        print(
            f"[{i}/{len(todo)}] {name}  "
            f"vlm {v.get('latency_s', 0):.1f}s{' ERR' if v.get('error') else ''}  "
            f"regex {r.get('latency_s', 0):.1f}s"
        )
    print(f"Saved to {pred_path}")


def main():
    """Entry point for `applied` / `python cli.py`."""
    ap = argparse.ArgumentParser(prog="applied")
    ap.add_argument(
        "--test", metavar="PATH",
        help="batch eval on an image/PDF or folder: runs regex+VLM in parallel, "
             "writes eval/runs/, never saves applications",
    )
    ap.add_argument(
        "--backend", choices=list(vlm.BACKENDS), default=vlm.DEFAULT_BACKEND,
        help="VLM backend (default: %(default)s)",
    )
    ap.add_argument("--run", help="run folder name for --test (reuse to resume)")
    args = ap.parse_args()

    if args.test:
        run_test(args.test, args.backend, args.run)
    else:
        capture(args.backend)


if __name__ == "__main__":
    main()
