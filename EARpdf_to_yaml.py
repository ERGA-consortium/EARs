# EARpdf_to_yaml.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee
version = "v24.10.16"

import pdfplumber
import yaml
import sys
import re
import argparse
import os
from collections import OrderedDict


def custom_representer(dumper, data):
    return dumper.represent_dict(data.items())

yaml.add_representer(OrderedDict, custom_representer)



def extract_text_from_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = ''
        for page in pdf.pages:
            text += page.extract_text() + '\n'
    return text


# Species table
def extract_basic_info(text):
    patterns = {
        "ERGA Assembly Report": r"ERGA Assembly Report\s+(.+)",
        "Tags": r"Tags:\s+(.+)",
        "TxID": r"TxID\s+(\d+)",
        "ToLID": r"ToLID\s+(\w+)",
        "Species": r"Species\s+(.+)",
        "Class": r"Class\s+(.+)",
        "Order": r"Order\s+(.+)"
    }
    info = OrderedDict()
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            info[key] = match.group(1).strip()
    return info


# Exp/Obs traits table
def extract_genome_traits(text):
    traits = OrderedDict({"Expected": OrderedDict(), "Observed": OrderedDict()})
    trait_patterns = [
        r"Haploid size \(bp\)\s+(\S+)\s+(\S+)",
        r"Haploid Number\s+(\S+(?:\s+\([^)]+\))?)\s+(\S+)",
        r"Ploidy\s+(\S+(?:\s+\([^)]+\))?)\s+(\S+)",
        r"Sample Sex\s+(\S+)\s+(\S+)"
    ]
    trait_names = ["Haploid size (bp)", "Haploid Number", "Ploidy", "Sample Sex"]
    
    for pattern, name in zip(trait_patterns, trait_names):
        match = re.search(pattern, text)
        if match:
            traits["Expected"][name] = match.group(1)
            traits["Observed"][name] = match.group(2)
        else:
            traits["Expected"][name] = ""
            traits["Observed"][name] = ""
    
    return traits


# Only EBP metrics
def extract_ebp_metrics(text):
    ebp_metrics = OrderedDict()
    match = re.search(r"Obtained EBP quality metric for (\w+): (.+)", text)
    if match:
        ebp_metrics["EBP quality code"] = {match.group(1): match.group(2)}
    return ebp_metrics


# Curator notes
def extract_curator_notes(text):
    curator_notes = OrderedDict()
    start = text.find("Curator notes")
    end = text.find("Quality metrics table")
    if start != -1 and end != -1:
        notes_text = text[start:end]
        
        # Extract Interventions/Gb
        interventions_match = re.search(r"Interventions/Gb:\s*(\d+)", notes_text)
        if interventions_match:
            curator_notes["Interventions/Gb"] = int(interventions_match.group(1))
        
        # Extract Contamination notes
        contamination_match = re.search(r'Contamination notes:\s*"(.*?)"(?=\s*\.\s*Other observations|\s*$)', notes_text, re.DOTALL)
        if contamination_match:
            contamination_notes = contamination_match.group(1).strip()
            contamination_notes = ' '.join(contamination_notes.split())
            curator_notes["Contamination notes"] = f'"{contamination_notes}"'
        
        # Extract Other observations
        other_observations_match = re.search(r'Other observations:\s*"(.*?)"(?=\s*$)', notes_text, re.DOTALL)
        if other_observations_match:
            other_observations = other_observations_match.group(1).strip()
            other_observations = ' '.join(other_observations.split())
            curator_notes["Other observations"] = f'"{other_observations}"'

    return curator_notes


# Metrics table
def extract_metrics_table(text):
    start_phrase = "Quality metrics table"
    end_phrase = "HiC contact map of curated assembly"

    start_index = text.find(start_phrase)
    end_index = text.find(end_phrase, start_index)

    if start_index == -1 or end_index == -1:
        return OrderedDict()

    table_text = text[start_index:end_index]
    lines = [line.strip() for line in table_text.split('\n') if line.strip()]

    metrics = OrderedDict()
    
    expected_rows = [
        "Total bp", "GC %", "Gaps/Gbp", "Total gap bp", "Scaffolds",
        "Scaffold N50", "Scaffold L50", "Scaffold L90", "Contigs",
        "Contig N50", "Contig L50", "Contig L90", "QV", "Kmer compl.",
        "BUSCO sing.", "BUSCO dupl.", "BUSCO frag.", "BUSCO miss."
    ]

    # Find header lines
    header_index = -1
    for i, line in enumerate(lines):
        if "Pre-curation" in line or "Curated" in line:
            header_index = i
            break

    if header_index == -1 or header_index + 1 >= len(lines):
        return OrderedDict()

    header1 = lines[header_index].split()
    header2 = lines[header_index + 1].split()

    # Get number of columns and their headers
    if len(header1) == 2:  # two-column case
        column_headers = [f"{header1[0]} {header2[1]}", f"{header1[1]} {header2[2]}"]
    else:  # four-column case
        column_headers = [f"{header1[i]} {header2[i+1]}" for i in range(len(header1))]

    for header in column_headers:
        metrics[header] = OrderedDict()

    # Parse metrics
    for line in lines[header_index + 2:]:
        parts = line.split()
        if len(parts) >= len(column_headers):
            for expected_row in expected_rows:
                if line.startswith(expected_row):
                    metric_name = expected_row
                    values = parts[len(metric_name.split()):]
                    if len(values) == len(column_headers):
                        for i, header in enumerate(column_headers):
                            metrics[header][metric_name] = values[i]
                    break

    # Remove any empty dictionaries
    metrics = OrderedDict({k: v for k, v in metrics.items() if v})

    return metrics


