"""Score `applied --test` runs against hand-verified labels.

Usage:
  python eval/run_eval.py --init-labels eval/runs/RUN      # build labels skeleton
  python eval/run_eval.py eval/runs/RUN [--split test] [--key png|pdf]   # score a run
  (--key png is the default, for runs over eval/images; pdf for runs over eval/pdfs)

labels.jsonl (one JSON object per line; joined to predictions by `pdf` or `png` name):
  {"pdf": "a.pdf", "png": "a.png" | null, "split": "dev|test", "format": "workday",
   "verified": true,
   "fields": {"role": ..., "company": ..., "location": ..., "job_id": ..., "pay": ...}}
(--init-labels is legacy: it writes a `file` key and is not used with the current labels.)

Match rules (see matches()): role/company/job_id exact (case and punctuation ignored);
location: San Diego labels accept any text containing "San Diego", else exact or same
first city; pay: prediction holds the label's whole-dollar min and max.

Scores are computed offline from the stored predictions: no models are re-run.
Arms: regex, vlm, merge (VLM else regex), router (regex first, VLM only if a
core field is empty), oracle (either is right; an upper bound).
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime

# Allow `python eval/run_eval.py` from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(HERE, "labels.jsonl")
# Append-only experiment log (tracked in git): one row per scoring call.
LOG = os.path.join(HERE, "experiments.csv")
CORE = ["role", "company", "location", "job_id", "pay"]
# Pre-registered decision rule (see the plan).
MIN_GAIN_PTS = 8.0
MAX_P = 0.05
# Latency gate, fixed before seeing results: 5 s (capture runs in the background).
MAX_P50_S = 5.0


def load_jsonl(path):
    """Read a JSON-lines file into a list of dicts."""
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def norm(field, value):
    """Normalize a field value for exact-match comparison (None stays None)."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).lower()
    if field == "pay":
        # Compare the numbers only: "$90,000 - $110k" -> (90000, 110000).
        nums = []
        for n, k in re.findall(r"(\d[\d,]*\.?\d*)\s*(k?)", text):
            val = float(n.replace(",", "") or 0)
            nums.append(val * 1000 if k and val < 1000 else val)
        return tuple(nums) or None
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def pay_ints(value):
    """Whole-dollar numbers in a pay string (decimals and '$' dropped; 'k' expanded).

    "$20.00/hr - $31.00/hr" -> {20, 31}; "$176.4K" -> {176400}. None if no numbers.
    """
    nums = norm("pay", value)  # tuple of floats, or None
    return {int(n) for n in nums} if nums else None


def first_city(value):
    """Normalized text before the first comma/slash/semicolon ("Boston, MA" -> "boston")."""
    return norm("location", re.split(r"[,/;]", str(value))[0])


def matches(field, pred, lab):
    """True if the prediction is adequate for this field under the agreed rules.

    role, company, job_id: exact match after case/punctuation normalization.
    location: if the label is San Diego (or a multi-location posting that includes
      San Diego), any prediction containing "San Diego" (case-insensitive) matches;
      otherwise an exact match or the same first city matches.
    pay: the prediction contains the label's whole-dollar min and max (decimals ignored).
    Empty vs empty (no value on either side) always matches.
    """
    truth = (lab["fields"] or {}).get(field)
    t_empty, p_empty = norm(field, truth) is None, norm(field, pred) is None

    # San Diego labels (also via all_locations, e.g. a null location on a multi-loc posting).
    if field == "location":
        label_text = f"{truth or ''} {lab.get('all_locations') or ''}".lower()
        if "san diego" in label_text:
            return not p_empty and "san diego" in str(pred).lower()

    # Otherwise both must be empty, or neither.
    if t_empty or p_empty:
        return t_empty and p_empty

    if field == "location":
        return norm(field, pred) == norm(field, truth) or first_city(pred) == first_city(truth)

    if field == "pay":
        want, got = pay_ints(truth), pay_ints(pred)
        if want is None or got is None:
            return want is None and got is None
        return min(want) in got and max(want) in got

    return norm(field, pred) == norm(field, truth)


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes of n, as percentages."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (center - half), 100 * (center + half))


