# make_EAR.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee
# note: AI tools were used to improve/clean/comment this version of the code
EAR_version = "v26.10.21"

import sys
import argparse
import logging
import re
import yaml
import os
import io
import pytz
import requests
import glob
import math
import json
import hashlib
from urllib.parse import quote
from datetime import datetime
from xml.sax.saxutils import escape
from xml.etree import ElementTree
from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.pdfbase.pdfdoc import PDFStream, PDFName, PDFDictionary

# Write image streams as raw binary instead of ASCII85 text: ~20% smaller PDF, identical content.
rl_config.useA85 = 0


# ===========================================================================
# GENERIC HELPERS
# ===========================================================================

# valid haplotype keys, in canonical order
VALID_HAPS = ["hap1", "hap2", "hap3", "hap4"]

# ---- EBP chromosome-scale calibration --------------------------------------
# Scaffold target is RELATIVE (scaffold N50 ~= chromosomal N50) rather than a fixed
# bp value, and the standard adds that for species whose chromosome N50 is below a
# megabase the target becomes C.C.Q40 - a 1 Mb contig bar is unreachable when a
# whole chromosome is smaller than that.
#
# We don't define a numeric test for diving the 'C', in ERGA practice it's more a
# curation judgement backed by Hi-C signal, etc. Everything below is therefore a
# HEURISTIC PROXY and is always reported as "estimated".
#
# CHR_SCALE_FRAC operationalises "~=": a level counts as chromosome-scale when its
# N50 reaches 90% of the mean chromosome, which leaves karyotype variance (the mean
# is not the median chromosome) in the assembly's favour.
CHR_SCALE_FRAC = 0.90        # N50 >= this fraction of a chromosome -> 'C'
EBP_CONTIG_N50_BAR = 1_000_000   # the '6' of 6.C.Q40, before any small-chromosome cap

# Images are embedded at this resolution at their printed size (see make_image).
IMAGE_DPI = 300


class EARError(Exception):
    """A problem with the inputs that stops the report. The message is written to
    the log and shown in the terminal, so it should tell the user what to fix."""


def parse_haploid_number(value):
    """Return the haploid chromosome number as a positive int, or raise ValueError
    with the reason.

    Accepts 38, 38.0, "38", "38.0", "n=38", "n = 38". Deliberately rejects anything
    else - "2n=76", "19-21", "38 (2n)", "21,5" because every threshold downstream
    is scaled by this number, and a misread that inflates n shrinks the estimated
    chromosome size, which biases every check toward a false PASS. Refusing is
    always safer than guessing.
    """
    if value is None or isinstance(value, bool):    # bool is an int subclass; never a karyotype
        raise ValueError("it is not a number")

    if isinstance(value, (int, float)):
        number = value
    else:
        text = str(value).strip()
        if re.match(r'^2\s*n\s*=', text, re.IGNORECASE):
            raise ValueError("it looks like a diploid count (2n); the haploid number n is expected")
        # Strip an explicit "n=" prefix, then require a bare number. No commas are
        # stripped: "21,5" is a decimal comma in some locales and would otherwise
        # be read as 215.
        prefix = re.match(r'^n\s*=\s*(.*)$', text, re.IGNORECASE)
        if prefix:
            text = prefix.group(1).strip()
        try:
            number = float(text)
        except (ValueError, OverflowError):
            raise ValueError("it is not a plain number")

    try:
        if not (number > 0) or not float(number).is_integer():
            raise ValueError("it is not a positive whole number")
        return int(number)
    except OverflowError:
        raise ValueError("it is not a usable number")


# Words that mean "no value" when written for a field (case-insensitive). YAML itself
# already reads an empty value, null and ~ as nothing. "unknown" is NOT in the list:
# it is a meaningful answer for sex.
NO_VALUE_WORDS = {"na", "n/a", "n.a.", "none", "null", "nan", "-", "--", "—"}


def is_na(value):
    """True if the user wrote a 'no value' word (NA, n/a, none, "-", ...) on purpose."""
    return isinstance(value, str) and value.strip().lower() in NO_VALUE_WORDS


def is_empty(value):
    """True if a value is missing, blank, a 'no value' word, or still holds a template placeholder."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):      # YAML .nan
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "" or is_na(stripped) or stripped.startswith("<Insert") or stripped.startswith("<insert"):
            return True
    return False


def note_missing(what, value):
    """Log an optional input that is not available: a warning when it was left out,
    only an info line when the user wrote NA (or another 'no value' word) on purpose."""
    if is_na(value):
        logging.info(f"# {what} given as '{value.strip()}'; using NA.")
    else:
        logging.warning(f"# {what} is missing; using NA.")


def link_target(link):
    """A web link as written, with https:// added when it has no scheme (e.g. 'www.x.org'),
    which PDF viewers would otherwise not open as a web page."""
    text = str(link).strip()
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', text) or text.lower().startswith("mailto:"):
        return text
    return "https://" + text


LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

_QUOTED_PATH = re.compile(r"'([^']*[/\\][^']*)'")
_BARE_PATH = re.compile(r"(?<![\w.:/\\])((?:/|[A-Za-z]:\\)[^\s'\"|,;]+)")


def _file_name(path):
    if "://" in path:          # a web link, not a file
        return path
    return re.split(r"[/\\]", path.rstrip("/\\"))[-1] or path     # both / and \\ (Windows)


def _looks_like_path(text):
    """Absolute or ./ ../ paths, or folder/file.ext - but not text like 'N/A' or web links."""
    if "://" in text:
        return False
    if re.match(r"^(/|~|\.{1,2}[/\\]|[A-Za-z]:[/\\])", text):
        return True
    return re.search(r"[/\\][^/\\]*\.\w+$", text) is not None


def shorten_paths(line):
    """A log line with file paths reduced to file names (web links are kept), so the
    copy of the log stored in the PDF does not reveal folder structures or user names."""
    line = _QUOTED_PATH.sub(lambda m: "'" + (_file_name(m.group(1)) if _looks_like_path(m.group(1))
                                             else m.group(1)) + "'", line)
    return _BARE_PATH.sub(lambda m: _file_name(m.group(1)), line)


class _LogCapture(logging.Handler):
    """Collects this run's log lines (paths shortened) to store in the PDF."""
    def __init__(self):
        super().__init__(logging.INFO)
        self.setFormatter(LOG_FORMAT)
        self.lines = []

    def emit(self, record):
        self.lines.append(xml_safe(shorten_paths(self.format(record))))


# ---- Report data stored in the PDF ---------------------------------------
# Everything the report shows except the pictures (plots are listed by file name), plus
# the run log, is stored as JSON inside the PDF:
#   {"format": "EAR-data/1", "report_id", "date", "report": {...}, "log": [...]}
# "report" holds the values exactly as displayed (all text; "NA" where the PDF shows NA).
# The Report ID is the first 8 hex characters of the SHA-256 of "report" serialised with
# sorted keys (report_id_of), so anyone can check that stored data and printed ID match.
# The JSON sits uncompressed in the PDF's XMP metadata, readable in a text editor and by
# read_ear_data() with the standard library only. Only the original PDF keeps it: tools
# that rewrite PDFs (Ghostscript, "print to PDF", online compressors) probably drop it.
EAR_DATA_FORMAT = "EAR-data/1"
EAR_XMP_NS = "https://github.com/ERGA-consortium/EARs/ns/1.0/"
# Characters XML cannot hold (control characters, lone surrogates from undecodable file
# names, U+FFFE/U+FFFF); they are stored as U+FFFD so the metadata always stays valid.
_XML_UNSAFE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_safe(value):
    """value (a string, or a dict/list of them) with characters XML cannot hold replaced."""
    if isinstance(value, dict):
        return {xml_safe(k): xml_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [xml_safe(v) for v in value]
    return _XML_UNSAFE.sub("\ufffd", value) if isinstance(value, str) else value


def report_id_of(report):
    """The Report ID of a report section: SHA-256 of its JSON with sorted keys, first 8 hex characters."""
    canonical = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def xmp_packet(data, title, author, subject, creator):
    """XMP metadata: the standard document fields (some viewers read these instead of the
    PDF's Info dictionary) plus the data as indented JSON in a CDATA block. Text that could
    end the block or the packet early ("]]>", "</") is written with JSON escapes, which any
    JSON parser reads back as the same characters."""
    cdata = (json.dumps(data, ensure_ascii=False, indent=2)
             .replace("</", "<\\/").replace("]]>", "]]\\u003e"))
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        f'    xmlns:ear="{EAR_XMP_NS}">\n'
        f'   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{escape(title)}</rdf:li></rdf:Alt></dc:title>\n'
        f'   <dc:creator><rdf:Seq><rdf:li>{escape(author)}</rdf:li></rdf:Seq></dc:creator>\n'
        f'   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(subject)}</rdf:li></rdf:Alt></dc:description>\n'
        f'   <xmp:CreatorTool>{escape(creator)}</xmp:CreatorTool>\n'
        f'   <ear:data><![CDATA[\n{cdata}\n]]></ear:data>\n'
        '  </rdf:Description>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>')


def read_ear_data(pdf_path):
    """The report data stored in an EAR PDF as a dict, or None when the PDF has none
    (made by the old make_EAR.py, or rewritten by a tool that drops or compresses the
    metadata). The XMP is parsed as XML, so it is still found if another program re-saves
    it in a different but equivalent form. Standard library only."""
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


def read_text_file(path):
    """Read a text file, tolerating non-UTF-8 encodings.
    User notes are sometimes saved in encoding containing accented characters,
    which raises a UnicodeDecodeError under strict UTF-8. So, Try UTF-8 first,
    then CP1252, then Latin-1 (weird but decodes any byte and so never fails).
    Logs which non-UTF-8 encoding was used so can re-save as UTF-8 if weird result.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            logging.warning(f"# Text file '{path}' is not valid UTF-8; decoded as {enc}. "
                            f"Re-save it as UTF-8 if any characters look wrong.")
            return text
        except UnicodeDecodeError:
            continue
    # just in case...
    return raw.decode("utf-8", errors="replace")


def resolve_text_field(value, field_name):
    """Resolve a free-text field (PIPELINE, curation_notes).
    If the value is the path of an existing file, return the file's contents;
    otherwise return the value as literal text. Returns None (-> "NA") when empty or
    when the file cannot be read. A value that looks like a file name but does not
    exist is shown as text, with a warning (most likely a wrong relative path).
    """
    if is_empty(value):
        return None
    text = str(value).strip()
    if "\n" not in text and os.path.isfile(text):
        try:
            content = read_text_file(text).strip()
        except OSError as e:
            logging.warning(f"# Could not read {field_name} file '{os.path.abspath(text)}': {e}. Using NA.")
            return None
        logging.info(f"# {field_name} read from file '{os.path.abspath(text)}'.")
        return content if content else None
    if "\n" not in text and re.search(r'\.(txt|md)$', text, re.IGNORECASE):
        logging.warning(f"# {field_name} looks like a file name ('{text}') but no such file was "
                        f"found from '{os.getcwd()}'; showing it as text.")
    return text


def plain_text(value):
    """User-entered text for a ReportLab Paragraph, shown exactly as typed.
    Paragraph text is parsed as markup, so '<scaffold_12>' would vanish and 'A&B'
    would turn into 'A&B;'. Escaping &, < and > prevents that; line breaks are kept."""
    return escape(str(value)).replace("\n", "<br/>")


def to_number(value):
    """A number from 1234, 1234.5 or '1,234' as float; None if not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(',', '').strip())
    except (ValueError, TypeError):
        return None