# BUSCO info
def extract_busco_lineage(text):
    # Check warning case
    if re.search(r"Warning[!:]?\s+BUSCO versions or lineage datasets are not the same across results", text, re.IGNORECASE):
        return {
            "ver": "WARNING! possible version mismatch",
            "lineage": "WARNING! possible lineage mismatch"
        }
    
    # Regular expression to match the BUSCO info
    match = re.search(r"BUSCO:?\s+(\d+(?:\.\d+)*(?:\s+\([^)]+\))?)\s*(?:/\s*)?Lineage:\s+([^(\n]+)(?:\s*\([^)]+\))?", text)
    
    if match:
        version = match.group(1).strip()
        lineage = match.group(2).strip()
        return {
            "ver": version,
            "lineage": lineage
        }
    
    # Return None if neither the warning nor the expected line is found
    return None


# Data table
def extract_data_profile(text):
    data = OrderedDict()
    start = text.find("Data profile")
    end = text.find("Assembly pipeline")
    
    if start != -1 and end != -1:
        profile_text = text[start:end]
        
        lines = [line.strip() for line in profile_text.split('\n') if line.strip()]
        
        if len(lines) >= 3:  # We need at least 3 lines: "Data profile", Data line, and Coverage line
            data_line = lines[1]  # Second line
            coverage_line = lines[2]  # Third line
            
            # Extract profile and coverage information
            profile = ' '.join(data_line.split()[1:])  # Remove "Data" from the start
            coverage = ' '.join(coverage_line.split()[1:])  # Remove "Coverage" from the start
            
            # Add to data dictionary
            data['Profile'] = profile
            data['Coverage'] = coverage

    return data


# Pipelines info
def extract_pipeline_info(text, pipeline_name):
    pipeline_info = OrderedDict()
    start = text.find(f"{pipeline_name} pipeline")
    end = text.find("pipeline", start + len(f"{pipeline_name} pipeline"))
    if end == -1:  # If it's the last pipeline section
        end = len(text)

    if start != -1 and end != -1:
        pipeline_text = text[start:end]
        lines = pipeline_text.split('\n')
        current_tool = None

        for line in lines:
            line = line.strip()
            if line.startswith('-'):
                current_tool = line.strip('- ').strip(':')
                pipeline_info[current_tool] = OrderedDict()
            elif current_tool and (':' in line):
                key, value = line.split(':', 1)
                key = key.strip().replace('|_', '').strip()
                value = value.strip()
                if key == 'ver' or (key == 'key param' and value.lower() != 'na'):
                    pipeline_info[current_tool][key] = value

    return pipeline_info


#####
def extract_data_from_pdf(pdf_path):
    text = extract_text_from_pdf(pdf_path)

    data = extract_basic_info(text)
    data["Genome Traits"] = extract_genome_traits(text)
    data["EBP metrics"] = extract_ebp_metrics(text)
    data["Curator notes"] = extract_curator_notes(text)
    data["Metrics"] = extract_metrics_table(text)
    busco_info = extract_busco_lineage(text)
    if busco_info:
        data["BUSCO"] = busco_info
    data["Data"] = extract_data_profile(text)  # Changed this line
    data["Assembly pipeline"] = extract_pipeline_info(text, "Assembly")
    data["Curation pipeline"] = extract_pipeline_info(text, "Curation")

    # Extract submission info
    submission_info = re.findall(r"(Submitter|Affiliation|Date and time):\s*(.+)", text)
    for key, value in submission_info:
        data[key] = value.strip()

    return data


def save_to_yaml(data, output_path):
    # Custom YAML dumper to control formatting
    class CustomDumper(yaml.Dumper):
        def increase_indent(self, flow=False, indentless=False):
            return super(CustomDumper, self).increase_indent(flow, False)

    def str_presenter(dumper, data):
        if '\n' in data:  # check for multiline string
            return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
        return dumper.represent_scalar('tag:yaml.org,2002:str', data)

    yaml.add_representer(str, str_presenter)

    # Convert to YAML string
    yaml_string = yaml.dump(data, Dumper=CustomDumper, default_flow_style=False, sort_keys=False)

    # Post-process the YAML string
    lines = yaml_string.split('\n')
    processed_lines = []
    sections_to_add_space_before = [
        "EBP metrics:", "Metrics:", "Curator notes:", "BUSCO:",
        "Data:", "Assembly pipeline:", "Curation pipeline:", "Submitter:", "Date and time:"
    ]
    
    for i, line in enumerate(lines):
        if any(line.startswith(section) for section in sections_to_add_space_before):
            processed_lines.append("")  # Add a blank line before these sections
        processed_lines.append(line)
        if line.startswith("Tags:") or line.startswith("Order:"):
            processed_lines.append("")  # Add a blank line after Tags and Order

    # Join lines back together
    processed_yaml = '\n'.join(processed_lines)

    # Write to file
    with open(output_path, 'w') as yaml_file:
        yaml_file.write(processed_yaml.strip() + '\n')



def main():
    parser = argparse.ArgumentParser(
        description=f"EARpdf_to_yaml {version} - Parse EAR PDF and convert to YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pdf_file", nargs="?", help="Input PDF file path")
    parser.add_argument("--pdf", help="Input PDF file path (alternative to positional argument)")
    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    if args.pdf:
        input_pdf = args.pdf
    elif args.pdf_file:
        input_pdf = args.pdf_file
    else:
        print("Error: No input PDF file specified.", file=sys.stderr)
        parser.print_help(sys.stderr)
        sys.exit(1)

    output_yaml = os.path.splitext(os.path.basename(input_pdf))[0] + '.yaml'

    extracted_data = extract_data_from_pdf(input_pdf)
    save_to_yaml(extracted_data, output_yaml)

    print(f"Data has been extracted from {input_pdf} and saved to {output_yaml}")

if __name__ == "__main__":
    main()