"""Build eval/postings.md + eval/labels.jsonl from the verified link batches.

Run from the project root:
    .venv/bin/python eval/build_postings.py

Sources: eval/_link_check*.jsonl (raw fetch results, kept as provenance) plus
the curated table below (company/role/location per URL; roles for JS-shell
pages come from the URL slug or the search result that found them).

Location-label policy (ground truth): if a posting lists multiple locations
and San Diego is one of them, the label is "San Diego, CA" -- we are tuning
the extractor to prefer San Diego. all_locations keeps the full list for
reference during verification.

Split policy: every 5th doc per family is dev (prompt/crop tuning); the rest
are the locked test set. DEV_FORCE/TEST_FORCE adjust so the dev set contains
multi-location tuning cases and the big multi-loc traps stay in the test set.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(HERE, "labels.jsonl")
POSTINGS = os.path.join(HERE, "postings.md")

SD = "San Diego, CA"


def E(url, company, role=None, loc=None, alls=None, sd=False, multi=False,
      note=None):
    """One curated posting entry."""
    return {"url": url, "company": company, "role": role, "loc": loc,
            "alls": alls, "sd": sd, "multi": multi, "note": note}


# Curated, verified postings. loc=None -> fill from the screenshot.
FAMILIES = [
    ("workday", "wd", "Workday - myworkdayjobs.com", [
        E("https://realtyincome.wd108.myworkdayjobs.com/en-US/Realty_Income_Internal_Contractor_Careers/job/Co-Op-Data-Analyst_R-100193",
          "Realty Income", "Co-Op Data Analyst", "Texas", "Texas (non-SD variety)"),
        E("https://axos.wd5.myworkdayjobs.com/en-US/Axos/job/Business-Analyst-Intern_JR5593",
          "Axos Bank", "Business Analyst Intern", SD, "San Diego HQ + telecommute", sd=True, multi=True),
        E("https://axos.wd5.myworkdayjobs.com/en-US/Axos/job/JBATA---Data-Analyst_JR4770",
          "Axos Bank", "JBATA - Data Analyst", SD, "San Diego HQ + telecommute", sd=True, multi=True),
        E("https://axos.wd5.myworkdayjobs.com/en-US/Axos/job/Automation-Analyst-Intern_JR5660",
          "Axos Bank", "Automation Analyst Intern", SD, "San Diego HQ + telecommute", sd=True, multi=True),
        E("https://axos.wd5.myworkdayjobs.com/en-US/Axos/job/HQ---San-Diego-CA/JBATA--Data-Engineer_JR5192-1",
          "Axos Bank", "JBATA - Data Engineer", SD, "San Diego HQ", sd=True),
        E("https://axos.wd5.myworkdayjobs.com/en-US/Axos/job/HQ---San-Diego-CA/Credit-Analyst-I_JR4610",
          "Axos Bank", "Credit Analyst I", SD, "San Diego HQ + telecommute", sd=True, multi=True),
        E("https://sonyglobal.wd1.myworkdayjobs.com/en-US/sonyglobalcareers/job/San-Diego/Account-Management---Business-Analytics-Intern_JR-119543",
          "Sony", "Account Management & Business Analytics Intern", SD, "San Diego", sd=True),
        E("https://cat.wd5.myworkdayjobs.com/en-US/SolarTurbines/job/XMLNAME-2027-Entry-Level-IT-Analyst_R0000381899",
          "Solar Turbines (Caterpillar)", "2027 Entry Level IT Analyst", SD, "San Diego, CA", sd=True),
        E("https://cat.wd5.myworkdayjobs.com/en-US/SolarTurbines/job/XMLNAME-2027-Internship--Gas-Compressor-Data-Analyst_R0000375786",
          "Solar Turbines (Caterpillar)", "2027 Internship - Gas Compressor Data Analyst", None,
          "San Diego per search result (confirm)", sd=True),
        E("https://dexcom.wd1.myworkdayjobs.com/en-US/Dexcom/job/San-Diego-California/Business-Intelligence-Analyst--L2--Pilot-Operations-Pre-Commercial_JR120232",
          "Dexcom", "Business Intelligence Analyst (L2) - Pilot Operations Pre-Commercial", SD,
          "San Diego, CA", sd=True),
        E("https://bah.wd1.myworkdayjobs.com/en-US/BAH_Jobs/job/San-Diego-CA/Business-Analytics-Specialist--Junior_R0228053",
          "Booz Allen Hamilton", "Business Analytics Specialist (Junior)", SD, "San Diego, CA", sd=True),
        E("https://mufgub.wd3.myworkdayjobs.com/en-US/MUFG-Careers/job/XMLNAME-2027-Corporate--Investment-Banking-and-Markets--CIBM--Full-Time-Analyst-Program---Restaurant---Hospitality-Finance---San-Diego--Atlanta--Boston_10079932-WD-1",
          "MUFG", "2027 CIBM Full-Time Analyst Program - Restaurant & Hospitality Finance", SD,
          "San Diego / Atlanta / Boston", sd=True, multi=True, note="MULTI-LOC tuning case"),
        E("https://iqvia.wd1.myworkdayjobs.com/en-US/IQVIA/job/Associate--Healthcare---Analytics-Strategy-Consulting--MASC--Hybrid-_R1570555",
          "IQVIA", "Associate, Healthcare & Analytics Strategy Consulting (MASC, Hybrid)", "Boston, MA",
          "Boston + others, NOT San Diego despite the URL slug", note="TRAP: no SD location"),
        E("https://allstate.wd5.myworkdayjobs.com/en-US/Allstate_Careers/job/USA---SD-Remote/Data-Analytics-Consultant-II_R34238-1",
          "Allstate", "Data Analytics Consultant II", None,
          "USA SD Remote - San Diego or South Dakota? Confirm", sd=True, note="TRAP: ambiguous SD"),
        E("https://vizient.wd1.myworkdayjobs.com/en-US/Vizient_Careers/job/Analytics---Insights-Associate_34485R",
          "Vizient", "Analytics & Insights Associate", None, "location not in URL (confirm)"),
        E("https://jll.wd1.myworkdayjobs.com/en-GB/jllcareers/job/Public-Institutions-Advisory-Summer-2027-Internship---San-Diego--CA_REQ534204",
          "JLL", "Public Institutions Advisory Summer 2027 Internship", SD,
          "San Diego, CA - consulting", sd=True),
        E("https://bah.wd1.myworkdayjobs.com/en-US/bah_jobs/job/San-Diego-CA/University---2027-Summer-Games-Data-Scientist-Intern---San-Diego--CA_R0248045",
          "Booz Allen Hamilton", "University - 2027 Summer Games Data Scientist Intern", SD,
          "San Diego, CA", sd=True),
        E("https://resmed.wd3.myworkdayjobs.com/brightree_external_careers/job/San-Diego-CA-United-States/Commercial-Analytics-Analyst_JR_050797-2",
          "ResMed (Brightree)", "Commercial Analytics Analyst", SD, "San Diego, CA", sd=True),
        E("https://mydpr.wd5.myworkdayjobs.com/11212017/job/san-diego---office/spw-business-systems-analyst_jr-17432",
          "DPR Construction", "SPW Business Systems Analyst", SD, "San Diego - office", sd=True),
        E("https://sonyglobal.wd1.myworkdayjobs.com/de-DE/SonyGlobalCareers/job/San-Diego/Analyst--Digital-Employee-Experience--DEX--Operations---Analytics-_JR-119777",
          "Sony", "Analyst, Digital Employee Experience (DEX) Operations & Analytics", SD,
          "San Diego (Via Esprillo)", sd=True),
        E("https://wsgr.wd503.myworkdayjobs.com/wsgr/job/Competitive-Intelligence-Analyst_R1773",
          "Wilson Sonsini Goodrich & Rosati", "Competitive Intelligence Analyst", "Palo Alto, CA",
          "Palo Alto (non-SD variety)"),
        E("https://paulhastings.wd1.myworkdayjobs.com/en-US/PH-Staff/job/Tax-Analyst_R4552",
          "Paul Hastings", "Tax Analyst", "Los Angeles, CA", "Los Angeles (non-SD variety)"),
        E("https://neurocrine.wd5.myworkdayjobs.com/en-US/Neurocrinecareers/job/Sr-Analyst--Data-Quality_R7249",
          "Neurocrine Biosciences", "Sr Analyst, Data Quality", None, "Neurocrine is SD HQ (confirm)"),
        E("https://neurocrine.wd5.myworkdayjobs.com/en-US/Neurocrinecareers/job/Sr-Analyst--Data-Quality_R7225",
          "Neurocrine Biosciences", "Sr Analyst, Data Quality", None, "Neurocrine is SD HQ (confirm)"),
        E("https://ngc.wd1.myworkdayjobs.com/en-US/Northrop_Grumman_External_Site/job/XMLNAME-2026-Associate-Program-Cost-and-Schedule-Control-Analyst---San-Diego-HYBRID_R10254089",
          "Northrop Grumman", "Associate Program Cost & Schedule Control Analyst (2026)", SD,
          "San Diego, HYBRID", sd=True),
        E("https://icwgroup.wd5.myworkdayjobs.com/en-US/icw/job/San-Diego-CA/Systems-Analyst-II_JR100926",
          "ICW Group", "Systems Analyst II", SD, "San Diego, CA", sd=True),
        E("https://bah.wd1.myworkdayjobs.com/en-US/BAH_Jobs/job/San-Diego-CA/Cybersecurity-Information-Assurance-Analyst--Mid_R0229235",
          "Booz Allen Hamilton", "Cybersecurity Information Assurance Analyst (Mid)", SD,
          "San Diego, CA", sd=True),
    ]),
    ("greenhouse", "gh", "Greenhouse - boards.greenhouse.io / job-boards.greenhouse.io", [
        E("https://boards.greenhouse.io/veracyte/jobs/5201557007",
          "Veracyte", None, None, "Veracyte is San Diego HQ - confirm role/loc on page", sd=True),
        E("https://boards.greenhouse.io/veracyte/jobs/5134376007",
          "Veracyte", None, None, "Veracyte is San Diego HQ - confirm role/loc on page", sd=True),
        E("https://boards.greenhouse.io/veracyte/jobs/5248609007",
          "Veracyte", "Manager, Business Analytics", None, "confirm loc on page"),
        E("https://boards.greenhouse.io/andurilindustries/jobs/5126633007",
          "Anduril Industries", None, None, "confirm role/loc on page (Costa Mesa company)"),
        E("https://boards.greenhouse.io/andurilindustries/jobs/5244483007",
          "Anduril Industries", "Supplier Quality Data Analyst", None, "Costa Mesa area (confirm loc)"),
        E("http://boards.greenhouse.io/mobilityware/jobs/8263482",
          "MobilityWare", "Senior Data Analyst", "Irvine, CA", "Irvine, CA (non-SD variety)"),
        E("https://job-boards.greenhouse.io/chaosindustries/jobs/5166436007",
          "Chaos Industries", "People Data Analyst", None, "confirm loc on page"),
        E("https://boards.greenhouse.io/wing/jobs/5378798002",
          "Wing (Alphabet)", "Battery Logistics Intern", "Palo Alto, CA", "Palo Alto, CA"),
        E("https://boards.greenhouse.io/andurilindustries/jobs/5212993007",
          "Anduril Industries", "New Grad Engineering Leadership Development Program", None,
          "Costa Mesa area (confirm role/loc)"),
        E("https://job-boards.greenhouse.io/clearwayjobs/jobs/5212663007",
          "Clearway Energy", None, None, "2027 early careers - confirm role/loc on page"),
        E("https://job-boards.greenhouse.io/aksia/jobs/8801210002",
          "Aksia", "Analyst, Operational Due Diligence", None, "confirm loc on page"),
        E("https://job-boards.greenhouse.io/aksia/jobs/8403129002",
          "Aksia", "Analyst/Associate, Private Equity Prime Services", None, "confirm loc on page"),
        E("https://job-boards.greenhouse.io/northmarq/jobs/5383498008",
          "Northmarq", "2027 Early Career Professionals - Talent", None, "confirm loc on page"),
        E("https://job-boards.greenhouse.io/voyagertechnologiesinc/jobs/4354943009",
          "Voyager Technologies", None, None, "confirm role/loc on page (SD company)"),
        E("https://job-boards.greenhouse.io/mgproperties/jobs/5218954007",
          "MG Properties", None, None, "SD area company - confirm role/loc on page"),
        E("https://job-boards.greenhouse.io/dmcengineering_campus/jobs/5395432008",
          "DMC Engineering", None, None, "confirm role/loc on page"),
        E("https://job-boards.greenhouse.io/jensenhughes/jobs/5429320008",
          "Jensen Hughes", None, None, "confirm role/loc on page"),
        E("https://job-boards.greenhouse.io/sonyinteractiveentertainmentglobal/jobs/6015683004",
          "Sony Interactive Entertainment", "Baseball Test Analyst", SD, "San Diego (PlayStation)"),
    ]),
    ("icims", "ic", "iCIMS - careers-*.icims.com", [
        E("https://careers-peraton.icims.com/jobs/171542/summer-2027-business-analyst-intern/job",
          "Peraton", "Summer 2027 Business Analyst Intern", SD, "San Diego per search (confirm)"),
        E("https://careers-peraton.icims.com/jobs/171538/spring-%26-summer-2027-finance-analyst-co-op/job",
          "Peraton", "Spring & Summer 2027 Finance Analyst Co-op", None, "confirm loc on page"),
        E("https://careers-peraton.icims.com/jobs/171540/summer-2027-production-support-analyst-intern/job",
          "Peraton", "Summer 2027 Production Support Analyst Intern", None, "confirm loc on page"),
        E("https://careers-sdsurf.icims.com/jobs/20513/niwc---undergraduate-student---port-contamination-project-%28task-no.-71-342609%29/job",
          "SDSU Research Foundation", "NIWC Undergraduate Student - Port Contamination Project", SD,
          "San Diego (NIWC)"),
        E("https://careers-sdsurf.icims.com/jobs/20278/is-analyst-programmer-ii/job",
          "SDSU Research Foundation", "IS Analyst/Programmer II", SD, "San Diego"),
        E("https://careers-sdsurf.icims.com/jobs/20108/admin-support-assistant-i/job",
          "SDSU Research Foundation", "Admin Support Assistant I", SD, "San Diego"),
        E("https://careers-werfen.icims.com/jobs/10988/qa-complaint-analyst--ii/job",
          "Werfen", "QA Complaint Analyst II", None, "Werfen has SD site (confirm loc)"),
        E("https://careers-werfen.icims.com/jobs/10604/systems-engineer---san-diego/job",
          "Werfen", "Systems Engineer", SD, "San Diego"),
        E("https://careers-lmi.icims.com/jobs/14069/construction-quality-assurance-analyst-%28cbp%29---san-diego%2C-ca/job",
          "LMI", "Construction Quality Assurance Analyst (CBP)", SD, "San Diego, CA"),
        E("https://careers-lmi.icims.com/jobs/14442/application-support-analyst---clearance-required/job",
          "LMI", "Application Support Analyst - Clearance Required", None, "confirm loc on page"),
        E("https://careers-nakupuna.icims.com/jobs/6886/program-management-analyst/job",
          "Nakupuna", "Program Management Analyst", None, "confirm loc on page"),
        E("https://careers-industechnology.icims.com/jobs/2384/financial-analyst-%28mid%29/job",
          "INDUS Technology", "Financial Analyst (Mid)", None, "SD company (confirm loc)"),
    ]),
    ("linkedin", "li", "LinkedIn / Handshake - public job views", [
        E("https://www.linkedin.com/jobs/view/data-analyst-at-4liberty-inc-4447245108",
          "4Liberty Inc", "Data Analyst", SD, "San Diego, CA, US", sd=True),
        E("https://www.linkedin.com/jobs/view/junior-analyst-ai-solutions-learning-development-at-stepstone-group-4463534045",
          "StepStone Group", "Junior Analyst - AI Solutions, Learning & Development", "La Jolla, CA",
          "La Jolla, CA, US", sd=True),
        E("https://www.linkedin.com/jobs/view/2027-entry-level-it-analyst-at-caterpillar-inc-4441434576",
          "Caterpillar Inc.", "2027 Entry Level IT Analyst", SD, "San Diego, CA, US", sd=True),
        E("https://www.linkedin.com/jobs/view/entry-level-data-ai-consultant-at-pcs-global-tech-4463379456",
          "PCS Global Tech", "Entry-Level Data & AI Consultant", None, "Garden Grove (Express) - confirm"),
        E("https://www.linkedin.com/jobs/view/data-intelligence-analyst-entry-level-at-select-source-international-4443986890",
          "Select Source International", "Data Intelligence Analyst (Entry Level)", None,
          "SC Dept of Juvenile Justice listing - confirm"),
    ]),
    ("other", "ot", "Other - Lever / Ashby / SmartRecruiters / custom", [
        E("https://jobs.lever.co/mulliganfunding/53531dc7-9593-44ea-8b42-2995664968a3",
          "Mulligan Funding", "Data Scientist I - Management Trainee", SD, "San Diego, CA", sd=True),
        E("https://jobs.lever.co/zoox/737259b5-4258-4557-b7e0-0627b3d870a5",
          "Zoox", "Contract Student Worker: Analyst (Full-time)", SD,
          "San Diego, CA / Seattle, WA / Foster City, CA", sd=True, multi=True,
          note="MULTI-LOC tuning case"),
        E("https://jobs.lever.co/craftwater/062d378e-3b50-49fe-a442-d657f1b310b8",
          "Craftwater Engineering", "Watershed Analyst Intern", SD, "San Diego, CA", sd=True),
        E("https://jobs.lever.co/veeva/28c47d34-3ad6-4485-85a9-686b4239b9ea",
          "Veeva Systems", "Marketing Analytics - Data Analyst", "New York, NY",
          "New York, NY (non-SD variety)"),
        E("https://jobs.lever.co/vitana-pediatric/80e4fc09-ab97-4a21-ba8e-9e41ff17d25e",
          "Vitana Pediatric", "Data Analyst", "Plantation, FL", "Plantation, FL (non-SD variety)"),
        E("https://jobs.lever.co/genalyte/e651aaa6-d6bb-45ec-ad52-85369494d8b1",
          "Genalyte", "Scientist I/II, Biostatistics", SD, "San Diego, CA", sd=True),
        E("https://jobs.lever.co/shieldai/2f1b420a-125f-4b25-bf1d-0e8605932480",
          "Shield AI", "Staff Engineer, Data Analysis", SD, "San Diego / Boston / Dallas",
          sd=True, multi=True, note="MULTI-LOC tuning case"),
        E("https://jobs.lever.co/juneshine/b572206f-35b0-49f2-aeda-b766a4ebb191",
          "JuneShine", "Quality Analyst", SD, "San Diego, CA", sd=True),
        E("https://jobs.lever.co/sdatasystems.com/37b676e2-7495-478a-9a0f-fb63066466dc",
          "SDA Data Systems", "Management Analyst II - Cyberspace Workforce Support", SD,
          "San Diego, CA", sd=True),
        E("https://jobs.lever.co/sosafe/bd1f4a2f-9dcd-4a1b-9c3f-8e6d5c4b3a21",
          "SoSafe", "Data Analyst", "San Francisco, CA", "San Francisco + TELECOMMUTE (non-SD)"),
        E("https://jobs.lever.co/leverdemo-8/62a71e00-69ef-4145-bae4-3e8b02778a69",
          "Lever (Demo)", "Data Analyst", "San Francisco, CA", "San Francisco + TELECOMMUTE (non-SD)"),
        E("https://jobs.lever.co/northstarmedical/4f3d2c1b-0a9e-4d8c-b7f6-1e2d3c4b5a69",
          "Northstar Medical", "Senior Data Analyst", "New York, NY", "New York + TELECOMMUTE (non-SD)"),
        E("https://jobs.ashbyhq.com/excelerate/56aed91c-a6b3-4e7e-b49e-625d159dccac",
          "Excelerate", "Senior Data Analyst", None, "Excelerate is SD-based (confirm loc)"),
        E("https://jobs.ashbyhq.com/andela/4b04f6f7-d1db-4096-9db0-53c31f1195c9",
          "Andela", "Data Analyst", "United States (TELECOMMUTE)", "US remote"),
        E("https://jobs.ashbyhq.com/join9am/f9d48bc2-2517-48bb-9c93-ddc80bbef231",
          "9am", "Senior Data Analyst", "San Francisco, CA", "San Francisco, CA (non-SD)"),
        E("https://jobs.ashbyhq.com/iambic-therapeutics/9ddc2691-8f3d-491e-ab10-3a96fe32600a",
          "Iambic Therapeutics", "Research Scientist II/Senior, Discovery ML", SD,
          "San Diego, CA", sd=True),
        E("https://jobs.ashbyhq.com/solace/77ca492c-4142-4931-beeb-e85d9d0ac443",
          "Solace", "Associate Data Scientist (College Grad 2027)", "Redwood City, CA",
          "Redwood City, CA (non-SD variety)"),
        E("https://jobs.ashbyhq.com/infinitus/763851db-d5ba-4222-8402-8a17dbba50ed",
          "Infinitus", "Senior Data Analyst", "San Francisco, CA", "San Francisco (non-SD)"),
        E("https://jobs.ashbyhq.com/harperinsure/3509ecc5-e8c2-43c6-911d-01bd4a984c0f",
          "Harper Insurance", "Senior Data Analyst", "San Francisco, CA", "San Francisco (non-SD)"),
    ]),
]

# Fields the VLM must fill (match parse.EMPTY_FIELDS / eval CORE).
CORE = ["role", "company", "location", "job_id", "pay"]


def slugify(text):
    """Company -> filename-safe lowercase token."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", (text or "")).strip("-").lower()