def format_number(value):
    """Format a numeric value with thousands separators; pass through otherwise."""
    try:
        value_float = float(value)
        if value_float.is_integer():
            return f'{int(value_float):,}'
        return f'{value_float:,}'
    except (ValueError, TypeError, OverflowError):
        return value


def format_2dp(value):
    """QV / k-mer completeness for display: two decimals, 'inf' kept, '' when missing."""
    if value is None or value == '':
        return ''
    number = float(value)
    if math.isinf(number):
        return 'inf'
    return f"{number:.2f}"


# ===========================================================================
# EXTERNAL DATA: GoaT + ToLID
# ===========================================================================

def get_species_genomic_data_from_goat(species):
    """Look up taxon id, Class/Order, and Expected genome_size / haploid_number /
    ploidy (each with its GoaT aggregation source) for a species.

    Falls back to the genus if the species name is not found. Any network or
    parsing failure is returned in 'error' and leaves the numeric fields as None
    so the caller can degrade to NA rather than crash.
    """
    result = {
        'taxon_id': None,
        'class_name': None,
        'order_name': None,
        'genome_size': None,
        'genome_size_source': None,
        'haploid_number': None,
        'haploid_source': None,
        'ploidy': None,
        'ploidy_source': None,
        'resolution_level': None,
        'error': None,
    }

    try:
        parts = species.split()
        candidates = [(species, 'species')]
        if len(parts) >= 2:
            candidates.append((parts[0], 'genus'))

        first_result = None
        for name, level in candidates:
            name_encoded = quote(name)
            search_url = (f'https://goat.genomehubs.org/api/v2/search'
                          f'?query=tax_name%28{name_encoded}%29&result=taxon')
            response = requests.get(search_url, timeout=30)
            response.raise_for_status()
            search_data = response.json()
            if search_data.get('results'):
                first_result = search_data['results'][0]['result']
                result['resolution_level'] = level
                break

        if first_result is None:
            result['error'] = "Species not found"
            return result

        taxon_id = first_result['taxon_id']
        result['taxon_id'] = taxon_id

        # Traverse lineage for Class and Order (kept as in the original report)
        if 'lineage' in first_result:
            for node in first_result['lineage']:
                rank = node.get('taxon_rank') or node.get('rank')
                if rank == 'class':
                    result['class_name'] = node.get('scientific_name')
                elif rank == 'order':
                    result['order_name'] = node.get('scientific_name')

        record_url = (f'https://goat.genomehubs.org/api/v2/record'
                      f'?recordId={taxon_id}&result=taxon&taxonomy=ncbi')
        record_response = requests.get(record_url, timeout=30)
        record_response.raise_for_status()
        record_data = record_response.json()

        if not record_data.get('records'):
            result['error'] = "No genomic records found"
            return result

        attributes = record_data['records'][0]['record'].get('attributes', {})

        haploid_info = attributes.get('haploid_number', {})
        if haploid_info.get('value') is not None:
            result['haploid_number'] = int(haploid_info['value'])
            result['haploid_source'] = haploid_info.get('aggregation_source')

        ploidy_info = attributes.get('ploidy', {})
        if ploidy_info.get('value') is not None:
            result['ploidy'] = ploidy_info['value']
            result['ploidy_source'] = ploidy_info.get('aggregation_source')

        size_info = attributes.get('genome_size', {})
        if size_info.get('value') is not None:
            result['genome_size'] = size_info['value']
            result['genome_size_source'] = size_info.get('aggregation_source')

        return result

    except requests.exceptions.RequestException as e:
        result['error'] = f"API request failed: {e}"
        return result
    except (KeyError, ValueError, TypeError) as e:
        result['error'] = f"Data parsing error: {e}"
        return result
    except Exception as e:
        result['error'] = f"Unexpected error: {e}"
        return result


def check_tolid_exists(tolid):
    """Returns:
      'exists'  -> registered (non-empty result)
      'absent'  -> HTTP 200 but empty result (genuinely not a known ToLID) -> caller hard-stops
      'unknown' -> any other status (e.g. 400) or a network/timeout error (could not check)
    """
    url = f"https://id.tol.sanger.ac.uk/api/v2/tol-ids/{tolid}"
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                logging.warning(f"# ToLID API returned non-JSON for '{tolid}'; could not validate.")
                return 'unknown'
            if isinstance(data, list) and len(data) > 0:
                return 'exists'
            return 'absent'
        logging.warning(f"# ToLID API returned HTTP {response.status_code} for '{tolid}'; could not validate.")
        return 'unknown'
    except requests.exceptions.RequestException as e:
        logging.warning(f"# ToLID API request failed for '{tolid}': {e}")
        return 'unknown'


# ===========================================================================
# TOOL OUTPUT PARSERS
# ===========================================================================

# ---- gfastats -------------------------------------------------------------

# Fields shown in the quality metrics table, in display order.
GFASTATS_KEYS = [
    "Total scaffold length",
    "GC content %",
    "# gaps in scaffolds",
    "Total gap length in scaffolds",
    "# scaffolds",
    "Scaffold N50",
    "Scaffold L50",
    "Scaffold L90",
    "# contigs",
    "Contig N50",
    "Contig L50",
    "Contig L90",
]
# Read but not displayed: needed to match Merqury rows to haplotypes (see match_merqury_rows).
GFASTATS_EXTRA_KEYS = ["Total contig length"]


def parse_gfastats(path):
    """Return {field: value string} for GFASTATS_KEYS + GFASTATS_EXTRA_KEYS from a
    gfastats report; a field missing from the report maps to ''. Raises OSError if
    the file cannot be read."""
    with open(path, "r") as fh:
        content = fh.read()
    stats = {}
    for key in GFASTATS_KEYS + GFASTATS_EXTRA_KEYS:
        match = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", content, re.MULTILINE)
        stats[key] = match.group(1) if match else ''
    return stats


def gfastats_number(stats, key):
    """Numeric value of a gfastats field, or None if absent / not a finite number."""
    number = to_number(stats.get(key)) if stats else None
    return number if number is not None and math.isfinite(number) else None


def compute_ebp_metric(haplotype, stats, qv_value, scaffold_is_c=None, contig_is_c=None):
    """Compute the EBP quality metric string for a haplotype.
    Format: <contig N50 log10>.<scaffold N50 log10>.Q<floor(QV)>.
    QV may be missing -> the Q field becomes QNA; an infinite QV (Merqury found no
    errors) is written Qinf.

    scaffold_is_c / contig_is_c: True when that level has been judged
    chromosome-scale, False when it has not, None when it could not be judged.
    When either is True an additional '(estimated ...)' form is appended in which
    that field carries the EBP's literal 'C' - so 6.7.Q43 becomes
    '6.7.Q43 (estimated 6.C.Q43)'. The numeric form is never replaced.
    """
    def log10_or_na(value):
        number = to_number(value)
        if number is None or not math.isfinite(number) or number <= 0:
            return "NA"
        return str(math.floor(math.log10(number)))
    contig_n50_log = log10_or_na(stats.get("Contig N50"))
    scaffold_n50_log = log10_or_na(stats.get("Scaffold N50"))
    try:
        qv = float(qv_value)
        qstr = "Qinf" if math.isinf(qv) else f"Q{math.floor(qv)}"
    except (ValueError, TypeError):
        qstr = "QNA"
    metric = (f"Obtained EBP quality metric for {haplotype}: "
              f"{contig_n50_log}.{scaffold_n50_log}.{qstr}")

    # A 'C' is only stamped when the numeric value it replaces was itself readable,
    # so the estimated form can never contradict the numeric one it sits beside.
    scaffold_c = bool(scaffold_is_c) and scaffold_n50_log != "NA"
    contig_c = bool(contig_is_c) and contig_n50_log != "NA"

    # Only worth printing when a C was actually earned; otherwise the estimated
    # form is identical to the numeric one and adds nothing.
    if scaffold_c or contig_c:
        c_contig = "C" if contig_c else contig_n50_log
        c_scaffold = "C" if scaffold_c else scaffold_n50_log
        metric += f" (estimated {c_contig}.{c_scaffold}.{qstr})"
    return metric


# ---- BUSCO / compleasm ----------------------------------------------------

def _is_compleasm(content):
    return ('## lineage:' in content) and ('# BUSCO version is:' not in content)


def _compleasm_section(content):
    """Return (lineage name, section text) of the lineage to report from a compleasm
    summary. When the file lists several lineages (e.g. eukaryota plus mammalia) the
    specific one is used; otherwise the only one."""
    sections = []
    for part in content.split('## lineage:')[1:]:
        name = part.strip().split('\n')[0].strip() or 'NA'
        sections.append((name, part))
    if not sections:
        return 'NA', content
    specific = [s for s in sections if not s[0].lower().startswith('eukaryota')]
    return (specific or sections)[0]


def extract_busco_values(file_path):
    """Return (Single, Duplicated, Fragmented, Missing) as percentage strings, or
    ('', '', '', '') if the file has no recognisable result. Raises OSError if the
    file cannot be read.

    Handles BUSCO short_summary.txt and compleasm summary.txt. compleasm results are
    mapped onto BUSCO's four categories so the table means the same for both tools:
      Duplicated = D + R   (R = retrocopies, split out of D by compleasm --retrocopy)
      Fragmented = F + I   (compleasm's two fragmented subclasses)
    """
    with open(file_path, 'r') as file:
        content = file.read()

    try:
        if _is_compleasm(content):
            _, section = _compleasm_section(content)

            def pct(letter):
                m = re.search(rf"^\s*{letter}:([\d.]+)%", section, re.MULTILINE)
                return float(m.group(1)) if m else None
            s, d, r, f, i, missing = (pct(x) for x in ('S', 'D', 'R', 'F', 'I', 'M'))
            if None in (s, d, f, missing):
                logging.warning(f"Could not parse completeness values from '{file_path}'")
                return '', '', '', ''
            dupl = d + (r or 0.0)
            frag = f + (i or 0.0)
            return tuple(f"{v:.2f}%" for v in (s, dupl, frag, missing))
        else:
            results_line = re.findall(r"C:.*n:\d+", content)[0]

            def grab(letter):
                m = re.findall(rf"{letter}:(\d+\.\d+%)", results_line)
                return m[0] if m else ''
            return grab('S'), grab('D'), grab('F'), grab('M')
    except (IndexError, AttributeError):
        logging.warning(f"Could not parse completeness values from '{file_path}'")
        return '', '', '', ''


def extract_busco_info(file_path):
    """Return a one-line metadata string (tool/version/mode/lineage), best-effort.
    Works for both BUSCO and compleasm; missing fields are reported as NA.
    Returns None only if the file cannot be read.
    """
    try:
        with open(file_path, 'r') as file:
            content = file.read()
    except Exception as e:
        logging.warning(f"Error reading '{file_path}': {str(e)}")
        return None

    if _is_compleasm(content):
        lineage_name, _ = _compleasm_section(content)
        vmatch = re.search(r"[Cc]ompleasm[ _]?v?([\d.]+)", content)
        version = vmatch.group(1) if vmatch else 'NA'
        return f"compleasm: {version} / Lineage: {lineage_name}"

    version_match = re.search(r"# BUSCO version is: ([\d.]+)", content)
    version = version_match.group(1) if version_match else 'NA'
    lineage_match = re.search(
        r"The lineage dataset is: (.*?) \(Creation date:.*?, number of (?:genomes|species): (\d+), number of BUSCOs: (\d+)\)",
        content)
    if lineage_match:
        lineage = f"{lineage_match.group(1)} (genomes:{lineage_match.group(2)}, BUSCOs:{lineage_match.group(3)})"
    else:
        lineage = 'NA'
    mode_match = re.search(r"# BUSCO was run in mode: (\w+)", content)
    mode = mode_match.group(1) if mode_match else 'NA'
    pred_match = re.search(r"# Gene predictor used: (\w+)", content)
    pred = pred_match.group(1) if pred_match else 'NA'
    return f"BUSCO: {version} ({mode}, {pred}) / Lineage: {lineage}"


