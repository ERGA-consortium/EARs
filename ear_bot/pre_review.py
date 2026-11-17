"""Pre-review of an EAR PDF made by make_EAR.py v26.11.10 or later.

The PDF stores the report data as JSON (see scripts/EARpdf_to_yaml.py). The
bot reads it with Diego's own reader, compares it with the PR text and checks
that no section is empty. pre_review() only reaches the network through link_check, which tests can replace.
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
SEVERITY = [OK, NOTE, WARNING, FAIL]  # a row for several haps shows the worst


def is_missing(value):
    if isinstance(value, (list, dict)):
        return not value
    return value is None or str(value).strip().lower() in {"", "na", "n/a", "none", "-"}


def check_link(url):
    """(level, detail) for a Hi-C contact map link, without downloading the file."""
    if not re.match(r"https?://", url, re.IGNORECASE):
        return FAIL, "not a web link"
    try:
        response = requests.get(url, stream=True, timeout=20)
        response.close()
    except requests.RequestException:
        # timeouts, DNS, SSL chains that browsers complete: not proof the link is wrong
        return NOTE, "unreachable now, check by hand"
    if response.status_code in (404, 410):
        return FAIL, f"broken (HTTP {response.status_code})"
    if response.status_code in (401, 403):
        return NOTE, "access refused, check by hand"
    if response.status_code >= 400:
        return NOTE, f"HTTP {response.status_code}, check by hand"
    if "text/html" in response.headers.get("Content-Type", ""):
        return NOTE, "web page, check by hand"
    return OK, "file found"


def _code(value, limit=120):
    # PDF text goes into a GitHub comment: code spans keep @mentions and markdown inert.
    if value is None:
        return "missing"
    text = " ".join(str(value).replace("`", "'").split())
    return f"`{text[:limit]}…`" if len(text) > limit else f"`{text}`"


def _norm(value):
    # "_Genus species_" or "*Genus species*" in the PR text still matches.
    return " ".join(str(value or "").strip("_* ").split()).lower()


def _worst(levels):
    return max(levels, key=SEVERITY.index)


def _per_hap(values, describe):
    """One row for all haps: (worst level, 'Hap1: … Hap2: …')."""
    results = [(hap, *describe(value)) for hap, value in values.items()]
    level = _worst([level for _, level, _ in results])
    # Hap names come from the PDF: anything but hap1, hap2… is shown as code, so it stays inert.
    label = lambda hap: hap.capitalize() if re.fullmatch(r"hap\d+", str(hap)) else _code(hap)  # noqa: E731
    return level, " ".join(f"{label(hap)}: {detail}." for hap, _, detail in results)


def pre_review(report, pr_fields, link_check=check_link):
    """Rows of (level, check, detail) for a stored report and the PR text fields.

    Section headings are rows with level None.
    """
    rows = []
    section = lambda name: rows.append((None, name, ""))  # noqa: E731
    species = report.get("species") or {}

    def compare(name, pr_value, pdf_value, level, same):
        if same:
            rows.append((OK, name, _code(pdf_value)))
        else:
            rows.append(
                (level, name, f"PR: {_code(pr_value)}, PDF: {_code(pdf_value)}")
            )

    section("Report vs PR body check")
    pdf_species = species.get("Species")
    compare(
        "Species name",
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

    section("Contact map info")
    links = report.get("hic_temp_links") or {}
    if links:
        level, detail = _per_hap(
            links,
            lambda link: (WARNING, "NA") if is_missing(link) else link_check(str(link).strip()),
        )
        rows.append((level, "Pretext link", detail))
    else:
        rows.append((WARNING, "Pretext link", "NA"))

    section("Metrics")
    metrics = report.get("quality_metrics")
    if isinstance(metrics, dict) and metrics:
        empty = [
            f"{name} ({hap.capitalize()})"
            for hap, values in metrics.items()
            for name, value in (values or {}).items()
            if is_missing(value)
        ]
        rows.append(
            (WARNING, "Missing fields", f"{len(empty)}: " + ", ".join(map(_code, empty)))
            if empty
            else (OK, "Missing fields", "`0`")
        )
    else:
        rows.append((WARNING, "Missing fields", "NA"))
    if is_missing(report.get("kmer_spectra")):
        rows.append((FAIL, "K-mer spectra plots", "NA"))
    else:
        rows.append((OK, "K-mer spectra plots", "present"))

    section("Contamination screening")
    fcs = report.get("fcs_gx") or {}
    blobs = report.get("blobplots") or {}
    if all(is_missing(v) for v in fcs.values()) and all(is_missing(v) for v in blobs.values()):
        needed = "NA. FCS-GX or a blobplot is needed for at least one hap"
        rows.append((FAIL, "FCS-GX", needed))
        rows.append((FAIL, "Blobplot", needed))
    else:
        if fcs:
            level, detail = _per_hap(
                fcs,
                lambda n: (WARNING, "NA")
                if is_missing(n)
                else (OK if str(n).strip() == "0" else WARNING, f"{_code(n)} flagged"),
            )
            rows.append((level, "FCS-GX", detail))
        else:
            rows.append((WARNING, "FCS-GX", "NA"))
        if blobs:
            level, detail = _per_hap(
                blobs, lambda p: (WARNING, "NA") if is_missing(p) else (OK, "present")
            )
            rows.append((level, "Blobplot", detail))
        else:
            rows.append((WARNING, "Blobplot", "NA"))

    section("Additional info")
    data = report.get("data_profile")
    if isinstance(data, dict):
        data = [f"{kind}: {cov}" for kind, cov in data.items()]
    if is_missing(data) or not isinstance(data, list) or not data:
        rows.append((FAIL, "Dataset info", "NA"))
    else:
        empty = [
            line.split(": ", 1)[0]
            for line in data
            if is_missing(line.split(": ", 1)[-1])
        ]
        rows.append(
            (WARNING, "Dataset info", "NA for " + ", ".join(map(_code, empty)))
            if empty
            else (OK, "Dataset info", ", ".join(map(_code, data)))
        )

    def present(name, value):
        rows.append((WARNING, name, "NA") if is_missing(value) else (OK, name, "present"))

    present("Pipelines info", report.get("pipeline"))
    present("Curation notes", report.get("curation_notes"))

    flags = report.get("flags") or []
    rows.append(
        (WARNING, "Warning flags", f"{len(flags)}: " + "; ".join(map(_code, flags)))
        if flags
        else (OK, "Warning flags", "`0`")
    )
    return rows


def format_comment(rows=(), version=None, error=None):
    """The pre-review comment. With error, the data could not be read: one FAIL."""
    if error:
        return (
            "### Automatic pre-review\n"
            f"{ICONS[FAIL]} **Metadata missing or unreadable:** {_code(error, limit=500)}.\n\n"
            "Please make the EAR with make_EAR.py v26.11.10 or later and upload the PDF it writes as it is. "
            "Programs that re-save PDFs (Ghostscript, print to PDF, online compressors) remove the data."
        )
    lines = [
        "### Automatic pre-review",
        f"{ICONS[OK]} **Metadata present.** PDF made with make_EAR {_code(version)}",
        "",
        "| | Check | Result |",
        "|---|---|---|",
    ]
    for level, name, detail in rows:
        if level is None:
            lines.append(f"| | **{name}** | |")
        else:
            lines.append(f"| {ICONS[level]} | {name} | {detail.replace('|', '/')} |")
    fails = [name for level, name, _ in rows if level == FAIL]
    warnings = any(level == WARNING for level, _, _ in rows)
    lines.append("")
    if fails:
        item = "item" if len(fails) == 1 else "items"
        lines.append(
            f"**{len(fails)} critical {item} to fix:** {', '.join(fails)}. "
            "Please fix them in the PR text or the PDF, and I will check again."
        )
    else:
        lines.append(
            "**No critical items found to fix.**"
            + (" The warnings are for the supervisor and reviewer to double-check." if warnings else "")
        )
    return "\n".join(lines)
