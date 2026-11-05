#!/usr/bin/env python3
# EARpdf_to_yaml.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee
# note: AI tools were used to improve/clean/comment this version of the code
"""Writes the data stored as JSON in the EAR PDF's XMP metadata (format "EAR-data/1") as a YAML file.
"""
version = "v26.10.23"

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

import yaml

EAR_DATA_FORMAT = "EAR-data/1"
EAR_XMP_NS = "https://github.com/ERGA-consortium/EARs/ns/1.0/"


class ConvertError(Exception):
    """Message points to why the PDF cannot be converted"""


# ---------------------------------------------------------------------------
# Reading the stored data
# ---------------------------------------------------------------------------
def read_ear_data(pdf_path):
    """The data stored in an EAR PDF as a dict, or None when the PDF has none."""
    with open(pdf_path, "rb") as fh:
        raw = fh.read()
    found = None
    for packet in re.finditer(rb"<((?:[\w.-]+:)?)xmpmeta\b.*?</\1xmpmeta>", raw, re.S):
        try:
            node = ElementTree.fromstring(packet.group(0)).find(f".//{{{EAR_XMP_NS}}}data")
        except ElementTree.ParseError:
            continue
        if node is not None and node.text:
            found = json.loads(node.text)          # the last one wins, as for PDF updates
    return found


def report_id_of(report):
    """The Report ID: SHA-256 of the report as JSON with sorted keys, first 8 hex characters."""
    canonical = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def load(pdf_path):
    """The stored data of a PDF, checked; raises ConvertError with the reason otherwise."""
    try:
        data = read_ear_data(pdf_path)
    except OSError as e:
        raise ConvertError(f"cannot read the file ({e.strerror or e})")
    except json.JSONDecodeError:
        raise ConvertError("the stored data is not valid JSON")
    if data is None:
        raise ConvertError("no EAR data stored in this PDF (probably made before make_EAR.py v26.10.21, "
                           "or rewritten by another program, e.g. Ghostscript)")
    if not isinstance(data, dict) or data.get("format") != EAR_DATA_FORMAT:
        found = data.get("format") if isinstance(data, dict) else None
        raise ConvertError(f"unknown data format {found!r}; this script reads {EAR_DATA_FORMAT}")
    if not isinstance(data.get("report"), dict) or report_id_of(data["report"]) != data.get("report_id"):
        raise ConvertError("the stored data does not match its Report ID (edited after the PDF was made?)")
    return data


# ---------------------------------------------------------------------------
# From the stored report to YAML sections
# ---------------------------------------------------------------------------
# Sections that are pictures in the PDF (stored as plot file names): not written.
PICTURES = ("kmer_spectra", "blobplots", "alignments", "profiling")

BUSCO_LINE = re.compile(r"^(?:(?P<hap>hap\d+): )?(?P<tool>BUSCO|compleasm): (?P<ver>.*?) / Lineage: (?P<lineage>.*)$")


def genome_traits(traits):
    """{trait: {Expected, Observed}} -> {Expected: {trait: ...}, Observed: {trait: ...}}."""
    return {column: {name: values.get(column, "NA") for name, values in traits.items()}
            for column in ("Expected", "Observed")}


def gene_completeness(lines):
    """The tool/version/lineage line as {tool, ver, lineage}. When the haplotypes differ:
    {warning, hap1: {...}, hap2: {...}}. 'NA' when unknown; the lines as shown otherwise."""
    def entry(match):
        return {"tool": match["tool"], "ver": match["ver"], "lineage": match["lineage"]}

    if len(lines) == 1:
        if "/ Lineage:" not in lines[0] and lines[0].endswith(": NA"):
            return "NA"
        match = BUSCO_LINE.match(lines[0])
        return entry(match) if match and not match["hap"] else lines
    if lines and lines[0].startswith("Warning!"):
        out = {"warning": lines[0]}
        for line in lines[1:]:
            match = BUSCO_LINE.match(line)
            if not match or not match["hap"]:
                return lines
            out[match["hap"]] = entry(match)
        return out
    return lines


