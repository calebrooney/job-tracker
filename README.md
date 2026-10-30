# Job Application Tracker

A simple job application tracker (Flask + Postgres) modeled on the columns in
`Intern Apps.xlsx`. The web GUI is capture-first: paste a job link or upload a
screenshot, and it reads the posting (scrape / local VLM / OCR), infers the fields,
and prefills the form for you to validate instead of typing from scratch.

## Architecture

```
  capture sources                    pipeline (job_tracker.cli · job_tracker.web)
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

## `applied` commands

After install, run `applied` from any directory (`python -m job_tracker.cli` also works).

### `applied`

Log a new application. Paste a job link and/or a screenshot/PDF, review the
inferred fields, then save.

Capture inputs:

- **Link** — paste the URL.
- **Screenshot file** — drag a file into the terminal, or paste a path.
- **PDF via Cmd+P** — use the macOS system print dialog (not Chrome’s built-in
  Save as PDF). Chrome: Cmd+Option+P → **PDF** → **Copy PDF to Clipboard**.
  Safari: Cmd+P → **PDF** → **Copy PDF to Clipboard**. Then press Enter with
  an empty path prompt.
- **Clipboard image** — copy a screenshot, then Enter at the media prompt
  (Cmd+V of a photo into the terminal does not work).

Field review keys:

- **Enter** — accept inferred value
- **x** — clear / skip
- **Delete / Backspace** — type a new value
- **1, 2, 3…** — pick a numbered menu option (`*` is the default)
- **y / n** — yes / no

### `applied --launch`

Stop any process on port **5001**, start the Flask web GUI there, and open
http://127.0.0.1:5001 in your default browser. Flask stays in the foreground
(Ctrl+C to stop).

Equivalent without the browser open:

```bash
flask --app job_tracker.web run --debug --port 5001
```

### `applied --test PATH`

Batch eval on an image/PDF or a folder. Runs VLM and OCR+regex in parallel,
writes `eval/runs/<run>/predictions.jsonl`, and never touches the database.
Reuse `--run NAME` to resume a run. Use `--note "..."` to record what changed.

```bash
applied --test eval/samples --run my-run --note "prompt tweak"
```

### `applied --backend {local,free,paid}`

Pick the VLM backend for capture or `--test` (default: `local`). OpenRouter
`free` / `paid` need `OPENROUTER_API_KEY`.

```bash
applied --backend free
applied --test eval/samples --backend paid --run or-paid
```

## Web GUI

Open with `applied --launch` (port 5001).

- **Applications** list shows every row with status badges.
- **+ New Application** is the capture-first flow: paste a link and/or upload a
  screenshot, review inferred fields (Workday / Greenhouse / iCIMS heuristics
  when they match; see [JD-FORMATS.md](JD-FORMATS.md)), then save. Application
  date defaults to today. Use “Skip — enter details manually” to type everything.
- **Edit in place** on a detail page: click a value, edit, blur or Enter to save
  (`POST /app/<id>/field`). Escape reverts. Pay accepts a range with salary/hourly.
- Detail pages can (re)capture the job description from a link or screenshot.

## VLM field extraction (`src/job_tracker/vlm.py`)

Page 1 of a screenshot/PDF is read by a local vision model (default
**Qwen3-VL-4B, 4-bit, via mlx-vlm**; free, ~3 GB, downloaded on first use).
It falls back to OCR + regex on failure. Change the local model with
`VLM_LOCAL_MODEL=<alias or HF repo id>` (aliases in `vlm.py`).

## Stored job postings

Every page of a captured PDF/screenshot is rendered to PNG and stored in
Postgres (`jd_images`), so no local PDF is needed. View them in the web GUI on
an application's detail page under **JD Pages**.

<!-- ## Notes

- Extracted job description text is stored raw (no LLM cleanup).
- Pay is stored as a Postgres `numrange` (`comp_value_salary`,
  `comp_value_hourly`), so a posted range like `$82,625 - $103,653` keeps both
  endpoints instead of being averaged. Hourly input is also annualized
  (`* 2080`) into `comp_value_salary`. The raw text the user typed stays in
  `pay`, and `init_db()` re-derives the ranges from it on startup, converting
  older `numeric` columns as it goes. -->