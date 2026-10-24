# Job Application Tracker

A simple job application tracker (Flask + Postgres) modeled on the columns in
`Intern Apps.xlsx`. The web GUI is capture-first: paste a job link or upload a
screenshot, and it reads the posting (scrape / local OCR), infers the fields,
and prefills the form for you to validate instead of typing from scratch.

## Architecture

```
  capture sources                    pipeline (cli.py "applied" · app.py web)
┌─────────────────────┐
│ job link (URL)      │── parse_link (BeautifulSoup) ──► raw JD text ─┐
├─────────────────────┤                                               │
│ screenshot file     │── copied to uploads/ ─┐                       │
│ (drag / paste path) │                       │                       │
├─────────────────────┤                       │                       │
│ clipboard PDF       │── saved to uploads/,  │                       │
│ (Print→PDF menu)    │   kept until save     │                       │
├─────────────────────┤                       │                       │
│ clipboard image     │── uploads/ (PNGf)     │                       │
│ ⚠ page 1 only       │                       ▼                       │
└─────────────────────┘            ┌─────────────────────────┐        │
                                   │ captured file (img/PDF) │        │
                                   └──┬──────────┬──────────┬┘        │
                                      │          │          │         │
          ┌───────────────────────────┘          │          │         │
          ▼                                      ▼          ▼         │
  render_image(path)                   jd_text(path)        png_pages_bytes(path)
  · PDF: PAGE 1 ONLY                   · embedded text:     · every page rendered
  · render scale 1.5,                    all pages           at ~144 dpi as PNG
    downscale to ≤ 1 MP              · scanned PDF:               │
  · sent to VLM as JPEG                OCR every page             │
          │                                      │                     │
          ▼                                      │                     │
  extract_fields_vlm → role, company,            │                     │
  location, pay, job_id, ... (page 1             │                     │
  headers; single fast call)                     │                     │
          │                                      │                     │
          ▼                                      ▼                     │
  confirm fields (CLI single-key / web form) → applications row        │
  ─ job_description TEXT ◄────────────────────────                     │
  ─ extraction_logs (predicted vs confirmed, JSONB)                    │
          │                                                            │
          ▼ save                                                       ▼
  ┌────────────────────── Postgres ───────────────────────────┐
  │ applications (fields + job_description text)              │
  │ jd_images (BYTEA per page, cascade delete)  ◄── all pages │
  │ extraction_logs                                           │
  └───────────────┬───────────────────────────────────────────┘
                  │
  detail page: /app/<id> shows JD text + "JD Pages" gallery served
  from the DB via /app/<id>/jd/image/<page> — no local PDFs needed.

  eval: applied --test → vlm.extract_both = VLM (page 1) ∥ OCR+regex
        (all pages) → eval/runs/<run>/predictions.jsonl (DB untouched)
```

Key point: the VLM sees **page 1 only** by design — role/company/location
live in the header, and one small image keeps the model call fast. The full
posting is still captured in parallel: `job_description` gets text from **all
pages**, and `jd_images` stores a rendered PNG of **every page** in Postgres,
so nothing is lost even though no local PDF is kept.

## Setup

1. Install the Tesseract OCR binary (needed for screenshot capture):

   ```bash
   brew install tesseract
   ```

2. Install the package (creates `.venv`, installs deps, puts `applied` on PATH):

   ```bash
   ./install-applied.sh
   ```

   Or manually:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

   After a PATH install, open a new terminal tab and run `applied` from anywhere
   (same idea as Homebrew / Claude Code CLI entry points).

   `./install-applied.sh` also installs a **Print → PDF** menu item
   (`Copy PDF to Clipboard`) under `~/Library/PDF Services/`. That is the right
   hook for Cmd+P (a normal CLI command cannot intercept the print dialog).

3. Create a Postgres database and set `DATABASE_URL` in a `.env` file in this
   repo (see `.env.example`). Defaults to `postgresql://localhost/jobtracker`
   if unset.

   ```bash
   createdb jobtracker
   ```

   The `applications` table is created automatically on first run.

## `applied` (terminal capture)

```bash
applied
```

Capture a posting with a link and/or screenshot/PDF:

- **Link:** paste the URL as usual.
- **Screenshot file:** drag a file into the terminal, or paste a path.
- **PDF via Cmd+P (no Save to Disk):** use the **macOS system** print dialog
  (not Chrome’s built-in “Save as PDF” destination):
  - **Chrome:** Cmd+P, then click **Print using system dialog…** (or press
    **Cmd+Option+P**), then in the system dialog open the **PDF** button
    (bottom-left) → **Copy PDF to Clipboard**.
  - **Safari:** Cmd+P already opens the system dialog → **PDF** →
    **Copy PDF to Clipboard**.
  Then in `applied`, leave the path blank and press **Enter**.
- **Clipboard image:** copy a screenshot, then **Enter** at the media prompt
  (Cmd+V of a photo into the terminal does not work).

Then review inferred fields (instant single-key controls):

- **Enter** — accept inferred value
- **x** — reject / clear field and skip
- **Delete / Backspace** — live manual reentry / correction
- **1, 2, 3...** — instant selection on numbered menus (asterisk indicates inferred default)
- **y / n** — instant yes/no toggle (Homebrew style, no Enter required)

A link and a screenshot/PDF can both be used; clipboard is tried when there is
no link and you press Enter on the media prompt.

## Run the web GUI

```bash
flask --app app run
```

Then open http://127.0.0.1:5000

- **Applications** list shows every row with status badges.
- **+ New Application** starts the capture-first flow:
  1. Paste a job link (scraped with BeautifulSoup) and/or upload a screenshot
     (OCR'd with Tesseract). Screenshots are saved under `uploads/`.
  2. Fields are inferred with format-aware heuristics (**Workday**, **Greenhouse**,
     **iCIMS** POC; otherwise generic regex) and prefilled — including
     posted date, application deadline, department, and US-citizen-required
     when visible. See [JD-FORMATS.md](JD-FORMATS.md).
  3. Validate/edit the form and save. The **application date defaults to today**
     (editable for backfilling older applications).
  - Prefer to type it all yourself? Use the "Skip — enter details manually" link.
- An application's detail page still lets you (re)capture the job description
  from a link or screenshot.

## Add via CLI (same as `applied`)

```bash
applied
# or: python cli.py
```

## Import existing spreadsheet data (optional)

Load the current rows from `Sheet1` of the spreadsheet:

```bash
python import_xlsx.py
```

## Notes

- JS-heavy job boards (Workday, Handshake) may return little text from plain
  link scraping; the screenshot + OCR path is the reliable fallback.
- Extracted job description text is stored raw (no LLM cleanup).