def mcnemar_p(b, c):
    """Exact two-sided McNemar p-value from discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def pct(values, q):
    """Percentile q (0-100) of a list (nearest rank)."""
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q / 100 * (len(values) - 1))))]


def load_labels(key):
    """Map predictions' file name -> label row, joining on `key` ("pdf" or "png").

    Rows whose `key` is null (e.g. PDF-only docs have no PNG) are skipped.
    """
    return {r[key]: r for r in load_jsonl(LABELS) if r.get(key)}


def init_labels(run_dir):
    """Create labels.jsonl entries from a run's VLM output for hand verification."""
    existing = set(load_labels("pdf")) | set(load_labels("png")) if os.path.exists(LABELS) else set()
    added = 0
    with open(LABELS, "a") as out:
        for rec in load_jsonl(os.path.join(run_dir, "predictions.jsonl")):
            if rec["file"] in existing or "vlm" not in rec:
                continue
            fields = (rec["vlm"].get("fields") or rec["regex"]["fields"])
            # Deterministic ~25% dev split so the test set stays locked.
            split = "dev" if int(hashlib.md5(rec["file"].encode()).hexdigest(), 16) % 4 == 0 else "test"
            out.write(json.dumps({
                "file": rec["file"],
                "split": split,
                "format": parse.detect_format(text=rec["regex"].get("text")),
                "verified": False,  # set true after checking against the image
                "fields": {k: fields.get(k) for k in CORE},
            }) + "\n")
            added += 1
    print(f"Added {added} label stubs to {LABELS}. Verify each against the image, "
          "then set verified=true.")


def arm_predictions(rec):
    """Return {arm: (fields dict, latency_s)} for one stored record."""
    regex = rec["regex"]["fields"] or {}
    r_lat = rec["regex"]["latency_s"]
    v = rec["vlm"]
    vlm = v["fields"]
    arms = {"regex": (regex, r_lat)}
    if vlm is None:  # VLM errored: it contributes nothing for this doc
        return arms
    v_lat = v["latency_s"]
    arms["vlm"] = (vlm, v_lat)
    arms["merge"] = ({k: vlm.get(k) or regex.get(k) for k in CORE}, max(r_lat, v_lat))
    if all(regex.get(k) for k in CORE):
        arms["router"] = (regex, r_lat)
    else:  # regex first, then the VLM: latencies add
        arms["router"] = (vlm, r_lat + v_lat)
    return arms


