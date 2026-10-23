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

from flask import Flask, Response, flash, redirect, render_template, request, url_for
from PIL import Image
from werkzeug.utils import secure_filename

import db
import parse
import vlm

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

# Screenshots uploaded for OCR are stored here.
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Create the table on startup if needed.
db.init_db()


@app.template_filter("format_salary")
def format_salary(val):
    """Format numeric salary for display in salary form (e.g. $120,000/yr)."""
    if val is None or str(val).strip() == "":
        return "-"
    try:
        num = float(val)
        if num.is_integer():
            return f"${int(num):,}/yr"
        return f"${num:,.2f}/yr"
    except (ValueError, TypeError):
        return str(val)


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
        "detail.html", app=application, jd_images=db.list_jd_images(app_id)
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
    app.run(debug=True)