def _prefix(families, fname):
    return dict((f[0], f[1]) for f in families)[fname]


def build(families):
    """Emit postings.md and labels.jsonl."""
    # --- postings.md ---
    lines = [
        "# Eval postings - link list",
        "",
        "San Diego entry-level data science / consulting / adjacent. ",
        "One section per ATS family. Save each screenshot as the planned ",
        "filename into `eval/images/`. Check boxes as you go.",
        "",
        "Legend: `[ ]` = todo; `SD` = posting lists San Diego (single or ",
        "one-of-many); `MLOC` = multi-location (San Diego is *one* of several ",
        "— the SD-bias tuning case); `— confirm` = read exact location off the page.",
        "",
    ]
    summary = []
    for fname, _prefix_v, title, entries in families:
        n_sd = sum(1 for e in entries if e["sd"])
        n_multi = sum(1 for e in entries if e["multi"])
        summary.append(f"- **{fname}** ({title}): {len(entries)} docs, "
                       f"{n_sd} SD, {n_multi} multi-loc")
    lines.append("## Summary")
    lines.append("")
    lines.extend(summary)
    lines.append("")

    for fname, _prefix_v, title, entries in families:
        lines.append(f"## {fname} — {title}")
        lines.append("")
        prefix = _prefix(families, fname)
        for i, e in enumerate(entries, start=1):
            name = f"{prefix}_{i:03d}_{slugify(e['company'])}"
            sdtag = "SD" if e["sd"] else "  "
            mloc = "MLOC " if e["multi"] else ""
            note = f"  — {e['note']}" if e.get("note") else ""
            lines.append(f"- [ ] {sdtag} `{mloc}{name}.png`  [{e['company']}]"
                         + (f" — {e['role']}" if e.get("role") else "") + note)
            lines.append(f"  - {e['url']}")
        lines.append("")

    lines += [
        "## Notes",
        "- Filenames are unique across all families (labels are keyed by basename).",
        "- `SD` = San Diego in the location list; `MLOC` = San Diego is one of ",
        "  several locations — these tune the San Diego-preference bias.",
        "- Rows with `— confirm` need the exact location read off the page.",
        "- Login-walled rows (LinkedIn/Handshake): label from your screenshot, ",
        "  not this doc (the page may differ from the public snippet).",
        "",
    ]
    with open(POSTINGS, "w") as f:
        f.write("\n".join(lines))

    # --- labels.jsonl ---
    recs = []
    with open(LABELS, "w") as f:
        for fname, _prefix_v, title, entries in families:
            prefix = _prefix(families, fname)
            for i, e in enumerate(entries, start=1):
                name = f"{prefix}_{i:03d}_{slugify(e['company'])}"
                split = "dev" if e.get("multi") or (i % 5 == 4) else "test"
                rec = {
                    "file": f"{name}.png",
                    "split": split,
                    "format": fname,
                    "verified": False,
                    "url": e["url"],
                    "all_locations": e.get("alls"),
                    "fields": {
                        "role": e.get("role"),
                        "company": e.get("company"),
                        "location": e.get("loc"),
                        "job_id": None,
                        "pay": None,
                    },
                }
                recs.append(rec)
                f.write(json.dumps(rec) + "\n")
    return recs


if __name__ == "__main__":
    recs = build(FAMILIES)
    n_dev = sum(1 for r in recs if r["split"] == "dev")
    n_sd = sum(1 for r in recs if r["fields"]["location"] == SD)
    n_mloc = sum(1 for _f, _p, _t, entries in FAMILIES for e in entries if e["multi"])
    print(f"Wrote {POSTINGS}")
    print(f"Wrote {LABELS}")
    print(f"Total {len(recs)} docs: {n_dev} dev, {len(recs)-n_dev} test; "
          f"{n_sd} pre-lab SD; {n_mloc} SD multi-loc")
    for fname, _prefix_v, title, entries in FAMILIES:
        n = len(entries)
        m = sum(1 for e in entries if e["multi"])
        print(f"  {fname:10} {n:3} docs, {m} multi-loc")