# ---- Merqury / MerquryFK --------------------------------------------------
# Both tools write, in the same folder:
#   <prefix>.qv                  QV of each assembly (+ a combined 'both' row)
#   <prefix>.<asm>.qv            QV of every scaffold of one assembly
#   <prefix>.completeness.stats  k-mer completeness of each assembly (+ combined)
#   <prefix>[.<asm>].spectra-cn / <prefix>.spectra-asm .{fl,ln,st}.png
# MerquryFK files start with a header row whose first cell is 'Assembly'.

def detect_kmer_tool(qv_file, completeness_file):
    """Return 'Merqury.FK' or 'Merqury' from the header of the summary files."""
    for path in (qv_file, completeness_file):
        if not path:
            continue
        try:
            with open(path) as f:
                for line in f:
                    if line.strip():
                        if line.split("\t", 1)[0].strip().lower() == "assembly":
                            return "Merqury.FK"
                        break
        except OSError:
            continue
    return "Merqury"


def _read_merqury_table(path):
    """(header in lower case or None, data rows split on tabs)."""
    with open(path) as fh:
        rows = [ln.rstrip("\n").split("\t") for ln in fh if ln.strip()]
    if rows and rows[0][0].strip().lower() == "assembly":
        return [c.strip().lower() for c in rows[0]], rows[1:]
    return None, rows


def _is_combined_row(label):
    label = label.strip().lower()
    return label == "both" or "+" in label


def read_merqury_qv(path):
    """Parse a summary .qv file. Returns (rows, combined_qv) where rows is a list of
    {'label', 'total', 'qv'} per assembly in file order ('total' = k-mers in the
    assembly, used for size matching) and combined_qv is the QV of the 'both' row.
    Merqury:   <asm> <asm-only> <total> <QV> <error rate>   (no header)
    MerquryFK: Assembly  No Support  Total  Error %  QV     (header)"""
    header, rows = _read_merqury_table(path)
    if header:
        qv_idx = header.index("qv") if "qv" in header else len(header) - 1
        total_idx = header.index("total") if "total" in header else None
    else:
        qv_idx, total_idx = 3, 2
    out, combined = [], None
    for parts in rows:
        if len(parts) <= qv_idx:
            continue
        try:
            qv = float(parts[qv_idx])            # float() also reads 'inf' / '+inf'
        except ValueError:
            continue
        label = parts[0].strip()
        if _is_combined_row(label):
            combined = qv
            continue
        total = None
        if total_idx is not None and len(parts) > total_idx:
            try:
                total = int(parts[total_idx])
            except ValueError:
                total = None
        out.append({'label': label, 'total': total, 'qv': qv})
    return out, combined


def read_merqury_completeness(path):
    """Parse a .completeness.stats file. Returns (rows, combined_pct) where rows is a
    list of (label, percent) per assembly in file order.
    Merqury:   <asm> all <found> <total> <percent>          (no header)
    MerquryFK: Assembly  Region  Found  Total  % Covered    (header)"""
    header, rows = _read_merqury_table(path)
    if header:
        pct_idx = next((i for i, c in enumerate(header) if c in ("% covered", "completeness")),
                       len(header) - 1)
    else:
        pct_idx = 4
    out, combined = [], None
    for parts in rows:
        if len(parts) <= pct_idx:
            continue
        try:
            pct = float(parts[pct_idx])
        except ValueError:
            continue
        label = parts[0].strip()
        if _is_combined_row(label):
            combined = pct
            continue
        out.append((label, pct))
    return out, combined


def find_merqury_runs(folder):
    """Locate the summary files of the Merqury/MerquryFK run(s) in a folder.
    The summary .qv is the one whose prefix matches '<prefix>.completeness.stats';
    the per-scaffold '<prefix>.<asm>.qv' files sit beside it and are never used.
    Returns [{'prefix', 'qv', 'completeness'}] (normally exactly one run)."""
    runs = []
    for comp in sorted(glob.glob(os.path.join(folder, "*.completeness.stats"))):
        prefix = os.path.basename(comp)[:-len(".completeness.stats")]
        qv = os.path.join(folder, prefix + ".qv")
        runs.append({'prefix': prefix, 'completeness': comp, 'qv': qv if os.path.isfile(qv) else None})
    if not runs:
        # No completeness file: a summary .qv is one that is not '<other>.<asm>.qv'.
        qvs = sorted(glob.glob(os.path.join(folder, "*.qv")))
        stems = [os.path.basename(q)[:-3] for q in qvs]
        for q, stem in zip(qvs, stems):
            if not any(stem.startswith(other + ".") for other in stems if other != stem):
                runs.append({'prefix': stem, 'qv': q, 'completeness': None})
    return runs