def data_profile(lines):
    """['HiFi: 30x', 'Hi-C: NA'] -> {HiFi: 30x, Hi-C: NA}; 'NA' stays NA. If a data type
    is listed twice, the lines are kept as shown."""
    if not isinstance(lines, list):
        return lines
    pairs = [line.split(": ", 1) if ": " in line else [line, ""] for line in lines]
    if len({key for key, _ in pairs}) != len(pairs):
        return lines
    return dict(pairs)


def yaml_sections(data):
    """The YAML content as a list of sections (dicts), in the order of the report.
    Fields this script does not know are kept, as stored, in a last section."""
    report = dict(data["report"])
    missing = object()

    def take(key, convert=None):
        value = report.pop(key, missing)
        return value if value is missing or convert is None else convert(value)

    header = {"ERGA Assembly Report": take("version"), "Tags": take("tags"), "Report ID": data["report_id"]}
    species = take("species")
    traits = take("genome_traits", genome_traits)
    ebp = take("ebp_metrics")
    sections = [
        header,
        species if isinstance(species, dict) else {},      # TxID, ToLID, Species, Class, Order
        {"Genome Traits": traits},
        {"EBP metrics": ebp if ebp is missing else {"EBP quality code": ebp}},
        {"Flags": take("flags")},
        {"Curation notes": take("curation_notes")},
        {"Hi-C contact map temp link": take("hic_temp_links")},
        {"Metrics": take("quality_metrics")},
        {"K-mer tool": take("kmer_tool"), "BUSCO": take("gene_completeness", gene_completeness)},
        {"FCS-GX flagged sequences": take("fcs_gx")},
        {"Data": take("data_profile", data_profile)},
        {"Pipeline": take("pipeline")},
        {"Submitter": take("submitter"), "Affiliation": take("affiliation")},
        {"Date and time": data.get("date", missing)},
    ]
    for key in PICTURES:
        report.pop(key, None)
    sections.append(report)
    sections = [{k: v for k, v in section.items() if v is not missing} for section in sections]
    return [section for section in sections if section]


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)        # list items indented under their key


def _represent_str(dumper, value):
    # Multi-line text (notes, pipeline) as a block that keeps its line breaks.
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _represent_str)


def to_yaml(data, source_name):
    """The YAML text: a comment line, then each section separated by a blank line."""
    header = f"# Data stored in {source_name}, written by EARpdf_to_yaml.py {version}\n\n"
    return header + "\n".join(
        yaml.dump(section, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
        for section in yaml_sections(data))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="EARpdf_to_yaml.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"EARpdf_to_yaml.py {version}: write the data stored in an EAR PDF "
                    f"(made by make_EAR.py v26.10.21 or later) as YAML.",
        epilog="The values are written as the PDF shows them. Exit code 1 when the PDF has no\n"
               "stored data, or the data does not match its Report ID.\n"
               "\n"
               "Examples:\n"
               "  python EARpdf_to_yaml.py mEleMax1_EAR.pdf             # writes mEleMax1_EAR.yaml\n"
               "  python EARpdf_to_yaml.py mEleMax1_EAR.pdf -o out.yaml\n"
               "  python EARpdf_to_yaml.py mEleMax1_EAR.pdf -o -        # prints the YAML")
    parser.add_argument("pdf", help="EAR PDF")
    parser.add_argument("-o", "--output", metavar="YAML",
                        help="output file (default: the PDF's name with .yaml; '-' prints it instead)")
    parser.add_argument("--version", action="version", version=f"EARpdf_to_yaml.py {version}")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    pdf = Path(args.pdf)
    try:
        text = to_yaml(load(pdf), pdf.name)
    except ConvertError as e:
        print(f"ERROR: {pdf}: {e}", file=sys.stderr)
        return 1
    if args.output == "-":
        sys.stdout.write(text)
        return 0
    out = Path(args.output) if args.output else pdf.with_suffix(".yaml")
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"ERROR: cannot write '{out}': {e.strerror or e}", file=sys.stderr)
        return 1
    print(f"YAML written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
