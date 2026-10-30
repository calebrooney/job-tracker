"""Flask web GUI for the job application tracker.

Add flow is capture-first: paste a link and/or upload a screenshot, we
infer the fields from the job description and prefill the form, then the
user validates/edits and saves. Also: list, view details, and re-capture
a JD on an existing application.
"""

import base64
import io
import os
from datetime import date

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from PIL import Image
from werkzeug.utils import secure_filename

from . import ROOT_DIR, db, parse, vlm

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

# Screenshots uploaded for OCR are stored here.
UPLOAD_DIR = os.path.join(ROOT_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Create the table on startup if needed.
db.init_db()


@app.template_filter("format_salary")
def format_salary(val):
    """Format a stored annual pay range for display (e.g. $82,625 - $103,653/yr)."""
    return parse.format_range(val, per="yr")


@app.template_filter("format_hourly")
def format_hourly(val):
    """Format a stored hourly pay range for display (e.g. $40 - $50/hr)."""
    return parse.format_range(val, per="hr")


@app.template_filter("range_text")
def range_text(val):
    """Plain "low - high" text, for prefilling the compensation input."""
    return parse.range_text(val) or ""


@app.template_filter("pay_text")
def pay_text(val):
    """Inline-edit value for a pay range: "$82,625 - $103,653" (no period)."""
    return "" if parse.range_endpoints(val) is None else parse.format_range(val, per=None)


def _comp_note(app_row):
    """Subtext under the inline compensation field (period / annualized rate)."""
    if app_row.get("compensation_type") is False:
        hourly = parse.format_range(app_row.get("comp_value_hourly"), per=None)
        salary = parse.format_range(app_row.get("comp_value_salary"), per=None)
        if parse.range_endpoints(app_row.get("comp_value_hourly")) is None:
            return ""
        return f"as posted: {hourly}/hr · {salary}/yr"
    if parse.range_endpoints(app_row.get("comp_value_salary")) is None:
        return ""
    return "per year"


@app.template_filter("comp_note")
def comp_note(app_row):
    """Template side of _comp_note (the inline save endpoint reuses it)."""
    return _comp_note(app_row)


@app.route("/")
def index():
    """Show the table of all applications."""
    return render_template("index.html", applications=db.list_applications())


def _jd_pages_png(pages, mime):
    """Normalize stored bytes or rendered PIL pages to (page, bytes, mime, w, h).

    Accepts (page, bytes) tuples (from vlm.png_pages_bytes) or PIL images.
    """
    out = []
    for i, item in enumerate(pages, start=1):
        if isinstance(item, tuple):
            page, data = item
            img = Image.open(io.BytesIO(data))
            w, h = img.size
        else:
            page, img = i, item
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            w, h = img.size
        out.append((page, data, mime, w, h))
    return out


def _store_jd_images(app_id, pages, mime="image/png"):
    """Archive every captured page for an application (warn-only on failure)."""
    if not pages:
        return
    try:
        db.store_jd_images(app_id, _jd_pages_png(pages, mime))
    except Exception as err:  # keep the save working even if the archive fails
        print(f"Could not store JD page images: {err}")


def _render_form(data):
    """Render the validate/edit form, prefilled with `data`.

    `app_date` defaults to today so the common case needs no editing.
    """
    return render_template(
        "form.html",
        data=data,
        today=date.today().isoformat(),
        location_types=db.LOCATION_TYPES,
        statuses=db.STATUSES,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    """Capture step: read a link/screenshot and prefill the form to validate.

    GET shows the capture screen (or an empty form with ?manual=1). POST
    parses the posting, infers fields, and hands off to the form so the
    user only has to confirm.
    """
    if request.method == "POST":
        link = request.form.get("link", "").strip()
        screenshot = request.files.get("screenshot")

        text = None
        source = None
        data = None  # set directly when the VLM reads a screenshot
        try:
            if link:
                text = parse.parse_link(link)
                source = link
            elif screenshot and screenshot.filename:
                filename = secure_filename(screenshot.filename)
                saved_path = os.path.join(UPLOAD_DIR, filename)
                screenshot.save(saved_path)
                source = filename
                try:
                    # Default: VLM reads the screenshot (OCR text is only for the JD store).
                    data, _model = vlm.extract_fields_vlm(saved_path)
                except Exception:
                    data = None  # fall back to OCR + regex below
                # Full JD text: every page of a scanned PDF, not just page 1.
                text = vlm.jd_text(saved_path)
                # Archive all pages at save time (key avoids clashing with DB columns).
                data = dict(data or {})
                data["_jd_pages"] = vlm.png_pages_bytes(saved_path)
            else:
                flash("Paste a link or upload a screenshot, or enter details manually.")
                return redirect(url_for("add"))
        except Exception as err:  # surface parsing/OCR errors to the user
            flash(f"Could not read the job posting: {err}")
            return redirect(url_for("add"))

        # Infer fields (VLM for screenshots; else Workday/Greenhouse/iCIMS regex).
        pages = None
        if data is None:
            data = parse.extract_fields(text, url=link or None)
        else:
            pages = data.pop("_jd_pages", None)
        if pages is not None:
            # Data URLs survive the confirm-form round trip in hidden inputs.
            pages = [
                f"data:image/png;base64,{base64.b64encode(b).decode()}"
                for _pg, b in pages
            ]
        data["link"] = link or None
        data["job_description"] = text
        data["jd_source"] = source
        return render_template(
            "form.html",
            data=data,
            pages=pages,
            today=date.today().isoformat(),
            location_types=db.LOCATION_TYPES,
            statuses=db.STATUSES,
        )

    # GET: allow skipping capture to type everything by hand.
    if request.args.get("manual"):
        return _render_form({})
    return render_template("capture.html")


@app.route("/save", methods=["POST"])
def save():
    """Insert the application after the user validates the prefilled form."""
    form = request.form
    # Tri-state: yes / no / blank (unknown).
    us_cit = form.get("us_citizen_required", "")
    if us_cit == "yes":
        us_citizen_required = True
    elif us_cit == "no":
        us_citizen_required = False
    else:
        us_citizen_required = None

    # Parse and convert compensation (salary vs hourly)
    comp_type_raw = form.get("compensation_type", "salary")
    comp_val_raw = form.get("comp_value", "").strip()
    comp_type, comp_salary, comp_hourly = parse.process_compensation(
        comp_val_raw, comp_type=comp_type_raw
    )

    # JD page images carried from the capture step (data URLs in hidden inputs).
    jd_pages = []
    for i, url in enumerate(form.getlist("jd_pages"), start=1):
        head, _, b64 = (url or "").partition(",")
        if head.endswith(";base64") and b64:
            mime = head[5:-7]  # "data:image/png;base64" -> "image/png"
            jd_pages.append((i, base64.b64decode(b64), mime))

    data = {
        "role": form.get("role") or None,
        "company": form.get("company") or None,
        "location": form.get("location") or None,
        "location_type": form.get("location_type") or None,
        "compensation_type": comp_type,
        "comp_value_salary": comp_salary,
        "comp_value_hourly": comp_hourly,
        "pay": comp_val_raw or None,
        # Applied date defaults to today; user may override for edge cases.
        "app_date": form.get("app_date") or date.today().isoformat(),
        "due_date": form.get("due_date") or None,
        "job_id": form.get("job_id") or None,
        "link": form.get("link") or None,
        # Unchecked checkbox is absent from the form data.
        "energy_related": "energy_related" in form,
        "status": form.get("status") or "applied",
        # Carried over from the capture step (hidden inputs).
        "job_description": form.get("job_description") or None,
        "jd_source": form.get("jd_source") or None,
        "posted_date": form.get("posted_date") or None,
        "application_deadline": form.get("application_deadline") or None,
        "department": form.get("department") or None,
        "us_citizen_required": us_citizen_required,
    }
    app_id = db.add_application(data)
    _store_jd_images(app_id, jd_pages)
    flash("Application added.")
    return redirect(url_for("detail", app_id=app_id))


# Fields the detail page edits inline, one at a time. Compensation is saved as
# a group (type + amount) because both ranges are derived from the raw `pay`
# text, which keeps them in sync with the startup backfill in db.init_db().
EDIT_STRING_FIELDS = {"role", "company", "department", "location", "job_id", "link"}
EDIT_REQUIRED_FIELDS = {"role", "company"}
EDIT_OPTIONAL_DATES = {"due_date", "posted_date", "application_deadline"}
EDIT_FIELDS = EDIT_STRING_FIELDS | EDIT_OPTIONAL_DATES | {
    "app_date", "location_type", "status", "us_citizen_required", "energy_related",
}


def _edit_date(raw, label):
    """Parse a date input value; raises ValueError with a friendly message."""
    try:
        return date.fromisoformat(str(raw).strip()).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{label} needs a date (YYYY-MM-DD).") from None


def _edit_bool(raw):
    """Accept the select values (yes / no) or a JSON boolean."""
    return raw is True or str(raw).strip().lower() in ("yes", "true", "1", "on")


def _edit_value(field, raw):
    """Validate one inline field value and return what to store."""
    if field in EDIT_STRING_FIELDS:
        text = (raw or "").strip()
        if field in EDIT_REQUIRED_FIELDS and not text:
            raise ValueError(f"{field.capitalize()} is required.")
        return text or None
    if field == "app_date":
        if not (raw or "").strip():
            raise ValueError("Date applied is required.")
        return _edit_date(raw, "Date applied")
    if field in EDIT_OPTIONAL_DATES:
        label = field.replace("_", " ").capitalize()
        return _edit_date(raw, label) if (raw or "").strip() else None
    if field == "location_type":
        value = (raw or "").strip()
        if value and value not in db.LOCATION_TYPES:
            raise ValueError("Unknown workplace type.")
        return value or None
    if field == "status":
        value = (raw or "").strip()
        if value not in db.STATUSES:
            raise ValueError("Unknown status.")
        return value
    if field == "us_citizen_required":
        value = (raw or "").strip()
        return None if not value else _edit_bool(value)
    if field == "energy_related":
        return _edit_bool(raw)
    raise ValueError(f"'{field}' is not an editable field.")


def _edit_compensation(payload):
    """Compensation type + amount saved together; the amount may be a range."""
    comp_type = (payload.get("type") or "salary").strip()
    amount = (payload.get("value") or "").strip()
    c_type, c_salary, c_hourly = parse.process_compensation(
        amount, comp_type=comp_type
    )
    if amount and c_salary is None:
        raise ValueError("Could not read that pay amount (e.g. $82,625 - $103,653).")
    return {
        "compensation_type": c_type,
        "comp_value_salary": c_salary,
        "comp_value_hourly": c_hourly,
        "pay": amount or None,
    }


@app.route("/app/<int:app_id>/field", methods=["POST"])
def edit_field(app_id):
    """Save one inline-edited detail-page field (JSON in, JSON out)."""
    payload = request.get_json(silent=True) or {}
    field = (payload.get("field") or "").strip()
    if field != "compensation" and field not in EDIT_FIELDS:
        return jsonify(ok=False, error=f"'{field}' is not an editable field."), 400
    try:
        if field == "compensation":
            updates = _edit_compensation(payload)
        else:
            updates = {field: _edit_value(field, payload.get("value"))}
    except ValueError as err:
        return jsonify(ok=False, error=str(err)), 400
    if not db.update_application(app_id, updates):
        return jsonify(ok=False, error="Application not found."), 404

    row = db.get_application(app_id)
    value = row.get(field) if field != "compensation" else None
    return jsonify(
        ok=True,
        field=field,
        value=None if value is None else str(value),
        comp_type="hourly" if row.get("compensation_type") is False else "salary",
        comp_value=pay_text(
            row.get("comp_value_hourly")
            if row.get("compensation_type") is False
            else row.get("comp_value_salary")
        ),
        comp_note=_comp_note(row),
    )


@app.route("/app/<int:app_id>/delete", methods=["POST"])
def delete_app(app_id):
    """Delete an application and redirect to the dashboard."""
    db.delete_application(app_id)
    flash("Application deleted.")
    return redirect(url_for("index"))


@app.route("/app/<int:app_id>")
def detail(app_id):
    """Show a single application and its stored job description."""
    application = db.get_application(app_id)
    if application is None:
        flash("Application not found.")
        return redirect(url_for("index"))
    return render_template(
        "detail.html",
        app=application,
        jd_images=db.list_jd_images(app_id),
        location_types=db.LOCATION_TYPES,
        statuses=db.STATUSES,
    )


@app.route("/app/<int:app_id>/jd/image/<int:page>")
def jd_image(app_id, page):
    """Serve one stored JD page image straight from the database."""
    row = db.get_jd_image(app_id, page)
    if row is None:
        return "Not found", 404
    return Response(row[1], mimetype=row[0])


@app.route("/app/<int:app_id>/jd", methods=["POST"])
def capture_jd(app_id):
    """Capture a job description from a pasted link or a screenshot upload."""
    link = request.form.get("link", "").strip()
    screenshot = request.files.get("screenshot")

    text = None
    source = None
    try:
        if link:
            text = parse.parse_link(link)
            source = link
        elif screenshot and screenshot.filename:
            filename = secure_filename(screenshot.filename)
            saved_path = os.path.join(UPLOAD_DIR, f"{app_id}_{filename}")
            screenshot.save(saved_path)
            text = vlm.jd_text(saved_path)  # all pages, not just the first
            source = filename
            _store_jd_images(app_id, vlm.png_pages_bytes(saved_path))
        else:
            flash("Provide a link or a screenshot to capture the job description.")
            return redirect(url_for("detail", app_id=app_id))
    except Exception as err:  # surface parsing/OCR errors to the user
        flash(f"Could not capture job description: {err}")
        return redirect(url_for("detail", app_id=app_id))

    db.update_jd(app_id, text, source)
    flash("Job description captured.")
    return redirect(url_for("detail", app_id=app_id))


if __name__ == "__main__":
    # Port 5001 — avoid 5000 (macOS AirPlay / Control Center).
    app.run(debug=True, port=5001)