def match_merqury_rows(rows, stats_by_hap):
    """Map Merqury assembly rows to haplotypes by size.

    Merqury counts every k-mer that fits inside a contig, so for each assembly
        total k-mers = total contig length - (k - 1) x number of contigs
    exactly (gaps split contigs, so they are accounted for). Matching on that
    identifies each assembly regardless of row order or names, and also catches
    Merqury having been run on a different version of an assembly.

    Returns ({label: hap}, k) when every haplotype matches exactly one row and all
    matches share one k; otherwise (None, None)."""
    if not rows or not stats_by_hap:
        return None, None
    mapping, ks = {}, set()
    for hap, stats in stats_by_hap.items():
        length = gfastats_number(stats, "Total contig length")
        n_contigs = gfastats_number(stats, "# contigs")
        if not length or not n_contigs:
            return None, None
        length, n_contigs = int(length), int(n_contigs)
        hits = []
        for row in rows:
            if row['total'] is None:
                continue
            diff = length - row['total']
            if diff >= 0 and diff % n_contigs == 0 and 10 <= diff // n_contigs <= 100:
                hits.append((row['label'], diff // n_contigs + 1))
        if len(hits) != 1 or hits[0][0] in mapping:
            return None, None
        mapping[hits[0][0]] = hap
        ks.add(hits[0][1])
    if len(ks) != 1:
        return None, None
    return mapping, ks.pop()


def find_merqury_plots(merqury_dir, prefix=None):
    """Find Merqury/MerquryFK spectra plots, preferring filled (.fl) then line (.ln)
    then stacked (.st). Returns {'spectra_asm': path|None, 'combined': path|None,
    'per_asm': {asm label: path}}.

    With the run prefix known (from the summary files) plots are classified exactly:
      <prefix>.spectra-cn.*         combined copy-number spectra
      <prefix>.<asm>.spectra-cn.*   copy-number spectra of one assembly
    so prefixes that themselves contain 'hap1'/'asm1', or assembly names without
    such tags (e.g. 'x.bp.p_ctg'), are handled. Without a prefix, a name-pattern
    rule is used as before."""
    plots = {'spectra_asm': None, 'combined': None, 'per_asm': {}}
    if not merqury_dir or not os.path.isdir(merqury_dir):
        return plots

    for style in ("fl", "ln", "st"):
        suffix = f".spectra-cn.{style}.png"
        cn_files = sorted(glob.glob(os.path.join(merqury_dir, f"*{suffix}")))
        if not cn_files:
            continue
        for path in cn_files:
            stem = os.path.basename(path)[:-len(suffix)]
            if prefix is not None:
                if stem == prefix:
                    plots['combined'] = path
                elif stem.startswith(prefix + "."):
                    plots['per_asm'][stem[len(prefix) + 1:]] = path
            else:
                tag = re.search(r'[\._]((?:asm|hap)\d+)[\._]', os.path.basename(path))
                if tag:
                    plots['per_asm'][tag.group(1)] = path
                else:
                    plots['combined'] = path
        break

    for style in ("fl", "ln", "st"):
        asm_files = sorted(glob.glob(os.path.join(merqury_dir, f"*.spectra-asm.{style}.png")))
        if prefix is not None:
            exact = os.path.join(merqury_dir, f"{prefix}.spectra-asm.{style}.png")
            asm_files = [exact] if exact in asm_files else asm_files
        if asm_files:
            plots['spectra_asm'] = asm_files[0]
            break
    return plots


# ---- FCS-GX ---------------------------------------------------------------

def parse_fcs_gx(filepath):
    """Count the entries flagged by FCS-GX: the rows of the action report after its
    two header lines (both start with '#'). Raises OSError if unreadable."""
    with open(filepath, 'r') as f:
        return sum(1 for line in f if line.strip() and not line.startswith('#'))


# ---- Images ---------------------------------------------------------------

def make_image(path, max_w, max_h, compress=True, label="Image"):
    """A ReportLab Image of a PNG fitted inside max_w x max_h points with its
    proportions kept, or None (logged) if the file is missing or unreadable.

    compress=True: flatten transparency onto white, downsample to IMAGE_DPI at the
    printed size, and reduce to 256 colours without dithering. EAR plots are
    flat-colour graphics, so this is visually lossless while making the PDF smaller.
    """
    if not os.path.isfile(path):
        logging.warning(f"# {label} '{os.path.abspath(path)}' not found; using NA.")
        return None
    try:
        from PIL import Image as PILImage   # installed with ReportLab
        with PILImage.open(path) as im:
            im.load()
            w_px, h_px = im.size
            scale = min(max_w / w_px, max_h / h_px)
            draw_w, draw_h = w_px * scale, h_px * scale
            if not compress:
                return Image(path, width=draw_w, height=draw_h)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                flat = PILImage.new("RGB", rgba.size, "white")
                flat.paste(rgba, mask=rgba.getchannel("A"))
            else:
                flat = im.convert("RGB")
        target_w = max(1, round(draw_w / 72 * IMAGE_DPI))
        if flat.width > target_w:
            target_h = max(1, round(flat.height * target_w / flat.width))
            flat = flat.resize((target_w, target_h), PILImage.Resampling.LANCZOS)
        flat = flat.quantize(colors=256, method=PILImage.Quantize.MEDIANCUT,
                             dither=PILImage.Dither.NONE)
        buffer = io.BytesIO()
        flat.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return Image(buffer, width=draw_w, height=draw_h)
    except Exception as e:
        logging.warning(f"# Could not read {label} '{os.path.abspath(path)}': {e}; using NA.")
        return None


# ===========================================================================
# CONFIG: merge YAML (optional) with CLI flags (override)
# ===========================================================================

PER_HAP_FLAGS = ['gfastats_nstar', 'busco_short', 'fcs_gx', 'blobplot', 'pretext_temp_link',
                 'merqury_spectra_cn']
# Merqury files that can be given one by one instead of (or on top of) merqury_folder;
# one per run, not per haplotype (merqury_spectra_cn is the per-haplotype one).
MERQURY_FILE_KEYS = ['merqury_qv', 'merqury_completeness', 'merqury_spectra_asm',
                     'merqury_spectra_cn_combined']


def build_config(args):
    """Merge an optional YAML file with command-line flags.
    A supplied flag overrides the YAML value; so YAML is "optional".
    Per-haplotype flags are comma-separated and mapped by position (index 0 ->
    hap1). Each flag replaces only its own field for that haplotype and leaves
    sibling fields and other haplotypes intact.
    """
    config = {}
    if args.yaml:
        try:
            with open(args.yaml, "r") as fh:
                config = yaml.safe_load(fh) or {}
        except OSError as e:
            raise EARError(f"Could not read the YAML file '{os.path.abspath(args.yaml)}': {e.strerror or e}.")
        except yaml.YAMLError as e:
            hint = ""
            if "sequence entries are not allowed here" in str(e):
                hint = (" A value written as a bare - is read by YAML as a list item: "
                        "write it in quotes (\"-\"), as NA, or leave it empty.")
            raise EARError(f"The YAML file '{os.path.abspath(args.yaml)}' is not valid YAML: {e}{hint}")
        if not isinstance(config, dict):
            raise EARError(f"The YAML file '{os.path.abspath(args.yaml)}' does not contain the expected fields.")
        logging.info(f"# YAML file: '{os.path.abspath(args.yaml)}'")

    def override_scalar(key, value):
        if value is not None:
            config[key] = value

    override_scalar('tolid', args.tolid)
    override_scalar('species', args.species)
    override_scalar('sex', args.sex)
    override_scalar('submitter', args.submitter)
    override_scalar('affiliation', args.affiliation)
    override_scalar('tags', args.tags)
    override_scalar('PIPELINE', args.PIPELINE)

    # DATA: 'type:depth,type:depth' ; depth optional
    if args.DATA is not None:
        data_list = []
        for item in args.DATA.split(','):
            item = item.strip()
            if not item:
                continue
            if ':' in item:
                dtype, depth = item.split(':', 1)
                data_list.append({dtype.strip(): depth.strip()})
            else:
                data_list.append({item: ''})
        config['DATA'] = data_list

    # PROFILING (PNG-only)
    profiling = config.get('PROFILING') or {}
    if args.genomescope is not None:
        profiling['genomescope'] = args.genomescope
    if args.smudgeplot is not None:
        profiling['smudgeplot'] = args.smudgeplot
    if profiling:
        config['PROFILING'] = profiling

    # NOTES
    notes = config.get('NOTES') or {}
    if args.obs_haploid_num is not None:
        notes['obs_haploid_num'] = args.obs_haploid_num
    if args.obs_ploidy is not None:
        notes['obs_ploidy'] = args.obs_ploidy
    if args.obs_sex is not None:
        notes['obs_sex'] = args.obs_sex
    if args.curation_notes is not None:
        notes['curation_notes'] = args.curation_notes
    if args.alignment_png is not None:
        notes['alignment_png'] = args.alignment_png
    if notes:
        config['NOTES'] = notes

    # ASSEMBLIES: overlay per-haplotype flags by position
    assemblies = config.get('ASSEMBLIES') or {}

    lengths = {flag: len(getattr(args, flag).split(',')) for flag in PER_HAP_FLAGS
               if getattr(args, flag) is not None}
    if len(set(lengths.values())) > 1:
        logging.warning("# Per-haplotype command-line lists have different lengths ("
                        + ", ".join(f"--{k}: {n}" for k, n in lengths.items())
                        + "); values are matched to haplotypes by position and missing positions are NA.")

    def overlay_hap_field(field, csv_value):
        if csv_value is None:
            return
        values = [v.strip() for v in csv_value.split(',')]
        for i, value in enumerate(values):
            if i >= len(VALID_HAPS):
                logging.warning("More than 4 haplotypes supplied on the CLI; extra values ignored.")
                break
            hap = VALID_HAPS[i]
            if not isinstance(assemblies.get(hap), dict):
                assemblies[hap] = {}
            assemblies[hap][field] = value

    for flag in PER_HAP_FLAGS:
        overlay_hap_field(flag, getattr(args, flag))

    # merqury_folder and the Merqury files are one per run; a CLI value lands on hap1
    for key in ['merqury_folder'] + MERQURY_FILE_KEYS:
        if getattr(args, key) is not None:
            if not isinstance(assemblies.get('hap1'), dict):
                assemblies['hap1'] = {}
            assemblies['hap1'][key] = getattr(args, key)

    if assemblies:
        config['ASSEMBLIES'] = assemblies

    return config


# ===========================================================================
# MAIN
# ===========================================================================

def _config_section(config, key, kind):
    """A top-level YAML block as a dict / list; an empty block (key with nothing
    under it) counts as empty instead of crashing."""
    value = config.get(key)
    if value is None or value == '':
        return kind()
    if kind is list and isinstance(value, dict):        # DATA written as a mapping
        return [{k: v} for k, v in value.items()]
    if not isinstance(value, kind):
        what = "a list of entries" if kind is list else "a block of fields"
        raise EARError(f"'{key}' in the YAML must be {what}; check its indentation.")
    return value


def make_report(config, outdir=".", compress_images=True, log_capture=None):
    """Build the EAR PDF from a merged config. Returns {'pdf', 'report_id', 'payload'
    (the displayed values), 'data' (everything stored in the PDF), 'details'}.
    Raises EARError when the inputs do not allow a report.
    log_capture: a _LogCapture already collecting this run's log (main() starts it before
    reading the YAML); when None, one is attached for the duration of this call."""
    root = logging.getLogger()
    own_capture = log_capture is None
    if own_capture:
        log_capture = _LogCapture()
        root.addHandler(log_capture)
        old_level = root.level
        if old_level > logging.INFO:              # NOTSET (0) already lets INFO through
            root.setLevel(logging.INFO)
    try:
        return _make_report(config, outdir, compress_images, log_capture)
    finally:
        if own_capture:
            root.removeHandler(log_capture)
            root.setLevel(old_level)


def _make_report(config, outdir, compress_images, log_capture):
    summary = []   # run summary written to the log at the end

    data_list = _config_section(config, 'DATA', list)
    profiling = _config_section(config, 'PROFILING', dict)
    asm_data = _config_section(config, 'ASSEMBLIES', dict)
    notes_cfg = _config_section(config, 'NOTES', dict)

    # ---- helper: fetch a scalar sample field, warn + NA when missing --------
    def sample_value(key, mandatory=False):
        value = config.get(key)
        if is_empty(value):
            if mandatory:
                raise EARError(f"Mandatory field '{key}' is missing.")
            note_missing(f"Field '{key}'", value)
            return "NA"
        return value

    # =======================================================================
    # VALIDATION (mandatory fields, checked after the YAML/CLI merge)
    # =======================================================================
    species = sample_value("species", mandatory=True)
    if not isinstance(species, str):
        raise EARError("'species' must be text (e.g. \"Homo sapiens\").")
    species = species.strip()

    # keep only recognised haplotype keys, in canonical order
    haplotypes = [h for h in VALID_HAPS if h in asm_data and isinstance(asm_data[h], dict)]
    if not haplotypes:
        raise EARError("No haplotypes (hap1..hap4) found in ASSEMBLIES.")

    if not any(not is_empty(asm_data[h].get('gfastats_nstar')) for h in haplotypes):
        raise EARError("A gfastats report is mandatory (at least one haplotype).")
    if not any(not is_empty(asm_data[h].get('busco_short')) for h in haplotypes):
        raise EARError("A BUSCO/compleasm summary is mandatory (at least one haplotype).")

    obs_haploid_raw = notes_cfg.get("obs_haploid_num")
    if is_empty(obs_haploid_raw):
        raise EARError("'obs_haploid_num' is mandatory.")
    try:
        obs_haploid_n = parse_haploid_number(obs_haploid_raw)
    except ValueError as reason:
        raise EARError(f"'obs_haploid_num' = {obs_haploid_raw!r} cannot be used: {reason}. "
                       f"Give the haploid chromosome number n as a whole number (e.g. 23).")
    if str(obs_haploid_raw).strip() != str(obs_haploid_n):
        logging.info(f"# obs_haploid_num {obs_haploid_raw!r} read as {obs_haploid_n}.")

    # =======================================================================
    # SAMPLE INFORMATION
    # =======================================================================
    tol_id = sample_value("tolid")
    sex = sample_value("sex")
    submitter = sample_value("submitter")
    affiliation = sample_value("affiliation")
    tags = sample_value("tags")

    # ToLID validation: hard-stop when the registry confirms the ToLID is unknown
    if tol_id != "NA":
        tolid_status = check_tolid_exists(tol_id)
        if tolid_status == 'absent':
            raise EARError(f"ToLID '{tol_id}' is not registered in the ToLID registry. "
                           f"Remove the ToLID from the input to produce the report.")
        elif tolid_status == 'unknown':
            logging.warning(f"# Could not validate ToLID '{tol_id}' (ToLID API unreachable "
                            f"or returned an unexpected status). Proceeding with it as given.")

    # GoaT lookup (Expected traits + taxonomy)
    goat = get_species_genomic_data_from_goat(species)
    if goat['error']:
        logging.warning(f"# GoaT lookup issue for '{species}': {goat['error']}. Affected fields show NA.")
    genus_level = goat['resolution_level'] == 'genus'
    if genus_level:
        logging.warning(f"# GoaT has no record for '{species}'; TxID and the Expected values come from "
                        f"the genus '{species.split()[0]}' and are marked '(genus-level)' in the report.")
    genus_tag = " (genus-level)" if genus_level else ""

    taxon_number = f"{goat['taxon_id']}{genus_tag}" if goat['taxon_id'] is not None else 'NA'
    class_name = goat['class_name'] or 'NA'
    order_name = goat['order_name'] or 'NA'
    haploid_number = goat['haploid_number']
    haploid_source = goat['haploid_source'] or 'NA'
    ploidy = goat['ploidy']
    ploidy_source = goat['ploidy_source'] or 'NA'
    genome_size = to_number(goat['genome_size'])
    genome_size_source = goat['genome_size_source'] or 'NA'

    sp_data = [
        ["TxID", "ToLID", "Species", "Class", "Order"],
        [taxon_number, tol_id, species, class_name, order_name],
    ]
    transposed_sp_data = list(map(list, zip(*sp_data)))
    summary.append(f"GoaT: taxon {goat['taxon_id']} resolved at {goat['resolution_level'] or 'n/a'} level; "
                   f"expected size {format_number(genome_size) if genome_size else 'NA'} ({genome_size_source}), "
                   f"haploid number {haploid_number if haploid_number is not None else 'NA'} ({haploid_source}), "
                   f"ploidy {ploidy if ploidy is not None else 'NA'} ({ploidy_source})"
                   + (f"; error: {goat['error']}" if goat['error'] else ""))

    # =======================================================================
    # SEQUENCING DATA (listed as "<type>: <coverage>" in the Data profile)
    # =======================================================================
    data_lines = []
    for item in data_list:
        if not isinstance(item, dict):
            continue
        for technology, coverage in item.items():
            if is_empty(technology):
                continue
            if is_empty(coverage):
                coverage_text = 'NA'
            else:
                coverage_text = str(coverage).strip()
                if to_number(coverage_text) is not None:     # plain number: add the 'x' of coverage
                    coverage_text = f"{format_number(to_number(coverage_text))}x"
            data_lines.append(f"{str(technology).strip()}: {coverage_text}")
    if not data_lines:
        logging.warning("# DATA (sequencing data and coverage) is missing; the data profile shows NA.")
        data_lines = ["NA"]

    # =======================================================================
    # METHODS (single free-text block)
    # =======================================================================
    pipeline_text = resolve_text_field(config.get('PIPELINE'), 'PIPELINE')
    if pipeline_text is None:
        note_missing("PIPELINE (methods)", config.get('PIPELINE'))
        pipeline_text = "NA"

    # =======================================================================
    # GENOME PROFILING (PNG-only, optional)
    # =======================================================================
    genomescope_png = profiling.get('genomescope')
    smudgeplot_png = profiling.get('smudgeplot')
    if is_empty(genomescope_png):
        note_missing("GenomeScope PNG", genomescope_png)
        genomescope_png = None
    if is_empty(smudgeplot_png):
        note_missing("Smudgeplot PNG", smudgeplot_png)
        smudgeplot_png = None

    # =======================================================================
    # ASSEMBLY DATA: gfastats (a given file must be readable)
    # =======================================================================
    gfastats_data = {}
    for hap in haplotypes:
        path = asm_data[hap].get('gfastats_nstar')
        if is_empty(path):
            continue
        path = str(path).strip()
        try:
            stats = parse_gfastats(path)
        except OSError as e:
            raise EARError(f"The gfastats report for {hap} could not be read: "
                           f"'{os.path.abspath(path)}' ({e.strerror or e}).")
        if not stats["Total scaffold length"]:
            raise EARError(f"'{os.path.abspath(path)}' (gfastats for {hap}) does not look like a "
                           f"gfastats report: no 'Total scaffold length' line.")
        missing_keys = [k for k in GFASTATS_KEYS if not stats[k]]
        if missing_keys:
            logging.warning(f"# gfastats for {hap} is missing fields: {', '.join(missing_keys)}. "
                            f"Those cells show as blank. (If L90/N90 fields are missing, "
                            f"run gfastats with --nstar-report.)")
        gfastats_data[hap] = stats
        summary.append(f"{hap} gfastats: '{os.path.abspath(path)}'")

    # display names for the metrics table
    display_names = GFASTATS_KEYS.copy()
    display_names[display_names.index("Total scaffold length")] = "Total bp"
    display_names[display_names.index("GC content %")] = "GC %"
    display_names[display_names.index("Total gap length in scaffolds")] = "Total gap bp"
    display_names[display_names.index("# scaffolds")] = "Scaffolds"
    display_names[display_names.index("# contigs")] = "Contigs"

    gaps_index = GFASTATS_KEYS.index("# gaps in scaffolds")
    exclusion_list = ["# gaps in scaffolds"]

    # gaps per Gbp
    gaps_per_gbp_data = {}
    for hap, stats in gfastats_data.items():
        gaps = gfastats_number(stats, "# gaps in scaffolds")
        total_length = gfastats_number(stats, "Total scaffold length")
        gaps_per_gbp_data[hap] = (round(gaps / total_length * 1_000_000_000, 2)
                                  if gaps is not None and total_length else '')

    # =======================================================================
    # ASSEMBLY DATA: Merqury / MerquryFK (one run covering all haplotypes)
    # Read from merqury_folder, and/or from files given one by one (any file names):
    # a file given directly replaces the one found in the folder. The run-level keys
    # can sit in any haplotype block (the CLI puts them in hap1); merqury_spectra_cn
    # is per haplotype. The YAML accepts these keys too, but the template only shows
    # merqury_folder to keep it simple.
    # =======================================================================
    def run_level(key):
        values = [str(asm_data[h].get(key)).strip() for h in haplotypes if not is_empty(asm_data[h].get(key))]
        if len({os.path.abspath(v) for v in values}) > 1:
            logging.warning(f"# Multiple different {key} paths given; using the first non-empty one.")
        return values[0] if values else None

    merqury_folder = run_level('merqury_folder')
    direct = {key: run_level(key) for key in MERQURY_FILE_KEYS}
    direct_cn = {h: str(asm_data[h].get('merqury_spectra_cn')).strip() for h in haplotypes
                 if not is_empty(asm_data[h].get('merqury_spectra_cn'))}
    folder_ok = bool(merqury_folder) and os.path.isdir(merqury_folder)
    if merqury_folder and not folder_ok:
        logging.warning(f"# merqury_folder '{os.path.abspath(merqury_folder)}' is not a directory; nothing is read from it.")

    def given_directly(what, path, replaces):
        note = " (replaces the one in merqury_folder)" if replaces else ""
        logging.info(f"# {what} given directly: '{os.path.abspath(path)}'{note}")

    def read_merqury_file(reader, path, what):
        """Rows of a summary file given or found, [] (logged) if unreadable or empty."""
        if not path:
            return []
        if not os.path.isfile(path):
            logging.warning(f"# Merqury {what} '{os.path.abspath(path)}' not found; using NA.")
            return []
        try:
            rows = reader(path)[0]
        except (OSError, UnicodeDecodeError) as e:
            logging.warning(f"# Could not read the Merqury {what} '{os.path.abspath(path)}' ({e}); using NA.")
            return []
        if not rows:
            logging.warning(f"# No values found in the Merqury {what} '{os.path.abspath(path)}'; using NA.")
        return rows

    stats_for_matching = {h: gfastats_data[h] for h in haplotypes if h in gfastats_data}
    run = None              # the summary files found in the folder
    if folder_ok:
        runs = find_merqury_runs(merqury_folder)
        for candidate in runs:
            rows = read_merqury_qv(candidate['qv'])[0] if candidate['qv'] else []
            if match_merqury_rows(rows, stats_for_matching)[0]:
                run = candidate
                break
        if run is None and runs:
            run = runs[0]
            if len(runs) > 1:
                logging.warning(f"# Several Merqury runs found in '{os.path.abspath(merqury_folder)}' and none "
                                f"matches the assemblies by size; using '{run['prefix']}'.")

    qv_path = run['qv'] if run else None
    comp_path = run['completeness'] if run else None
    if direct['merqury_qv']:
        given_directly("Merqury QV file", direct['merqury_qv'], qv_path)
        qv_path = direct['merqury_qv']
    if direct['merqury_completeness']:
        given_directly("Merqury completeness file", direct['merqury_completeness'], comp_path)
        comp_path = direct['merqury_completeness']

    qv_data = {}            # hap -> float QV
    completeness_data = {}  # hap -> float %
    asm_to_hap = {}
    kmer_label = detect_kmer_tool(qv_path, comp_path)
    if qv_path or comp_path:
        rows = read_merqury_file(read_merqury_qv, qv_path, "QV file")
        comp_rows = read_merqury_file(read_merqury_completeness, comp_path, "completeness file")
        mapping, k = match_merqury_rows(rows, stats_for_matching)
        labels = [r['label'] for r in rows] or [label for label, _ in comp_rows]
        if mapping:
            asm_to_hap = mapping
            logging.info(f"# {kmer_label} assemblies matched to haplotypes by size (k={k}): "
                         + ", ".join(f"{a} -> {h}" for a, h in mapping.items()))
        elif len(labels) > len(haplotypes):
            # e.g. the per-scaffold '<prefix>.<asm>.qv' given instead of the summary '<prefix>.qv'
            logging.warning(f"# The {kmer_label} results list {len(labels)} entries for {len(haplotypes)} "
                            f"haplotype(s) and none match the assemblies by size, so it is not clear which is "
                            f"which; QV and k-mer completeness show NA. Is it the summary '<prefix>.qv' "
                            f"(not the per-scaffold '<prefix>.<asm>.qv')?")
        elif labels:
            asm_to_hap = {label: haplotypes[i] for i, label in enumerate(labels)}
            reason = ("different assembly versions, or gfastats 'Total contig length' missing?" if rows
                      else "no QV file to compare sizes with")
            logging.warning(f"# Could not match the {kmer_label} assemblies to the haplotypes by size "
                            f"({reason}); using file order: "
                            + ", ".join(f"{a} -> {h}" for a, h in asm_to_hap.items()))
        for row in rows:
            if row['label'] in asm_to_hap:
                qv_data[asm_to_hap[row['label']]] = row['qv']
        for label, pct in comp_rows:
            if label in asm_to_hap:
                completeness_data[asm_to_hap[label]] = pct
        if comp_rows and asm_to_hap and not completeness_data:
            logging.warning(f"# The {kmer_label} completeness file lists other assemblies ("
                            + ", ".join(label for label, _ in comp_rows)
                            + ") than the QV file; k-mer completeness shows NA.")
        if not qv_path:
            logging.warning(f"# No {kmer_label} summary .qv file"
                            + (f" for run '{run['prefix']}'" if run else "") + "; QV shows NA.")
        if not comp_path:
            logging.warning(f"# No {kmer_label} .completeness.stats file"
                            + (f" for run '{run['prefix']}'" if run else "") + "; k-mer completeness shows NA.")
        matched = (f"matched by size (k={k})" if mapping else "matched by file order" if asm_to_hap
                   else "not matched")
        shown = lambda path: f"'{os.path.abspath(path)}'" if path else "NA"
        summary.append(f"{kmer_label}: {shown(qv_path)} | {shown(comp_path)} | {matched}"
                       + (": " + ", ".join(f"{a}->{h}" for a, h in asm_to_hap.items()) if asm_to_hap else ""))
    elif not merqury_folder:
        if any(is_na(asm_data[h].get('merqury_folder')) for h in haplotypes):
            logging.info("# merqury_folder given as NA; QV and k-mer completeness show NA.")
        else:
            logging.warning("# No merqury_folder provided; QV and k-mer completeness show NA.")
    else:
        found_in = f" found in '{os.path.abspath(merqury_folder)}'" if folder_ok else ""
        logging.warning(f"# No Merqury summary files (.qv / .completeness.stats){found_in}; "
                        f"QV and k-mer completeness show NA.")

    # =======================================================================
    # ASSEMBLY DATA: BUSCO / compleasm (a given file must be readable)
    # =======================================================================
    busco_data = {metric: {} for metric in ['BUSCO sing.', 'BUSCO dupl.', 'BUSCO frag.', 'BUSCO miss.']}
    for hap in haplotypes:
        path = asm_data[hap].get('busco_short')
        if is_empty(path):
            continue
        path = str(path).strip()
        try:
            values = extract_busco_values(path)
        except OSError as e:
            raise EARError(f"The BUSCO/compleasm summary for {hap} could not be read: "
                           f"'{os.path.abspath(path)}' ({e.strerror or e}).")
        if not any(values):
            raise EARError(f"'{os.path.abspath(path)}' (busco_short for {hap}) does not look like a "
                           f"BUSCO short summary or a compleasm summary.")
        busco_data['BUSCO sing.'][hap], busco_data['BUSCO dupl.'][hap], \
            busco_data['BUSCO frag.'][hap], busco_data['BUSCO miss.'][hap] = values
        summary.append(f"{hap} gene completeness: '{os.path.abspath(path)}'")

    # =======================================================================
    # ASSEMBLY DATA: FCS-GX (per haplotype, optional)
    # =======================================================================
    fcs_gx_counts = {}
    for hap in haplotypes:
        path = asm_data[hap].get('fcs_gx')
        fcs_gx_counts[hap] = None
        if is_empty(path):
            note_missing(f"FCS-GX report for {hap}", path)
            continue
        path = str(path).strip()
        try:
            fcs_gx_counts[hap] = parse_fcs_gx(path)
        except OSError as e:
            logging.warning(f"# Could not read FCS-GX report for {hap} '{os.path.abspath(path)}': "
                            f"{e.strerror or e}; using NA.")

    # =======================================================================
    # Build the QUALITY METRICS table
    # =======================================================================
    def cell(hap, key):
        stats = gfastats_data.get(hap)
        if not stats:
            return ''
        return format_number(stats[key])

    asm_table_data = [["Metrics"] + haplotypes]
    for key, metric in zip(GFASTATS_KEYS, display_names):
        if key in exclusion_list:
            continue
        asm_table_data.append([metric] + [cell(hap, key) for hap in haplotypes])

    # Gaps/Gbp row goes right after the gaps position
    asm_table_data.insert(gaps_index + 1,
                          ['Gaps/Gbp'] + [format_number(gaps_per_gbp_data.get(hap, '')) for hap in haplotypes])

    asm_table_data.append(['QV'] + [format_2dp(qv_data.get(hap)) for hap in haplotypes])
    asm_table_data.append(['Kmer compl.'] + [format_2dp(completeness_data.get(hap)) for hap in haplotypes])
    for metric in ['BUSCO sing.', 'BUSCO dupl.', 'BUSCO frag.', 'BUSCO miss.']:
        asm_table_data.append([metric] + [busco_data[metric].get(hap, '') for hap in haplotypes])
    # A missing value shows NA, as everywhere else in the report (an empty cell is ambiguous).
    asm_table_data = [row[:1] + ['NA' if is_empty(value) else value for value in row[1:]]
                      for row in asm_table_data]


    # =======================================================================
    # CURATION NOTES
    # =======================================================================
    obs_sex = notes_cfg.get("obs_sex")
    obs_sex = "NA" if is_empty(obs_sex) else obs_sex
    obs_ploidy = notes_cfg.get("obs_ploidy")
    obs_ploidy = "NA" if is_empty(obs_ploidy) else obs_ploidy
    curation_notes = resolve_text_field(notes_cfg.get("curation_notes"), 'curation_notes')
    if curation_notes is None:
        note_missing("Curation notes", notes_cfg.get("curation_notes"))
        curation_notes = "NA"

    # Observed genome size = max Total bp across haplotypes
    total_bps = [gfastats_number(stats, "Total scaffold length") for stats in gfastats_data.values()]
    total_bps = [int(v) for v in total_bps if v]
    max_total_bp = max(total_bps) if total_bps else None

    # =======================================================================
    # EBP CHROMOSOME SCALE (drives the 'C' notation and the contig N50 bar)
    # =======================================================================
    # Mean chromosome = observed assembly size / observed haploid number. Both are
    # the curated values already displayed in the Genome Traits table, so a reader
    # can recompute this from the report; they also beat GoaT's expected genome
    # size and haploid number, which may be genus-level estimates. max_total_bp is
    # used (rather than each haplotype's own length) to keep one species-level
    # chromosome size across haplotypes, matching the "Observed Haploid size" cell.
    observed_chr_size = max_total_bp / obs_haploid_n if max_total_bp else None
    if not observed_chr_size:
        logging.warning("# Could not derive a mean chromosome size from the observed "
                        "assembly size and haploid number; no 'C' is estimated and the "
                        "contig N50 check is skipped for this report.")

    # Per-haplotype chromosome-scale judgement.
    #   scaffold_c : EBP describes chromosome-scale as ">90% assigned to chromosomal
    #                scaffolds (and thus scaffold N50 ~= chromosomal N50)". L90 <=
    #                haploid number is a PROXY for that: it is a necessary condition,
    #                not a sufficient one, since L90 is a pure size statistic and says
    #                nothing about whether those scaffolds are validated chromosomes.
    #   contig_c   : contig N50 reaches chromosome scale EBP's 'C' at contig level.
    #                Like EBP's notation this is an N50 statement: it does NOT mean
    #                the chromosomes are gapless.
    #   contig_bar : the '6' of 6.C.Q40, capped at chromosome scale so the bar is
    #                never above what one chromosome can hold (EBP's C.C case).
    #                None when no chromosome size could be derived, in that case the
    #                check is skipped rather than run against a bar we cannot justify.
    # None means "could not judge" and is kept distinct from False throughout.
    ebp_scale = {}
    for hap in haplotypes:
        stats = gfastats_data.get(hap)
        scaffold_l90 = gfastats_number(stats, "Scaffold L90")
        contig_n50 = gfastats_number(stats, "Contig N50")

        scaffold_c = None
        if scaffold_l90 is not None:
            scaffold_c = scaffold_l90 <= obs_haploid_n

        contig_bar = None
        contig_c = None
        if observed_chr_size:
            contig_bar = min(float(EBP_CONTIG_N50_BAR), CHR_SCALE_FRAC * observed_chr_size)
            if contig_n50 is not None:
                contig_c = contig_n50 >= CHR_SCALE_FRAC * observed_chr_size

        ebp_scale[hap] = {
            'scaffold_c': scaffold_c,
            'contig_c': contig_c,
            'contig_bar': contig_bar,
            'contig_n50': contig_n50,
            'scaffold_l90': scaffold_l90,
        }
    if observed_chr_size:
        bar = min(float(EBP_CONTIG_N50_BAR), CHR_SCALE_FRAC * observed_chr_size)
        summary.append(f"Chromosome scale: n={obs_haploid_n}, mean chromosome {format_number(round(observed_chr_size))} bp, "
                       f"contig N50 bar {format_number(round(bar))} bp"
                       + (" (capped at chromosome scale)" if bar < EBP_CONTIG_N50_BAR else ""))

    # Expected column values (from GoaT), NA-safe
    genus_note = ", genus-level" if genus_level else ""
    expected_size = f"{format_number(round(genome_size))}{genus_tag}" if genome_size is not None else 'NA'
    expected_haploid = (f"{haploid_number} (source: {haploid_source}{genus_note})"
                        if haploid_number is not None else 'NA')
    expected_ploidy = f"{ploidy} (source: {ploidy_source}{genus_note})" if ploidy is not None else 'NA'
    observed_size = format_number(max_total_bp) if max_total_bp else 'NA'

    genome_traits_table_data = [
        ["Genome Traits", "Expected", "Observed"],
        ["Haploid size (bp)", expected_size, observed_size],
        ["Haploid Number", expected_haploid, str(obs_haploid_n)],
        ["Ploidy", expected_ploidy, obs_ploidy],
        ["Sample Sex", sex, obs_sex],
    ]

    # =======================================================================
    # EBP METRICS AND FLAGS (as text; rendered on page 1 and part of the hash)
    # =======================================================================
    ebp_lines = []
    ebp_codes = {}
    for hap in haplotypes:
        if hap not in gfastats_data:
            continue
        scale = ebp_scale.get(hap, {})
        line = compute_ebp_metric(hap, gfastats_data[hap], qv_data.get(hap, ''),
                                  scaffold_is_c=scale.get('scaffold_c'),
                                  contig_is_c=scale.get('contig_c'))
        ebp_lines.append(line)
        ebp_codes[hap] = line.split(": ", 1)[1]

    flags = []
    # Expected vs observed (GoaT); compared as numbers, never parsed back from display text
    if genome_size and max_total_bp and abs(genome_size - max_total_bp) / genome_size > 0.20:
        flags.append("Observed Haploid size (bp) has >20% difference with Expected")
    if haploid_number is not None and int(haploid_number) != obs_haploid_n:
        flags.append("Observed Haploid Number is different from Expected")
    if haploid_number is None:
        logging.warning(f"# GoaT has no haploid number for '{species}', so the observed "
                        f"value ({obs_haploid_n}) could not be cross-checked; it still "
                        f"drives the chromosome-scale checks.")
    if ploidy is not None and obs_ploidy != "NA":
        try:
            if int(float(ploidy)) != int(str(obs_ploidy).strip()):
                flags.append("Observed Ploidy is different from Expected")
        except (ValueError, TypeError):
            logging.warning(f"# Could not compare observed ploidy {obs_ploidy!r} with expected {ploidy!r}.")
    if sex != "NA" and obs_sex != "NA" and str(sex).strip().lower() != str(obs_sex).strip().lower():
        flags.append("Observed sex is different from Sample sex")

    # Per-haplotype QV, k-mer completeness, BUSCO
    for hap in haplotypes:
        qv = qv_data.get(hap)
        if qv is not None and qv < 40:
            flags.append(f"QV value is less than 40 for {hap}")
        completeness = completeness_data.get(hap)
        if completeness is not None and completeness < 90:
            flags.append(f"Kmer completeness value is less than 90 for {hap}")
        single = to_number(str(busco_data['BUSCO sing.'].get(hap, '')).rstrip('%'))
        if single is not None and single < 90:
            flags.append(f"BUSCO single copy value is less than 90% for {hap}")
        dupl = to_number(str(busco_data['BUSCO dupl.'].get(hap, '')).rstrip('%'))
        if dupl is not None and dupl > 5:
            flags.append(f"BUSCO duplicated value is more than 5% for {hap}")

    # Assembly-level checks
    for hap in haplotypes:
        scale = ebp_scale.get(hap, {})
        gaps_gbp = gaps_per_gbp_data.get(hap)
        if isinstance(gaps_gbp, (int, float)) and gaps_gbp > 1000:
            flags.append(f"More than 1000 gaps/Gbp for {hap}")

        # Scaffolding: the EBP scaffold target is 'C' (chromosome-scale), not a
        # fixed bp value, so failing it IS this warning. No scaffold N50
        # threshold is checked - L90 > n is the standard's own criterion.
        if scale.get('scaffold_c') is False:
            flags.append(f"Not 90% of assembly in chromosomes for {hap} "
                         f"(scaffold L90 = {format_number(scale.get('scaffold_l90'))}, "
                         f"expected <= {obs_haploid_n})")

        # Contig N50: the '6' of 6.C.Q40, capped at chromosome scale so species
        # whose chromosomes are under a megabase are judged against EBP's C.C
        # target instead of an unreachable 1 Mb bar.
        contig_n50 = scale.get('contig_n50')
        contig_bar = scale.get('contig_bar')
        if contig_n50 is not None and contig_bar and contig_n50 < contig_bar:
            capped = " (capped at chromosome scale)" if contig_bar < EBP_CONTIG_N50_BAR else ""
            # Exact bp, not rounded Mb: 999,999 vs 1,000,000 must not both
            # print as "1.00 Mb" in a line that says one is below the other.
            flags.append(f"Contig N50 below EBP standard for {hap} "
                         f"({format_number(round(contig_n50))} bp, "
                         f"expected >= {format_number(round(contig_bar))} bp){capped}")


    # =======================================================================
    # PDF CONSTRUCTION
    # =======================================================================
    # Output filename: fall back gracefully when ToLID is absent
    if tol_id != "NA":
        pdf_filename = f"{tol_id}_EAR.pdf"
    else:
        safe_species = re.sub(r'\s+', '_', species)
        pdf_filename = f"{safe_species}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_EAR.pdf"
        logging.warning(f"# ToLID missing; naming the report '{pdf_filename}'.")
    pdf_path = os.path.join(outdir, pdf_filename)

    margin = 0.5 * 72
    doc_info = dict(title=f"ERGA Assembly Report - {tol_id if tol_id != 'NA' else species}",
                    author=str(submitter) if submitter != "NA" else "",
                    subject=species,
                    creator=f"make_EAR.py {EAR_version}")
    pdf = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=margin, **doc_info)
    usable_width = pdf.width - 12      # frame width minus the frame's default 6 pt padding
    elements = []

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleStyle', fontName='Courier', fontSize=20))
    styles.add(ParagraphStyle(name='subTitleStyle', fontName='Courier', fontSize=16))
    styles.add(ParagraphStyle(name='normalStyle', fontName='Courier', fontSize=12))
    styles.add(ParagraphStyle(name='midiStyle', fontName='Courier', fontSize=10))
    styles.add(ParagraphStyle(name='treeStyle', fontName='Courier', fontSize=10, leftIndent=12))
    styles.add(ParagraphStyle(name='miniStyle', fontName='Courier', fontSize=8))
    styles.add(ParagraphStyle(name='FileNameStyle', fontName='Courier', fontSize=6))

    # =======================================================================
    # PDF SECTION 1 : header, species, genome traits, EBP, curator notes
    # =======================================================================
    elements.append(Paragraph("ERGA Assembly Report", styles['TitleStyle']))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(EAR_version, styles['normalStyle']))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(f"Tags: {plain_text(tags)}", styles['normalStyle']))
    elements.append(Spacer(1, 24))

    sp_data_table = Table(transposed_sp_data)
    sp_data_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), '#e7e7e7'),
        ("BACKGROUND", (1, 0), (1, -1), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ('FONTNAME', (0, 0), (0, 0), 'Courier'),
        ('FONTNAME', (1, 0), (1, 0), 'Courier'),
        ('FONTNAME', (0, 1), (-1, -1), 'Courier'),
        ('FONTNAME', (1, 1), (1, 1), 'Courier-Bold'),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    elements.append(sp_data_table)
    elements.append(Spacer(1, 32))

    genome_traits_table = Table(genome_traits_table_data)
    genome_traits_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), '#e7e7e7'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),
        ('FONTSIZE', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    elements.append(genome_traits_table)
    elements.append(Spacer(1, 28))

    elements.append(Paragraph("EBP metrics summary and curation notes", styles['subTitleStyle']))
    elements.append(Spacer(1, 24))

    for line in ebp_lines:
        elements.append(Paragraph(plain_text(line), styles["midiStyle"]))

    elements.append(Spacer(1, 8))
    elements.append(Paragraph(
        "The following metrics were automatically flagged as below EBP recommended standards or different from expected:",
        styles['midiStyle']))
    elements.append(Spacer(1, 4))
    for flag in flags:
        elements.append(Paragraph(f". {plain_text(flag)}", styles["midiStyle"]))

    elements.append(Spacer(1, 24))

    # Curator notes
    elements.append(Paragraph("Curator notes", styles['normalStyle']))
    elements.append(Spacer(1, 8))
    curator_notes_text = f". Curation notes: &quot;{plain_text(curation_notes)}&quot;"
    elements.append(Paragraph(curator_notes_text, styles["midiStyle"]))
    elements.append(Spacer(1, 8))

    # Hi-C contact map temp link line (comma-separated across haplotypes)
    link_bits = []
    hic_hash = []
    for hap in haplotypes:
        link = asm_data[hap].get('pretext_temp_link')
        if is_empty(link):
            link_bits.append(f"{hap}: NA")
            hic_hash.append((hap, 'NA'))
        else:
            target = link_target(link)
            if target != str(link).strip():
                logging.info(f"# Hi-C link for {hap} has no 'https://'; using '{target}'.")
            href = escape(target, {'"': '&quot;'})
            link_bits.append(f'{hap}: <link href="{href}" color="blue">[LINK]</link>')
            hic_hash.append((hap, target))
    if all(t == 'NA' for _, t in hic_hash):
        raw_links = [asm_data[h].get('pretext_temp_link') for h in haplotypes]
        if all(is_na(v) for v in raw_links):
            logging.info("# Hi-C Pretext contact map links given as NA.")
        else:
            logging.warning("# No Hi-C Pretext contact map link provided for any haplotype.")
    elements.append(Paragraph("Hi-C contact map temp link: " + ", ".join(link_bits), styles["midiStyle"]))

    elements.append(PageBreak())

    # =======================================================================
    # PDF SECTION 2 : quality metrics table
    # =======================================================================
    elements.append(Paragraph("Quality metrics table", styles['TitleStyle']))
    elements.append(Spacer(1, 48))

    asm_table = Table(asm_table_data)
    asm_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), '#e7e7e7'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    elements.append(asm_table)
    elements.append(Spacer(1, 5))

    # k-mer tool label
    elements.append(Paragraph(f"K-mer tool: {kmer_label}", styles['miniStyle']))

    # BUSCO/compleasm metadata line(s)
    busco_info_list = []    # (hap, info line)
    for hap in haplotypes:
        path = asm_data[hap].get('busco_short')
        if is_empty(path):
            continue
        info = extract_busco_info(str(path).strip())
        if info:
            busco_info_list.append((hap, info))
    if busco_info_list and all(info == busco_info_list[0][1] for _, info in busco_info_list):
        busco_lines = [busco_info_list[0][1]]
    elif busco_info_list:
        busco_lines = (["Warning! Gene-completeness tool/version or lineage differ across results:"]
                       + [f"{hap}: {info}" for hap, info in busco_info_list])
        logging.warning("WARNING: BUSCO/compleasm versions or lineage datasets are not the same across results")
    else:
        busco_lines = ["Gene-completeness metadata: NA"]
    for line in busco_lines:
        elements.append(Paragraph(plain_text(line), styles['miniStyle']))

    elements.append(PageBreak())

    # =======================================================================
    # PDF SECTION 3 : k-mer spectra
    # =======================================================================
    elements.append(Paragraph("K-mer spectra", styles['TitleStyle']))
    elements.append(Spacer(1, 48))

    plots = find_merqury_plots(merqury_folder, run['prefix'] if run else None) if folder_ok \
        else {'spectra_asm': None, 'combined': None, 'per_asm': {}}
    per_asm = dict(plots['per_asm'])
    # Haploid runs have no per-assembly plot: the only copy-number plot IS the assembly plot.
    if not per_asm and plots['combined']:
        per_asm = {None: plots['combined']}
        plots['combined'] = None

    def hap_of(asm_label):
        if asm_label in asm_to_hap:
            return asm_to_hap[asm_label]
        if asm_label is None and len(haplotypes) == 1:
            return haplotypes[0]
        return asm_label or "asm"

    # copy-number plots by haplotype; plots given directly replace the folder's
    per_hap_cn = {hap_of(label): path for label, path in per_asm.items()}
    for hap, path in direct_cn.items():
        given_directly(f"K-mer copy-number plot for {hap}", path, per_hap_cn.get(hap))
        per_hap_cn[hap] = path
    for key, what in (('merqury_spectra_asm', 'K-mer spectra-asm plot'),
                      ('merqury_spectra_cn_combined', 'Combined k-mer copy-number plot')):
        if direct[key]:
            slot = 'spectra_asm' if key == 'merqury_spectra_asm' else 'combined'
            given_directly(what, direct[key], plots[slot])
            plots[slot] = direct[key]

    spectra_images = []
    if plots['spectra_asm']:
        spectra_images.append((plots['spectra_asm'],
                               "Distribution of k-mer counts coloured by their presence in reads/assemblies"))
    hap_order = {h: i for i, h in enumerate(haplotypes)}
    for hap, path in sorted(per_hap_cn.items(), key=lambda kv: (hap_order.get(kv[0], 99), str(kv[0]))):
        spectra_images.append((path, f"Distribution of k-mer counts per copy numbers found in {hap}"))
    if plots['combined']:
        spectra_images.append((plots['combined'],
                               "Distribution of k-mer counts per copy numbers found in all assemblies (combined)"))

    image_cells = []
    spectra_shown = []
    for png_path, caption in spectra_images:
        img = make_image(png_path, 8.4 * cm, 7 * cm, compress_images, label="K-mer spectra plot")
        if img:
            image_cells.append([img, Paragraph(plain_text(caption), styles["midiStyle"])])
            spectra_shown.append({"plot": os.path.basename(png_path), "caption": caption})

    if not image_cells:
        # Entire section is NA -> no page break, let the next section flow on.
        elements.append(Paragraph("NA", styles["midiStyle"]))
        elements.append(Spacer(1, 24))
    else:
        num_columns = 2
        num_rows = (len(image_cells) + 1) // 2
        table_grid = [[image_cells[r * num_columns + c] if r * num_columns + c < len(image_cells) else []
                       for c in range(num_columns)] for r in range(num_rows)]
        spectra_table = Table(table_grid)
        spectra_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 20),
        ]))
        elements.append(spectra_table)
        elements.append(PageBreak())

    # =======================================================================
    # PDF SECTION 4 : contamination screening (FCS-GX + blobplots)
    # =======================================================================
    elements.append(Paragraph("Contamination screening", styles['TitleStyle']))
    elements.append(Spacer(1, 36))

    fcs_hash = {}
    for hap in haplotypes:
        count = fcs_gx_counts.get(hap)
        label = "NA" if count is None else str(count)
        fcs_hash[hap] = label
        elements.append(Paragraph(f"FCS-GX flagged sequences {hap}: {label}", styles["normalStyle"]))
    elements.append(Spacer(1, 36))

    # Blobplots: note missing ones inline (no page per missing plot); render each
    # present plot (large) with the first sharing this page and the rest paginated.
    blob_text_tmpl = ("<b>{hap}.</b> Bubble plot circles are scaled by sequence length, positioned by "
                      "coverage and GC proportion, and coloured by taxonomy. Histograms show total assembly "
                      "length distribution on each axis.")
    blob_side = min(20 * cm, usable_width)
    any_blob = False
    blob_hash = {}
    for hap in haplotypes:
        png_file = asm_data[hap].get('blobplot')
        img = None
        if is_empty(png_file):
            note_missing(f"Blobplot for {hap}", png_file)
        else:
            img = make_image(str(png_file).strip(), blob_side, blob_side, compress_images,
                             label=f"Blobplot for {hap}")
        if img:
            if any_blob:
                elements.append(PageBreak())      # break only between present plots
            elements.append(img)
            elements.append(Paragraph(blob_text_tmpl.format(hap=hap), styles["midiStyle"]))
            any_blob = True
            blob_hash[hap] = os.path.basename(str(png_file).strip())
        else:
            elements.append(Paragraph(f"<b>{hap}</b> blobplot: NA", styles["midiStyle"]))
            blob_hash[hap] = 'NA'

    if any_blob:
        elements.append(PageBreak())
    else:
        # No blobplots at all -> stay compact and let the next section flow on.
        elements.append(Spacer(1, 24))

    # =======================================================================
    # PDF SECTION 5 : alignments info
    # =======================================================================
    elements.append(Paragraph("Alignments info", styles['TitleStyle']))
    elements.append(Spacer(1, 24))

    alignment_field = notes_cfg.get("alignment_png")
    alignment_paths = []
    if not is_empty(alignment_field):
        alignment_paths = [p.strip() for p in str(alignment_field).split(',') if not is_empty(p)]
    else:
        note_missing("Alignment PNG", alignment_field)

    alignment_hash = []
    any_alignment = False
    for png_path in alignment_paths:
        img = make_image(png_path, 16 * cm, 16 * cm, compress_images, label="Alignment plot")
        if img:
            elements.append(img)
            elements.append(Paragraph(f"Alignment plot: {plain_text(os.path.basename(png_path))}",
                                      styles["midiStyle"]))
            elements.append(Spacer(1, 12))
            alignment_hash.append(os.path.basename(png_path))
            any_alignment = True
    if any_alignment:
        elements.append(PageBreak())
    else:
        # Entire section is NA -> no page break, let the next section flow on.
        elements.append(Paragraph("NA", styles["midiStyle"]))
        elements.append(Spacer(1, 24))

    # =======================================================================
    # PDF SECTION 6 : data profile (data types + genome profiling plots), pipelines, footer
    # =======================================================================
    elements.append(Paragraph("Data profile", styles['TitleStyle']))
    elements.append(Spacer(1, 24))

    for line in data_lines:
        elements.append(Paragraph(plain_text(line), styles["normalStyle"]))
    elements.append(Spacer(1, 24))

    profiling_hash = {}
    for title, png in (("GenomeScope", genomescope_png), ("Smudgeplot", smudgeplot_png)):
        elements.append(Paragraph(title, styles['normalStyle']))
        elements.append(Spacer(1, 6))
        img = make_image(str(png).strip(), 9 * cm, 9 * cm, compress_images, label=f"{title} PNG") if png else None
        if img:
            img.hAlign = 'LEFT'
            elements.append(img)
            profiling_hash[title] = os.path.basename(str(png).strip())
        else:
            elements.append(Paragraph("NA", styles["midiStyle"]))
            profiling_hash[title] = 'NA'
        elements.append(Spacer(1, 16))
    elements.append(Spacer(1, 16))

    # Pipelines and the footer are kept together, so the heading is never left
    # alone at the bottom of a page and the footer never ends up on a page by itself.
    closing = [Paragraph("Pipelines", styles['TitleStyle']), Spacer(1, 24)]
    # methods free-text: line breaks survive in the PDF
    closing.append(Paragraph(plain_text(pipeline_text), styles['treeStyle']))
    closing.append(Spacer(1, 48))

    submitter_style = ParagraphStyle(name='SubmitterStyle', fontName='Courier', fontSize=10)
    closing.append(Paragraph(f"Submitter: {plain_text(submitter)}", submitter_style))
    closing.append(Paragraph(f"Affiliation: {plain_text(affiliation)}", submitter_style))
    closing.append(Spacer(1, 8))

    # ---- everything the report displays (except pictures), in page order ------
    # All values are the text shown in the PDF. This is what the Report ID fingerprints
    # and what is stored in the PDF (see read_ear_data).
    report = {
        "version": EAR_version,
        "tags": str(tags),
        "species": {label: str(value) for label, value in zip(sp_data[0], sp_data[1])},
        "genome_traits": {row[0]: {"Expected": str(row[1]), "Observed": str(row[2])}
                          for row in genome_traits_table_data[1:]},
        "ebp_metrics": ebp_codes,
        "flags": flags,
        "curation_notes": str(curation_notes),
        "hic_temp_links": dict(hic_hash),
        "quality_metrics": {hap: {row[0]: str(row[i + 1]) for row in asm_table_data[1:]}
                            for i, hap in enumerate(haplotypes)},
        "kmer_tool": kmer_label,
        "gene_completeness": busco_lines,
        "kmer_spectra": spectra_shown or "NA",
        "fcs_gx": fcs_hash,
        "blobplots": blob_hash,
        "alignments": alignment_hash or "NA",
        "data_profile": data_lines if data_lines != ["NA"] else "NA",
        "profiling": profiling_hash,
        "pipeline": str(pipeline_text),
        "submitter": str(submitter),
        "affiliation": str(affiliation),
    }

    # ---- document fingerprint: hash of the displayed data ------------------
    report = xml_safe(report)
    doc_hash = report_id_of(report)
    closing.append(Paragraph(f"Report ID: {doc_hash}", submitter_style))
    closing.append(Spacer(1, 8))

    # date/time is displayed but deliberately NOT part of the hash
    cet = pytz.timezone("CET")
    formatted_datetime = datetime.now(cet).strftime("%Y-%m-%d %H:%M:%S %Z")
    closing.append(Paragraph(f"Date and time: {formatted_datetime}", submitter_style))
    elements.append(KeepTogether(closing))

    # ---- run summary: everything that shaped the checks, in one place ------
    for hap in haplotypes:
        summary.append(f"{hap}: FCS-GX {fcs_hash[hap]}, blobplot {blob_hash.get(hap, 'NA')}")
    summary.append(f"Profiling: GenomeScope {profiling_hash['GenomeScope']}, Smudgeplot {profiling_hash['Smudgeplot']}; "
                   f"alignment plots: {', '.join(alignment_hash) or 'NA'}")
    summary.append(f"Flags: {len(flags)}; Report ID: {doc_hash}")
    for line in summary:
        logging.info(f"[summary] {line}")

    # ---- store the report data (and this run's log) in the PDF's XMP metadata ----
    ear_data = {"format": EAR_DATA_FORMAT, "report_id": doc_hash, "date": formatted_datetime,
                "report": report, "log": list(log_capture.lines)}
    xmp = xmp_packet(ear_data, **xml_safe(doc_info))

    def store_data(canvas, doc):
        canvas.setCatalogEntry("Metadata", PDFStream(
            dictionary=PDFDictionary({"Type": PDFName("Metadata"), "Subtype": PDFName("XML")}),
            content=xmp.encode("utf-8")))

    pdf.build(elements, onFirstPage=store_data)
    logging.info(f"# Report written to '{os.path.abspath(pdf_path)}'")
    if read_ear_data(pdf_path) != ear_data:
        logging.warning("# The report data could not be read back from the PDF metadata; "
                        "the PDF itself is fine.")
    return {
        'pdf': pdf_path,
        'report_id': doc_hash,
        'payload': report,
        'data': ear_data,
        'details': {
            'merqury_run': {'prefix': run['prefix'] if run else None, 'qv': qv_path, 'completeness': comp_path},
            'asm_to_hap': asm_to_hap,
            'qv': qv_data,
            'completeness': completeness_data,
            'spectra': [(os.path.basename(p), c) for p, c in spectra_images],
        },
    }