def score(run_dir, split, allow_unverified, key, note=""):
    """Join predictions to labels (on the `key` column), print the report, write results.csv."""
    labels = load_labels(key)
    recs = []
    for rec in load_jsonl(os.path.join(run_dir, "predictions.jsonl")):
        lab = labels.get(rec["file"])
        if "vlm" not in rec or lab is None:
            continue
        if not (lab.get("verified") or allow_unverified):
            continue
        if split != "all" and lab.get("split") != split:
            continue
        recs.append((rec, lab))
    n = len(recs)
    print(f"Run {os.path.basename(run_dir)}: {n} labeled docs (split={split})")
    if n == 0:
        print("Nothing to score: add verified labels (see --init-labels).")
        return

    # correct[arm][field] -> list of 0/1 per doc; doc_ok[arm] -> list of 0/1.
    correct, doc_ok, lat = {}, {}, {}
    for arm in ("regex", "vlm", "merge", "router", "oracle"):
        correct[arm] = {k: [] for k in CORE}
        doc_ok[arm], lat[arm] = [], []
    rows = []
    halluc = [0, 0]  # [VLM values not found in OCR, VLM values checked]
    for rec, lab in recs:
        arms = arm_predictions(rec)
        # Docs where the VLM failed count as wrong for VLM-derived arms.
        for arm in ("vlm", "merge", "router"):
            arms.setdefault(arm, ({}, rec["regex"]["latency_s"] + rec["vlm"]["latency_s"]))
        ok = {}
        for arm, (fields, latency) in arms.items():
            ok[arm] = {k: int(matches(k, fields.get(k), lab)) for k in CORE}
            lat[arm].append(latency)
        ok["oracle"] = {k: int(ok["regex"][k] or ok["vlm"][k]) for k in CORE}
        for arm, per_field in ok.items():
            for k in CORE:
                correct[arm][k].append(per_field[k])
            doc_ok[arm].append(int(all(per_field.values())))
            rows.append({"file": rec["file"], "arm": arm, "all_core_ok": doc_ok[arm][-1],
                         **{f"{k}_ok": per_field[k] for k in CORE}})
        for found in (rec.get("vlm_in_ocr") or {}).values():
            if found is not None:
                halluc[1] += 1
                halluc[0] += int(not found)

    print(f"\n{'arm':8} " + " ".join(f"{k:>16}" for k in CORE) + f" {'all core':>16}")
    for arm in correct:
        cells = []
        for k in CORE:
            s = sum(correct[arm][k])
            lo, hi = wilson(s, n)
            cells.append(f"{100 * s / n:5.1f} [{lo:3.0f}-{hi:3.0f}]")
        s = sum(doc_ok[arm])
        lo, hi = wilson(s, n)
        cells.append(f"{100 * s / n:5.1f} [{lo:3.0f}-{hi:3.0f}]")
        print(f"{arm:8} " + " ".join(f"{c:>16}" for c in cells))

    print("\nLatency (s)   p50    p95   (test mode: both pipelines run at once, pessimistic)")
    for arm in ("regex", "vlm", "merge", "router"):
        print(f"{arm:8} {pct(lat[arm], 50):6.2f} {pct(lat[arm], 95):6.2f}")
    if halluc[1]:
        print(f"\nVLM hallucination rate (value not in OCR text): "
              f"{100 * halluc[0] / halluc[1]:.1f}% of {halluc[1]} checked values")

    # Per-format doc pass rate (small groups are noise: read with the CIs).
    print("\nDoc pass rate by format (vlm vs regex)")
    for fmt in sorted({lab.get("format") for _, lab in recs}):
        idx = [i for i, (_, lab) in enumerate(recs) if lab.get("format") == fmt]
        v = sum(doc_ok["vlm"][i] for i in idx)
        r = sum(doc_ok["regex"][i] for i in idx)
        print(f"  {fmt:10} n={len(idx):3}  vlm {100 * v / len(idx):5.1f}%  regex {100 * r / len(idx):5.1f}%")

    # Paired comparison on the doc-level pass rate (the "zero edits" metric).
    b = sum(1 for v, r in zip(doc_ok["vlm"], doc_ok["regex"]) if v and not r)
    c = sum(1 for v, r in zip(doc_ok["vlm"], doc_ok["regex"]) if r and not v)
    gain = 100 * (sum(doc_ok["vlm"]) - sum(doc_ok["regex"])) / n
    p = mcnemar_p(b, c)
    p50 = pct(lat["vlm"], 50)
    print(f"\nVLM vs regex: gain {gain:+.1f} pts, VLM-only wins {b}, regex-only wins {c}, "
          f"McNemar p={p:.3f}")
    verdict = gain >= MIN_GAIN_PTS and p < MAX_P and p50 <= MAX_P50_S
    print(f"Decision rule (gain >= {MIN_GAIN_PTS:.0f} pts, p < {MAX_P}, p50 <= {MAX_P50_S}s): "
          f"{'ADOPT VLM' if verdict else 'NOT MET'}  (p50={p50:.2f}s)")
    if n < 100:
        print(f"Note: n={n} is below ~100; intervals are wide, treat as a smoke test.")

    out = os.path.join(run_dir, "results.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Per-doc results: {out}")

    # Experiment log: one row per scoring call, in a tracked file, so every
    # config tried (and every time the test split was scored) stays on record.
    meta_path = os.path.join(run_dir, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    pct_of = lambda arm: round(100 * sum(doc_ok[arm]) / n, 1)
    row = {
        "scored_at": datetime.now().isoformat(timespec="seconds"),
        "run": os.path.basename(run_dir), "split": split, "key": key, "n": n,
        "backend": meta.get("backend"), "model": meta.get("model"),
        "prompt_sha1": meta.get("prompt_sha1"), "max_pixels": meta.get("max_pixels"),
        "crop_top": meta.get("crop_top"), "max_tokens": meta.get("max_tokens"),
        "git_commit": meta.get("git_commit"),
        "note": note or meta.get("note", ""),
        **{f"{arm}_all": pct_of(arm) for arm in ("regex", "vlm", "merge", "router", "oracle")},
        **{f"vlm_{k}": round(100 * sum(correct["vlm"][k]) / n, 1) for k in CORE},
        "halluc_pct": round(100 * halluc[0] / halluc[1], 1) if halluc[1] else None,
        "vlm_p50_s": round(p50, 2), "vlm_p95_s": round(pct(lat["vlm"], 95), 2),
        "gain_pts": round(gain, 1), "mcnemar_p": round(p, 3),
        "verdict": "ADOPT" if verdict else "NOT_MET",
    }
    with open(os.path.join(run_dir, "metrics.json"), "w") as f:
        json.dump(row, f, indent=2)  # same numbers, kept next to the run's predictions
    new_file = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new_file:
            w.writeheader()
        w.writerow(row)
    print(f"Logged to {LOG}")


def main():
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("run_dir", help="eval/runs/<run> folder")
    ap.add_argument("--init-labels", action="store_true",
                    help="append label stubs (pre-filled from VLM output) for this run")
    ap.add_argument("--split", default="test", choices=["dev", "test", "all"])
    ap.add_argument("--key", default="png", choices=["pdf", "png"],
                    help="labels column that holds the run's file names (pdf run vs screenshot run)")
    ap.add_argument("--note", default="",
                    help="log note for this scoring (default: the run's meta.json note)")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="score labels not yet marked verified (smoke tests only)")
    args = ap.parse_args()
    if args.init_labels:
        init_labels(args.run_dir)
    else:
        score(args.run_dir, args.split, args.allow_unverified, args.key, args.note)


if __name__ == "__main__":
    main()
