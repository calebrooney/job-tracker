"""Score `applied --test` runs against hand-verified labels.

Usage:
  python eval/run_eval.py --init-labels eval/runs/RUN      # build labels skeleton
  python eval/run_eval.py eval/runs/RUN [--split test]     # score a run

labels.jsonl (one JSON object per line, keyed by file name):
  {"file": "a.pdf", "split": "dev|test", "format": "workday", "verified": true,
   "fields": {"role": ..., "company": ..., "location": ..., "job_id": ..., "pay": ...}}

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

# Allow `python eval/run_eval.py` from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(HERE, "labels.jsonl")
CORE = ["role", "company", "location", "job_id", "pay"]
# Pre-registered decision rule (see the plan).
MIN_GAIN_PTS = 8.0
MAX_P = 0.05
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


def init_labels(run_dir):
    """Create labels.jsonl entries from a run's VLM output for hand verification."""
    existing = {r["file"] for r in load_jsonl(LABELS)} if os.path.exists(LABELS) else set()
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


def score(run_dir, split, allow_unverified):
    """Join predictions to labels, print the report, and write results.csv."""
    labels = {r["file"]: r for r in load_jsonl(LABELS)}
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
        truth = {k: norm(k, (lab["fields"] or {}).get(k)) for k in CORE}
        arms = arm_predictions(rec)
        # Docs where the VLM failed count as wrong for VLM-derived arms.
        for arm in ("vlm", "merge", "router"):
            arms.setdefault(arm, ({}, rec["regex"]["latency_s"] + rec["vlm"]["latency_s"]))
        ok = {}
        for arm, (fields, latency) in arms.items():
            ok[arm] = {k: int(norm(k, fields.get(k)) == truth[k]) for k in CORE}
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


def main():
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("run_dir", help="eval/runs/<run> folder")
    ap.add_argument("--init-labels", action="store_true",
                    help="append label stubs (pre-filled from VLM output) for this run")
    ap.add_argument("--split", default="test", choices=["dev", "test", "all"])
    ap.add_argument("--allow-unverified", action="store_true",
                    help="score labels not yet marked verified (smoke tests only)")
    args = ap.parse_args()
    if args.init_labels:
        init_labels(args.run_dir)
    else:
        score(args.run_dir, args.split, args.allow_unverified)


if __name__ == "__main__":
    main()