# ===========================================================================
# COMMAND LINE
# ===========================================================================

class _WarningCounter(logging.Handler):
    """Counts warnings and errors so the terminal summary can report them."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        self.count += 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="make_EAR.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Create an ERGA Assembly Report (EAR) PDF from a YAML file and/or command-line flags.\n"
                    "Flags override YAML values; the YAML file is optional.\n"
                    "More information: https://github.com/ERGA-consortium/EARs",
        epilog="Per-haplotype flags take comma-separated lists matched by position: the 1st value goes to\n"
               "hap1, the 2nd to hap2, and so on (up to 4). Leave a position empty to skip it,\n"
               "e.g. --fcs_gx ',h2_fcs.txt' gives hap1 no FCS-GX report.\n"
               "Relative paths are read from the folder you run the command in.\n"
               "\n"
               "Examples:\n"
               "  python make_EAR.py --yaml EAR.yaml\n"
               "  python make_EAR.py --yaml EAR.yaml --curation_notes notes.txt\n"
               "  python make_EAR.py --species 'Homo sapiens' --obs_haploid_num 23 \\\n"
               "      --gfastats_nstar h1_stats.txt,h2_stats.txt \\\n"
               "      --busco_short h1_busco.txt,h2_busco.txt --merqury_folder merqury/ \\\n"
               "      --PIPELINE 'hifiasm 0.19 Hi-C mode, YaHS default'")
    parser.add_argument('--version', action='version', version=f'make_EAR.py {EAR_version}')

    general = parser.add_argument_group("input")
    general.add_argument('--yaml', metavar='YAML',
                         help="YAML file with any of the fields below (optional)")

    sample = parser.add_argument_group("sample information")
    sample.add_argument('--tolid', metavar='TOLID',
                        help="ToLID, checked against the ToLID registry (optional)")
    sample.add_argument('--species', metavar='\'GENUS SPECIES\'',
                        help="species name, e.g. 'Homo sapiens' (mandatory unless in the YAML)")
    sample.add_argument('--sex', metavar='SEX',
                        help="expected sample sex, e.g. XX, XY, ZW, X0, unknown (optional)")
    sample.add_argument('--submitter', metavar='\'NAME\'', help="submitter full name (optional)")
    sample.add_argument('--affiliation', metavar='\'NAME\'', help="submitter affiliation (optional)")
    sample.add_argument('--tags', metavar='TAGS', help="e.g. ERGA-BGE (optional)")

    data = parser.add_argument_group("sequencing data")
    data.add_argument('--DATA', metavar='TYPE[:COVERAGE],...',
                      help="data types, each with an optional coverage, comma-separated, "
                           "e.g. 'HiFi:30,Hi-C' (optional)")

    profiling = parser.add_argument_group("genome profiling")
    profiling.add_argument('--genomescope', metavar='PNG', help="GenomeScope linear plot (optional)")
    profiling.add_argument('--smudgeplot', metavar='PNG', help="Smudgeplot (optional)")

    assembly = parser.add_argument_group("assembly data (comma-separated, one value per haplotype, in hap order)")
    assembly.add_argument('--gfastats_nstar', metavar='TXT[,TXT,...]',
                          help="gfastats --nstar-report output per haplotype "
                               "(mandatory for at least one haplotype unless in the YAML)")
    assembly.add_argument('--busco_short', metavar='TXT[,TXT,...]',
                          help="BUSCO short summary or compleasm summary.txt per haplotype "
                               "(mandatory for at least one haplotype unless in the YAML)")
    assembly.add_argument('--fcs_gx', metavar='TXT[,TXT,...]',
                          help="FCS-GX action report per haplotype (optional)")
    assembly.add_argument('--blobplot', metavar='PNG[,PNG,...]',
                          help="blob plot per haplotype (optional)")
    assembly.add_argument('--pretext_temp_link', metavar='URL[,URL,...]',
                          help="web link to the Pretext contact map (file or folder) per haplotype (optional)")

    kmer = parser.add_argument_group(
        "k-mer analysis (Merqury or MerquryFK, one run covering all haplotypes)",
        "Give the output folder, the files one by one (any file names, e.g. downloaded from Galaxy),\n"
        "or both: a file given directly replaces the one found in the folder. All optional.")
    kmer.add_argument('--merqury_folder', metavar='FOLDER',
                      help="output folder; the QV, completeness and plots are found in it")
    kmer.add_argument('--merqury_qv', metavar='TXT',
                      help="summary QV file <prefix>.qv (not the per-scaffold <prefix>.<asm>.qv)")
    kmer.add_argument('--merqury_completeness', metavar='TXT', help="<prefix>.completeness.stats")
    kmer.add_argument('--merqury_spectra_asm', metavar='PNG', help="spectra-asm plot")
    kmer.add_argument('--merqury_spectra_cn', metavar='PNG[,PNG,...]',
                      help="copy-number spectra plot per haplotype, comma-separated in hap order")
    kmer.add_argument('--merqury_spectra_cn_combined', metavar='PNG',
                      help="copy-number spectra plot of all assemblies combined")

    methods = parser.add_argument_group("methods and curation")
    methods.add_argument('--PIPELINE', metavar='\'TEXT\' | TXT',
                         help="methods as free text in quotes, or the path to a text file (optional)")
    methods.add_argument('--obs_haploid_num', metavar='N',
                         help="observed haploid number n as a whole number, e.g. 23 "
                              "(mandatory unless in the YAML)")
    methods.add_argument('--obs_ploidy', metavar='N', help="observed ploidy, e.g. 2 (optional)")
    methods.add_argument('--obs_sex', metavar='SEX', help="observed sex, e.g. XX, XY, ZW (optional)")
    methods.add_argument('--curation_notes', metavar='\'TEXT\' | TXT',
                         help="curation notes as free text in quotes, or the path to a text file (optional)")
    methods.add_argument('--alignment_png', metavar='PNG[,PNG,...]',
                         help="alignment plot(s) between haplotypes, e.g. D-GENIES or SyRI (optional)")

    output = parser.add_argument_group("output")
    output.add_argument('--no_image_compression', action='store_true',
                        help="embed plots exactly as provided (larger PDF). By default plots are "
                             "flattened, downsampled to 300 dpi at their printed size and reduced to 256 colours")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    log_path = os.path.abspath(f"EAR_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(LOG_FORMAT)
    counter = _WarningCounter()
    capture = _LogCapture()          # copy of this run's log stored in the PDF
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (file_handler, counter, capture):
        root.addHandler(handler)

    result = None
    try:
        logging.info(f"# make_EAR.py {EAR_version}, run from '{os.getcwd()}'")
        config = build_config(args)
        result = make_report(config, compress_images=not args.no_image_compression, log_capture=capture)
    except EARError as e:
        logging.error(f"# {e} Report not produced.")
        print(f"ERROR: {e}", file=sys.stderr)
    except Exception as e:
        logging.exception("# Unexpected error; report not produced.")
        print(f"ERROR: unexpected problem: {e}", file=sys.stderr)
    finally:
        for handler in (file_handler, counter, capture):
            root.removeHandler(handler)
        file_handler.close()

    if result is None:
        print(f"Report not produced. Details in the log: {log_path}", file=sys.stderr)
        return 1
    plural = "" if counter.count == 1 else "s"
    print(f"Report written: {os.path.abspath(result['pdf'])}")
    print(f"Log: {log_path} ({counter.count} warning{plural})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
