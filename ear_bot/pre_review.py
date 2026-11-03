"""Pre-review of an EAR PDF made by make_EAR.py v26.10.21 or later.

The PDF stores the report data as JSON (see scripts/EARpdf_to_yaml.py). The
bot reads it with Diego's own reader, compares it with the PR text and checks
that no section is empty. pre_review() is pure: report in, rows out.
"""

import importlib.util
import os
import re

import requests

_reader = os.path.join(os.path.dirname(__file__), "..", "scripts", "EARpdf_to_yaml.py")
_spec = importlib.util.spec_from_file_location("ear_data", _reader)
assert _spec and _spec.loader, f"cannot load {_reader}"
ear_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ear_data)

OK, NOTE, WARNING, FAIL = "ok", "note", "warning", "fail"
ICONS = {OK: "✅", NOTE: "ℹ️", WARNING: "⚠️", FAIL: "❌"}


def is_missing(value):
    return value is None or str(value).strip().lower() in {"", "na", "n/a", "none", "-"}


def check_link(url):
    """(level, detail) for a Hi-C contact map link, without downloading the file."""
    if not re.match(r"https?://", url, re.IGNORECASE):
        return FAIL, "not a web link"
    try:
        response = requests.get(url, stream=True, timeout=20)
        response.close()
    except requests.RequestException:
        return FAIL, "does not open"
    if response.status_code in (401, 403):
        return NOTE, "access refused, please check it by hand"
    if response.status_code >= 400:
        return FAIL, f"broken (HTTP {response.status_code})"
    if "text/html" in response.headers.get("Content-Type", ""):
        return NOTE, "opens a web page, not the file, please check it by hand"
    return OK, "file found"


def _code(value, limit=120):
    # PDF text goes into a GitHub comment: code spans keep @mentions and markdown inert.
    if value is None:
        return "missing"
    text = " ".join(str(value).replace("`", "'").split())
    return f"`{text[:limit]}`"


def _norm(value):
    return " ".join(str(value or "").split()).lower()


def pre_review(report, pr_fields, link_check=check_link):
    """Rows of (level, check, detail) for a stored report and the PR text fields."""
    rows = []
    species = report.get("species") or {}

    def compare(name, pr_value, pdf_value, level, same):
        if same:
            rows.append((OK, name, _code(pdf_value)))
        else:
            rows.append(
                (level, name, f"PR: {_code(pr_value)}, PDF: {_code(pdf_value)}")
            )

    pdf_species = species.get("Species")
    compare(
        "Species",
        pr_fields.get("Species"),
        pdf_species,
        FAIL,
        not is_missing(pdf_species)
        and _norm(pdf_species) == _norm(pr_fields.get("Species")),
    )
    pdf_tolid = species.get("ToLID")
    compare(
        "ToLID",
        pr_fields.get("ToLID"),
        pdf_tolid,
        WARNING,
        not is_missing(pdf_tolid)
        and str(pdf_tolid).strip() == str(pr_fields.get("ToLID") or "").strip(),
    )
    tags = report.get("tags")
    compare(
        "Project",
        pr_fields.get("Project"),
        tags,
        WARNING,
        str(pr_fields.get("Project")) in re.split(r"[,;\s]+", str(tags)),
    )

    links = report.get("hic_temp_links") or {}
    if not links:
        rows.append((WARNING, "PretextView link", "NA"))
    for hap, link in links.items():
        name = f"PretextView link {hap}"
        if is_missing(link):
            rows.append((WARNING, name, "NA"))
        else:
            level, detail = link_check(str(link).strip())
            rows.append((level, name, detail))

    def present(name, value, detail="present"):
        rows.append((WARNING, name, "NA") if is_missing(value) else (OK, name, detail))

    present("K-mer spectra", report.get("kmer_spectra"))
    for hap, count in (report.get("fcs_gx") or {"": "NA"}).items():
        present(f"FCS-GX {hap}".strip(), count, f"{_code(count)} flagged sequences")
    for hap, plot in (report.get("blobplots") or {"": "NA"}).items():
        present(f"BlobToolKit {hap}".strip(), plot)

    data = report.get("data_profile")
    if isinstance(data, dict):
        data = [f"{kind}: {cov}" for kind, cov in data.items()]
    if is_missing(data) or not isinstance(data, list):
        rows.append((WARNING, "Coverage", "NA"))
    else:
        empty = [
            line.split(": ", 1)[0]
            for line in data
            if is_missing(line.split(": ", 1)[-1])
        ]
        rows.append(
            (WARNING, "Coverage", "NA for " + ", ".join(map(_code, empty)))
            if empty
            else (OK, "Coverage", ", ".join(map(_code, data)))
        )

    present("Pipelines", report.get("pipeline"))
    present("Curation notes", report.get("curation_notes"))

    flags = report.get("flags") or []
    rows.append(
        (WARNING, "make_EAR flags", f"{len(flags)}: " + "; ".join(map(_code, flags)))
        if flags
        else (OK, "make_EAR flags", "0")
    )
    return rows


def format_comment(rows=(), version=None, error=None):
    """The pre-review comment. With error, the data could not be read: one FAIL."""
    if error:
        return (
            "### Pre-review\n"
            f"{ICONS[FAIL]} **EAR data in the PDF:** could not read it: {_code(error, limit=500)}.\n\n"
            "Please make the EAR with make_EAR.py v26.10.21 or later and upload the PDF it writes as it is. "
            "Programs that re-save PDFs (Ghostscript, print to PDF, online compressors) remove the data."
        )
    lines = [
        "### Pre-review",
        f"{ICONS[OK]} **EAR data in the PDF:** read, made with make_EAR {_code(version)}",
        "",
        "| | Check | Result |",
        "|---|---|---|",
    ]
    lines += [
        f"| {ICONS[level]} | {name} | {detail.replace('|', '/')} |"
        for level, name, detail in rows
    ]
    warnings = sum(level == WARNING for level, _, _ in rows)
    fails = sum(level == FAIL for level, _, _ in rows)
    lines.append("")
    lines.append(
        f"**{warnings} warnings, {fails} fails.** "
        + (
            "Please fix the fails in the PR text or the PDF, and I will check again."
            if fails
            else "Warnings don't stop the review: the supervisor and the reviewer will look at them."
        )
    )
    return "\n".join(lines)
