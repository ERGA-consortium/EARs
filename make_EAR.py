# make_EAR.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee

EAR_version = "v12.09.23_beta"

import sys
import argparse
import logging
import re
import yaml
import os
import pytz
import requests
import json
import glob
from math import ceil
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import cm



def make_report(yaml_file):

    logging.basicConfig(filename='EAR.log', level=logging.INFO)

    # Read the content of the EAR.yaml file
    with open(yaml_file, "r") as file:
        yaml_data = yaml.safe_load(file)


    ###################################################################################################

    # Reading GENERAL INFORMATION section from yaml -------------------------------

    # Check for required fields
    required_fields = ["ToLID", "Species", "Submitter", "Affiliation", "Tags"]

    missing_fields = [field for field in required_fields if field not in yaml_data or not yaml_data[field]]

    if missing_fields:
        logging.error(f"# GENERAL INFORMATION section in the yaml file is missing or empty for the following information: {', '.join(missing_fields)}")
        sys.exit(1)

    # Check that "Species" field is a string
    if not isinstance(yaml_data["Species"], str):
        logging.error(f"# GENERAL INFORMATION section in the yaml file contains incorrect data type for 'Species'. Expected 'str' but got '{type(yaml_data['Species']).__name__}'.")
        sys.exit(1)


    ## Get data for Header, ToLID table and submitter
    tol_id = yaml_data["ToLID"]
    species = yaml_data["Species"]
    sex = yaml_data["Sex"]
    submitter = yaml_data["Submitter"]
    affiliation = yaml_data["Affiliation"]
    tags = yaml_data["Tags"] 

    # Get data from GoaT based on species name

    # urllib.parse.quote to handle special characters and spaces in the species name
    species_name = requests.utils.quote(species)

    # get from goat
    goat_response = requests.get(f'https://goat.genomehubs.org/api/v2/search?query=tax_name%28{species_name}%29&result=taxon')
    goat_data = goat_response.json() # convert json to dict

    goat_results = goat_data['results']

    class_name = 'NA'
    order_name = 'NA'
    haploid_number = 'NA'
    haploid_source = 'NA'
    chrom_num = 'NA'
    ploidy = 'NA'

    for result in goat_results:
        lineage = result['result']['lineage']
        fields = result['result']['fields']

        for node in lineage:
            if node['taxon_rank'] == 'class':
                class_name = node['scientific_name']
            if node['taxon_rank'] == 'order':
                order_name = node['scientific_name']

        if 'haploid_number' in fields:
            haploid_number = fields['haploid_number']['value']
            haploid_source = fields['haploid_number']['aggregation_source']

        if 'chromosome_number' in fields:
            chrom_num = fields['chromosome_number']['value']

        if haploid_number != 'NA' and chrom_num != 'NA':
            ploidy = (round(chrom_num / haploid_number))


    # Make species data table -----------------------
    sp_data = [
        ["ToLID", "Species", "Class", "Order", "Haploid Number", "Ploidy", "Sex"],
        [tol_id, species, class_name, order_name, f"{haploid_number} (source: {haploid_source})", ploidy, sex]
    ]

    # Transpose the data
    transposed_sp_data = list(map(list, zip(*sp_data)))

    # Create the table with the transposed data
    sp_data_table = Table(transposed_sp_data)

    # Style the table
    sp_data_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ('FONTNAME', (0, 0), (0, 0), 'Courier'),  # regular font for row1, col1
        ('FONTNAME', (1, 0), (1, 0), 'Courier-Bold'),  # bold font for row1, col2
        ('FONTNAME', (0, 1), (-1, -1), 'Courier'),  # regular font for the rest of the table
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black)
    ]))



    # Reading SEQUENCING DATA section from yaml -----------------------------------

    # get DATA section from yaml
    data_list = yaml_data.get('DATA')

    # Get k v from DATA
    reads_data_table = [['Data', 'Coverage']]  # headers
    if data_list:
        for item in data_list:
            for technology, coverage in item.items():
                if not coverage:  # if coverage is empty or None
                    coverage = 'NA'
                reads_data_table.append([technology, coverage])
    else:
        logging.warning('Warning: No data found in the YAML file.')    

    # create the data table
    data_table = Table(reads_data_table)

    # Style the table
    data_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), '#d3d3d3'),  # grey background for the header
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),       # center alignment
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),  # bold font for the header
        ('FONTSIZE', (0, 0), (-1, -1), 12),           # font size for the header
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black)
    ]))



    # Get names of pipeline tools/versions ----------------------------------------

    # Get k v for Pipeline section
    pipeline_table_data = [['Tool', 'Version']]  # headers

    for section, section_content in yaml_data.items():
        if isinstance(section_content, dict):
            for tool, tool_properties in section_content.items():
                if isinstance(tool_properties, dict):
                    version = tool_properties.get('version')
                    # replace empty values with 'NA'
                    if not version:
                        version = 'NA'
                    pipeline_table_data.append([tool, version])

    # create pipeline table
    pipeline_table = Table(pipeline_table_data)
    pipeline_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), '#d3d3d3'),  # grey background for the header
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),       # center alignment
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),  # bold font for the header
        ('FONTSIZE', (0, 0), (-1, -1), 12),           # font size for the header
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black)
    ]))



    # Reading GENOME PROFILING DATA section from yaml -----------------------------

    profiling_data = yaml_data.get('PROFILING')
    
    # Check if profiling_data is available
    if not profiling_data:
        logging.error('Error: No profiling data found in the YAML file.')
        sys.exit(1)

    tools = ['GenomeScope', 'Smudgeplot']
    required_fields = ['results_folder']

    smudgeplot_folder = None  # Initialize smudgeplot_folder to None

    # Loop through the required tools
    for tool in tools:
        tool_data = profiling_data.get(tool)

        # Check if tool data is present
        if not tool_data:
            if tool == 'Smudgeplot':
                logging.warning(f"# PROFILING section in the yaml file is missing or empty for {tool} information. Skipping {tool}.")
                continue
            else:
                logging.error(f"# PROFILING section in the yaml file is missing or empty for {tool} information.")
                sys.exit(1)

        # Check for required fields and log an error message if any are missing
        for field in required_fields:
            if not tool_data.get(field):
                if tool == 'Smudgeplot':
                    logging.warning(f"# {tool} information in the PROFILING section in the yaml file is missing or empty for {field} information. Skipping {tool}.")
                    break
                else:
                    logging.error(f"# {tool} information in the PROFILING section in the yaml file is missing or empty for {field} information.")
                    sys.exit(1)


        # Get version if it's there, otherwise assign 'NA'
        tool_version = tool_data.get('version', 'NA')

        # Assign data to specific variables
        if tool == 'GenomeScope':
            genomescope_folder = tool_data['results_folder']
        else:
            smudgeplot_folder = tool_data['results_folder']



    # Read the content of the summary.txt file
    summary_file_path = os.path.join(genomescope_folder, '*summary.txt')
    summary_file = glob.glob(summary_file_path)[0]
    with open(summary_file, "r") as f:
        summary_txt = f.read()

    # Extract values from summary.txt
    genome_haploid_length = re.search(r"Genome Haploid Length\s+([\d,]+) bp", summary_txt).group(1)
    heterozygous = re.search(r"Heterozygous \(ab\)\s+([\d.]+)%", summary_txt).group(1)
    #p_value = re.search(r"p\s+=\s+(\d+)", summary_txt).group(1)

    # Read the content of model.txt
    model_file_path = os.path.join(genomescope_folder, '*model.txt')
    model_file = glob.glob(model_file_path)[0]
    with open(model_file, "r") as f:
        model_txt = f.read()

    # Extract kmercov value
    kmercov_search = re.search(r'kmercov\s+([\d.]+)e([+-]\d+)', model_txt)
    kmercov_value = float(kmercov_search.group(1)) * 10**float(kmercov_search.group(2))

    # Get the paths of genomescope pngs
    lin_plot_file_path = os.path.join(genomescope_folder, '*transformed_linear_plot.png')
    lin_plot_file = glob.glob(lin_plot_file_path)[0]
    log_plot_file_path = os.path.join(genomescope_folder, '*transformed_log_plot.png')
    log_plot_file = glob.glob(log_plot_file_path)[0]

    # Read the content of the smudgeplot_verbose_summary.txt file
    smu_plot_file = 'NA'

    if smudgeplot_folder is not None:
        smud_summary_file_path = os.path.join(smudgeplot_folder, '*verbose_summary.txt')
        smud_summary_file = glob.glob(smud_summary_file_path)[0]
        with open(smud_summary_file, "r") as f:
            smud_summary_txt = f.readlines()

        for line in smud_summary_txt:
            if line.startswith("* Proposed ploidy"):
                proposed_ploidy = line.split(":")[1].strip()

        # Get the paths of smudgeplots pngs
        smu_plot_file_path = os.path.join(smudgeplot_folder, '*smudgeplot.png')
        smu_plot_file = glob.glob(smu_plot_file_path)[0]
        smulog_plot_file_path = os.path.join(smudgeplot_folder, '*smudgeplot_log10.png')
        smulog_plot_file = glob.glob(smulog_plot_file_path)[0]

    else:
        proposed_ploidy = 'NA'
        smu_plot_file = None

        # Get the genome all profiling data
    profiling_data = [
        ["Estimated Haploid Length", "Heterozygosity rate", "Kmer coverage", "Proposed ploidy"],
        [genome_haploid_length, f"{heterozygous}%", round(kmercov_value, 2), proposed_ploidy]
    ]

    # Transpose profiling data
    transposed_profiling_data = list(map(list, zip(*profiling_data)))

    # Create the summary table
    profiling_table = Table(transposed_profiling_data)
    profiling_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),  # Use Courier font
        ('FONTSIZE', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black)
    ]))



    # Functions for Genome assembly sections --------------------------------------

    def format_number(value):
        try:
            value_float = float(value)
            if value_float.is_integer():
                # format as an integer if no decimal part
                return f'{int(value_float):,}'
            else:
                # format as a float
                return f'{value_float:,}'
        except ValueError:
            # return the original value if it can't be converted to a float
            return value


    # extract gfastats values
    def extract_gfastats_values(content, keys):
        return [re.findall(f"{key}: (.+)", content)[0] for key in keys]

    keys = [
        "Total scaffold length",
        "GC content %",
        "# gaps in scaffolds",
        "Total gap length in scaffolds",
        "# scaffolds",
        "Largest scaffold",
        "Scaffold auN",
        "Scaffold N50",
        "Scaffold L50",
        "Scaffold L90",
        "# contigs",
        "Largest contig",
        "Contig auN",
        "Contig N50",
        "Contig L50",
        "Contig L90",
    ]

    display_names = keys.copy()
    display_names[display_names.index("Total scaffold length")] = "Total bp"
    display_names[display_names.index("GC content %")] = "GC %"
    display_names[display_names.index("# gaps in scaffolds")] = "Gaps"
    display_names[display_names.index("Total gap length in scaffolds")] = "Gaps bp"
    display_names[display_names.index("# scaffolds")] = "Scaffolds"
    display_names[display_names.index("Largest scaffold")] = "Longest Scaf."
    display_names[display_names.index("# contigs")] = "Contigs"
    display_names[display_names.index("Largest contig")] = "Largest Cont."

    gaps_index = keys.index("# gaps in scaffolds")
    total_length_index = keys.index("Total scaffold length")

    
    # extract qv values
    def get_qv_value(dir_path, order, tool, haplotype):
        try:
            file_paths = glob.glob(f"{dir_path}/*.qv")
            for file_path in file_paths:
                with open(file_path, 'r') as file:
                    lines = file.readlines()
                    if len(lines) > order and lines[2].split('\t')[0].strip() == "Both":
                        target_line = lines[order]
                        fourth_column_value = target_line.split('\t')[3]
                        return fourth_column_value
        except Exception as e:
            logging.error(f"Error reading {dir_path}: {str(e)}")
        return ''


    # extract Kmer completeness values       
    def get_completeness_value(dir_path, order, tool, haplotype):
        try:
            file_paths = glob.glob(f"{dir_path}/*completeness.stats")
            for file_path in file_paths:
                with open(file_path, 'r') as file:
                    lines = file.readlines()
                    if len(lines) > order:
                        target_line = lines[order]
                        fifth_column_value = target_line.split('\t')[4].strip()
                        return fifth_column_value
        except Exception as e:
            logging.warning(f"Error reading {dir_path}: {str(e)}")
            return ''


    # extract BUSCO values
    def extract_busco_values(file_path):
        try:
            with open(file_path, 'r') as file:
                content = file.read()
                results_line = re.findall(r"C:.*n:\d+", content)[0]
                s_value = re.findall(r"S:(\d+\.\d+%)", results_line)[0]
                d_value = re.findall(r"D:(\d+\.\d+%)", results_line)[0]
                f_value = re.findall(r"F:(\d+\.\d+%)", results_line)[0]
                m_value = re.findall(r"M:(\d+\.\d+%)", results_line)[0]
                return s_value, d_value, f_value, m_value
        except Exception as e:
            logging.warning(f"Error reading {file_path}: {str(e)}")
            return '', '', '', ''


    def extract_busco_lineage(file_path):
        try:
            with open(file_path, 'r') as file:
                content = file.read()
                lineage_info = re.search(r"The lineage dataset is: (.*?) \(Creation date:.*?, number of genomes: (\d+), number of BUSCOs: (\d+)\)", content)
                if lineage_info:
                    return lineage_info.groups()
                else:
                    lineage_info = re.search(r"The lineage dataset is: (.*?) \(Creation date:.*?, number of species: (\d+), number of BUSCOs: (\d+)\)", content)
                    if lineage_info:
                        return lineage_info.groups()
                    else:
                        return None
        except Exception as e:
            logging.warning(f"Error reading {file_path}: {str(e)}")
            return None


    # this is for getting the kmer plots for contigging section
    def get_png_files(dir_path):
        png_files = glob.glob(f"{dir_path}/*.st.png")
        if len(png_files) < 4:
            logging.warning(f"Warning: Less than 4 png files found in {dir_path}. If this is diploid, some images may be missing.")
        return png_files[:4]    



    # Contigging section ----------------------------------------------------------

    contigging_data = yaml_data.get('CONTIGGING', {})

    # make a list from the assemblies available in contigging
    haplotypes = []
    for tool, tool_properties in contigging_data.items():
        for key in tool_properties.keys():
            if key != 'version' and key not in haplotypes:
                haplotypes.append(key)

    # get gfastats-based data
    gfastats_data = {}
    for tool, tool_properties in contigging_data.items():
        for haplotype, haplotype_properties in tool_properties.items():
            if isinstance(haplotype_properties, dict):
                if 'gfastats--nstar-report_txt' in haplotype_properties:
                    file_path = haplotype_properties['gfastats--nstar-report_txt']
                    with open(file_path, 'r') as file:
                        content = file.read()
                    gfastats_data[(tool, haplotype)] = extract_gfastats_values(content, keys)

    gaps_per_gbp_data = {}
    for (tool, haplotype), values in gfastats_data.items():
        try:
            gaps = float(values[gaps_index])
            total_length = float(values[total_length_index])
            gaps_per_gbp = round((gaps / total_length * 1_000_000_000), 2)
            gaps_per_gbp_data[(tool, haplotype)] = gaps_per_gbp
        except (ValueError, ZeroDivisionError):
            gaps_per_gbp_data[(tool, haplotype)] = ''


    # Define the contigging table (column names) DON'T MOVE THIS AGAIN!!!!!!!            
    cont_table_data = [["Metrics"] + [f'{tool} \n {haplotype}' for tool in contigging_data for haplotype in haplotypes if haplotype in contigging_data[tool]]]

    # Fill the table with the gfastats data
    for i in range(len(display_names)):
        metric = display_names[i]
        cont_table_data.append([metric] + [format_number(gfastats_data.get((tool, haplotype), [''])[i]) if (tool, haplotype) in gfastats_data else '' for tool in contigging_data for haplotype in haplotypes if haplotype in contigging_data[tool]])

    # Add the gaps/gbp in between
    gap_length_index = display_names.index("Gaps bp")
    cont_table_data.insert(gap_length_index + 1, ['Gaps/Gbp'] + [format_number(gaps_per_gbp_data.get((tool, haplotype), '')) for tool in contigging_data for haplotype in haplotypes if haplotype in contigging_data[tool]])


    # get QV, Kmer completeness and BUSCO data   
    qv_data = {}
    completeness_data = {}
    busco_data = {metric: {} for metric in ['BUSCO sing.', 'BUSCO dupl.', 'BUSCO frag.', 'BUSCO miss.']}
    for tool, tool_properties in contigging_data.items():
        tool_elements = [element for element in tool_properties.keys() if element != 'version']
        for i, haplotype in enumerate(tool_elements):
            haplotype_properties = tool_properties[haplotype]
            if isinstance(haplotype_properties, dict):
                if 'merqury_folder' in haplotype_properties:
                    qv_data[(tool, haplotype)] = get_qv_value(haplotype_properties['merqury_folder'], i, tool, haplotype)
                    completeness_data[(tool, haplotype)] = get_completeness_value(haplotype_properties['merqury_folder'], i, tool, haplotype)
                if 'busco_short_summary_txt' in haplotype_properties:
                    s_value, d_value, f_value, m_value = extract_busco_values(haplotype_properties['busco_short_summary_txt'])
                    busco_data['BUSCO sing.'].update({(tool, haplotype): s_value})
                    busco_data['BUSCO dupl.'].update({(tool, haplotype): d_value})
                    busco_data['BUSCO frag.'].update({(tool, haplotype): f_value})
                    busco_data['BUSCO miss.'].update({(tool, haplotype): m_value})

    # Fill the table with the QV data
    cont_table_data.append(['QV'] + [qv_data.get((tool, haplotype), '') for tool in contigging_data for haplotype in haplotypes if haplotype in contigging_data[tool]])

    # Fill the table with the Kmer completeness data
    cont_table_data.append(['Kmer compl.'] + [completeness_data.get((tool, haplotype), '') for tool in contigging_data for haplotype in haplotypes if haplotype in contigging_data[tool]])

    # Fill the table with the BUSCO data
    for metric in ['BUSCO sing.', 'BUSCO dupl.', 'BUSCO frag.', 'BUSCO miss.']:
        cont_table_data.append([metric] + [busco_data[metric].get((tool, haplotype), '') for tool in contigging_data for haplotype in haplotypes if haplotype in contigging_data[tool]])


    # create table object and its atributes
    cont_table = Table(cont_table_data)

    cont_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), '#d3d3d3'),  # grey background for the header
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),       # center alignment
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),  # bold font for the header
        ('FONTSIZE', (0, 0), (-1, -1), 10),           # font size
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black)
    ]))


    # SECTION 4 ------------------------------------------------------------------------------------    

    scaffolding_data = yaml_data.get('SCAFFOLDING', {})

    # make a list from the assemblies available in scaffolding
    haplotypes = []
    for tool, tool_properties in scaffolding_data.items():
        for key in tool_properties.keys():
            if key != 'version' and key not in haplotypes:
                haplotypes.append(key)

    # get gfastats-based data
    gfastats_data = {}
    for tool, tool_properties in scaffolding_data.items():
        for haplotype, haplotype_properties in tool_properties.items():
            if isinstance(haplotype_properties, dict):
                if 'gfastats--nstar-report_txt' in haplotype_properties:
                    file_path = haplotype_properties['gfastats--nstar-report_txt']
                    with open(file_path, 'r') as file:
                        content = file.read()
                    gfastats_data[(tool, haplotype)] = extract_gfastats_values(content, keys)

    gaps_per_gbp_data = {}
    for (tool, haplotype), values in gfastats_data.items():
        try:
            gaps = float(values[gaps_index])
            total_length = float(values[total_length_index])
            gaps_per_gbp = round((gaps / total_length * 1_000_000_000), 2)
            gaps_per_gbp_data[(tool, haplotype)] = gaps_per_gbp
        except (ValueError, ZeroDivisionError):
            gaps_per_gbp_data[(tool, haplotype)] = ''


    # Define the table (column names) PLEASE DON'T MOVE THIS AGAIN!!!!!!!            
    scaf_table_data = [["Metrics"] + [f'{tool} \n {haplotype}' for tool in scaffolding_data for haplotype in haplotypes if haplotype in scaffolding_data[tool]]]

    # Fill the table with the gfastats data
    for i in range(len(display_names)):
        metric = display_names[i]
        scaf_table_data.append([metric] + [format_number(gfastats_data.get((tool, haplotype), [''])[i]) if (tool, haplotype) in gfastats_data else '' for tool in scaffolding_data for haplotype in haplotypes if haplotype in scaffolding_data[tool]])

    # Add the gaps/gbp in between
    gap_length_index = display_names.index("Gaps bp")
    scaf_table_data.insert(gap_length_index + 1, ['Gaps/Gbp'] + [format_number(gaps_per_gbp_data.get((tool, haplotype), '')) for tool in scaffolding_data for haplotype in haplotypes if haplotype in scaffolding_data[tool]])


    # get QV, Kmer completeness and BUSCO data   
    qv_data = {}
    completeness_data = {}
    busco_data = {metric: {} for metric in ['BUSCO sing.', 'BUSCO dupl.', 'BUSCO frag.', 'BUSCO miss.']}
    for tool, tool_properties in scaffolding_data.items():
        tool_elements = [element for element in tool_properties.keys() if element != 'version']
        for i, haplotype in enumerate(tool_elements):
            haplotype_properties = tool_properties[haplotype]
            if isinstance(haplotype_properties, dict):
                if 'merqury_folder' in haplotype_properties:
                    qv_data[(tool, haplotype)] = get_qv_value(haplotype_properties['merqury_folder'], i, tool, haplotype)
                    completeness_data[(tool, haplotype)] = get_completeness_value(haplotype_properties['merqury_folder'], i, tool, haplotype)
                if 'busco_short_summary_txt' in haplotype_properties:
                    s_value, d_value, f_value, m_value = extract_busco_values(haplotype_properties['busco_short_summary_txt'])
                    busco_data['BUSCO sing.'].update({(tool, haplotype): s_value})
                    busco_data['BUSCO dupl.'].update({(tool, haplotype): d_value})
                    busco_data['BUSCO frag.'].update({(tool, haplotype): f_value})
                    busco_data['BUSCO miss.'].update({(tool, haplotype): m_value})

    # Fill the table with the QV data
    scaf_table_data.append(['QV'] + [qv_data.get((tool, haplotype), '') for tool in scaffolding_data for haplotype in haplotypes if haplotype in scaffolding_data[tool]])

    # Fill the table with the Kmer completeness data
    scaf_table_data.append(['Kmer compl.'] + [completeness_data.get((tool, haplotype), '') for tool in scaffolding_data for haplotype in haplotypes if haplotype in scaffolding_data[tool]])

    # Fill the table with the BUSCO data
    for metric in ['BUSCO sing.', 'BUSCO dupl.', 'BUSCO frag.', 'BUSCO miss.']:
        scaf_table_data.append([metric] + [busco_data[metric].get((tool, haplotype), '') for tool in scaffolding_data for haplotype in haplotypes if haplotype in scaffolding_data[tool]])

    scaf_table = Table(scaf_table_data)

    scaf_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), '#d3d3d3'),  # grey background for the header
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),       # center alignment
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),  # bold font for the header
        ('FONTSIZE', (0, 0), (-1, -1), 10),           # font size
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black)
    ]))



    # Make "pipeline" table with all tools and versions 

    # Get k v for Pipeline section
    pipeline_table_data = [['Tool', 'Version']]  # headers

    for section, section_content in yaml_data.items():
        if isinstance(section_content, dict):
            for tool, tool_properties in section_content.items():
                if isinstance(tool_properties, dict):
                    version = tool_properties.get('version')
                    # replace empty values with 'NA'
                    if not version:
                        version = 'NA'
                    pipeline_table_data.append([tool, version])

    # create the table
    pipeline_table = Table(pipeline_table_data)
    pipeline_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), '#d3d3d3'),  # grey background for the header
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),       # center alignment
        ('FONTNAME', (0, 0), (-1, -1), 'Courier'),  # bold font for the header
        ('FONTSIZE', (0, 0), (-1, -1), 12),           # font size for the header
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black)
    ]))



    # SECTION 5 ------------------------------------------------------------------------------------

    ## Get data for contamination plot [THIS IS CURRENTLY NOT PART OF THE REPORT] 

    # Get the current date and time in CET timezone
    now_utc = datetime.now(pytz.utc)
    cet = pytz.timezone("CET")
    now_cet = now_utc.astimezone(cet)
    date_time_str = now_cet.strftime("%Y-%m-%d %H:%M:%S %Z")



    ###################################################################################################

    # Set up the PDF file
    pdf_filename = f"{tol_id}_EAR.pdf"
    pdf = SimpleDocTemplate(pdf_filename, pagesize=A4)
    elements = []

    # Set all the styles
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleStyle', fontName='Courier', fontSize=20))
    styles.add(ParagraphStyle(name='subTitleStyle', fontName='Courier', fontSize=16))
    styles.add(ParagraphStyle(name='normalStyle', fontName='Courier', fontSize=12))
    styles.add(ParagraphStyle(name='miniStyle', fontName='Courier', fontSize=8))
    styles.add(ParagraphStyle(name='FileNameStyle', fontName='Courier', fontSize=6))



    # Add the title ---------------------------------
    title = Paragraph("ERGA Assembly Report", styles['TitleStyle'])  # Apply the custom style to the Paragraph object
    elements.append(title)

    # Add a spacer
    elements.append(Spacer(1, 12))

    # Add version -----------------------------------
    ver_paragraph = Paragraph(EAR_version, styles['normalStyle'])  # Apply the custom style to the Paragraph object
    elements.append(ver_paragraph)

    # Add a spacer
    elements.append(Spacer(1, 12))

    # Add tags --------------------------------------
    tags_paragraph = Paragraph(f"Tags: {tags}", styles['normalStyle'])  # Apply the custom style to the Paragraph object
    elements.append(tags_paragraph)

    # Add a spacer
    elements.append(Spacer(1, 24))



    # Add species table
    elements.append(sp_data_table)

    # Add spacer
    elements.append(Spacer(1, 32))



    # Add data profile section subtitle -------------
    subtitle = Paragraph("Data profile", styles['TitleStyle'])  # Apply the custom style to the Paragraph object
    elements.append(subtitle)

    # Add a spacer
    elements.append(Spacer(1, 24))

    # Add data table
    elements.append(data_table)

    # Add spacer
    elements.append(Spacer(1, 32))



    # Add pipeline section subtitle -----------------
    subtitle = Paragraph("Pipeline summary", styles['TitleStyle'])  # Apply the custom style to the Paragraph object
    elements.append(subtitle)

    # Add a spacer
    elements.append(Spacer(1, 24))

    # Add pipeline table
    elements.append(pipeline_table)


    # Add page break
    elements.append(PageBreak())



    # -----------------------------------------------------------------------------

    # Add genome profiling section subtitle
    subtitle = Paragraph("Genome profiling", styles['TitleStyle'])  # Apply the custom style to the Paragraph object
    elements.append(subtitle)

    # Add spacer
    elements.append(Spacer(1, 24))



    # Add the profiling table
    elements.append(profiling_table)

    # Add a spacer before the summary table
    elements.append(Spacer(1, 12))



    # Add Genomescope images side by side
    lin_plot = Image(lin_plot_file, width=9 * cm, height=9 * cm)
    log_plot = Image(log_plot_file, width=9 * cm, height=9 * cm)
    image_table = Table([[lin_plot, log_plot]])
    image_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    elements.append(image_table)

    # Add a spacer
    elements.append(Spacer(1, 12))



    # Add Smudgeplot images side by side
    if smu_plot_file:
        smu_plot = Image(smu_plot_file, width=9 * cm, height=9 * cm)
        smulog_plot = Image(smulog_plot_file, width=9 * cm, height=9 * cm)
        image_table = Table([[smu_plot, smulog_plot]])
        image_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(image_table)
    else:
        smu_plot = None  # Or any other default value you want
        elements.append(Paragraph("Smudgeplot data not available", styles['miniStyle']))


    # Add page break
    elements.append(PageBreak())



    # -----------------------------------------------------------------------------

    # Add contigging section subtitle
    subtitle = Paragraph("Genome assembly: contigging", styles['TitleStyle'])
    elements.append(subtitle)

    # Add a spacer 
    elements.append(Spacer(1, 48))

    # Add contigging table
    elements.append(cont_table)

    # Add a spacer
    elements.append(Spacer(1, 5))

    # Store lineage information from each file in a list
    lineage_info_list = []
    for tool, tool_properties in contigging_data.items():
        for haplotype, haplotype_properties in tool_properties.items():
            if isinstance(haplotype_properties, dict):
                if 'busco_short_summary_txt' in haplotype_properties:
                    lineage_info = extract_busco_lineage(haplotype_properties['busco_short_summary_txt'])
                    if lineage_info:
                        lineage_info_list.append(lineage_info)


    # Check if all elements in the list are identical
    if lineage_info_list.count(lineage_info_list[0]) == len(lineage_info_list):
        lineage_name, num_genomes, num_buscos = lineage_info_list[0]
        elements.append(Paragraph(f"Lineage: {lineage_name} (genomes:{num_genomes}, BUSCOs:{num_buscos})", styles['miniStyle']))
    else:
        elements.append(Paragraph("Warning: BUSCO lineage datasets are not the same across results", styles['miniStyle']))


    # Add page break
    elements.append(PageBreak())


    # -------------------------------------
    # Initialize counter
    counter = 0

    # Add title and images for each step
    for idx, (tool, tool_properties) in enumerate(contigging_data.items(), 1):
        # Print tool title
        elements.append(Paragraph(f"K-mer spectra: {tool}", styles["normalStyle"]))

        tool_elements = [element for element in tool_properties.keys() if element != 'version']
        haplotype = tool_elements[0] if tool_elements else None

        if haplotype:
            haplotype_properties = tool_properties[haplotype]
            if isinstance(haplotype_properties, dict) and 'merqury_folder' in haplotype_properties:
                # Get images
                png_files = get_png_files(haplotype_properties['merqury_folder'])

                # Create image objects and add filename below each image
                images = [[Image(png_file, width=5.4 * cm, height=4.5 * cm), Paragraph(os.path.basename(png_file), styles["FileNameStyle"])] for png_file in png_files]

                # Arrange images into a 2x2 grid
                image_table = Table([[images[0], images[1]], [images[2], images[3]]])

                # Center images
                image_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))

                # Add image table to elements
                elements.append(image_table)

                # Increase counter by the number of PNGs added
                counter += len(png_files)

                # If counter is a multiple of 8, insert a page break and reset counter
                if counter % 8 == 0:
                    elements.append(PageBreak())
                    counter = 0

        # Add spacer
        elements.append(Spacer(1, 12))

    # If we have processed all tools and the last page does not contain exactly 8 images, insert a page break
    if counter % 8 != 0:
        elements.append(PageBreak())



    # -----------------------------------------------------------------------------

    # Add scaffolding section subtitle
    subtitle = Paragraph("Genome assembly: scaffolding", styles['TitleStyle'])
    elements.append(subtitle)

    # Add a spacer 
    elements.append(Spacer(1, 48))

    # Add scaffolding table
    elements.append(scaf_table)

    # Add a spacer
    elements.append(Spacer(1, 5))

    # Store lineage information from each file in a list
    lineage_info_list = []
    for tool, tool_properties in scaffolding_data.items():
        for haplotype, haplotype_properties in tool_properties.items():
            if isinstance(haplotype_properties, dict):
                if 'busco_short_summary_txt' in haplotype_properties:
                    lineage_info = extract_busco_lineage(haplotype_properties['busco_short_summary_txt'])
                    if lineage_info:
                        lineage_info_list.append(lineage_info)



    custom_style = ParagraphStyle(
        name='CustomStyle',
        parent=getSampleStyleSheet()['Normal'],
        fontName='Courier',
        fontSize=8,
    )

    # Check if all elements in the list are identical
    if lineage_info_list.count(lineage_info_list[0]) == len(lineage_info_list):
        lineage_name, num_genomes, num_buscos = lineage_info_list[0]
        elements.append(Paragraph(f"Lineage: {lineage_name} (genomes:{num_genomes}, BUSCOs:{num_buscos})", styles['miniStyle']))
    else:
        elements.append(Paragraph("Warning: BUSCO lineage datasets are not the same across results", styles['miniStyle']))


    # Add page break
    elements.append(PageBreak())



    # -------------------------------------
    # Initialize counter
    tool_count = 0

    # Add title and images for each step
    for idx, (tool, tool_properties) in enumerate(scaffolding_data.items(), 1):
        # Print tool title
        elements.append(Paragraph(f"Pretext Full Map: {tool}", styles["normalStyle"]))

        tool_elements = [element for element in tool_properties.keys() if element != 'version']

        row_images = []
        row_names = []
        for haplotype in tool_elements:
            haplotype_properties = tool_properties[haplotype]
            if isinstance(haplotype_properties, dict) and 'pretext_FullMap_png' in haplotype_properties:
                # Get image path
                png_file = haplotype_properties['pretext_FullMap_png']

                # If png_file is not empty, display it
                if png_file:
                    # Create image object and add filename below the image
                    img = Image(png_file, width=9 * cm, height=9 * cm)
                    p = Paragraph(os.path.basename(png_file), styles["FileNameStyle"])

                    # Add image to the current row
                    row_images.append(img)
                    # Add name to the row_names
                    row_names.append(p)

        # If we have images, add them to the elements as a table
        if row_images:
            table_data = [row_images, row_names]  # First row for images, second row for names
            table = Table(table_data)
            elements.append(table)
        else:
            # If there are no images, print "Data not available"
            elements.append(Paragraph("Data not available", styles['miniStyle']))

        # Add spacer
        elements.append(Spacer(1, 12))

        tool_count += 1
        # Add a page break after every two tools, or if it's the last tool
        if tool_count % 2 == 0 or idx == len(scaffolding_data):
            elements.append(PageBreak())



    # -----------------------------------------------------------------------------

    # Add submitter, affiliation
    submitter_paragraph_style = ParagraphStyle(name='SubmitterStyle', fontName='Courier', fontSize=10)
    elements.append(Paragraph(f"Submitter: {submitter}", submitter_paragraph_style))
    elements.append(Paragraph(f"Affiliation: {affiliation}", submitter_paragraph_style))

    # Add a spacer before the date and time
    elements.append(Spacer(1, 8))

    # Add the date and time (CET) of the document creation
    cet = pytz.timezone("CET")
    current_datetime = datetime.now(cet)
    formatted_datetime = current_datetime.strftime("%Y-%m-%d %H:%M:%S %Z")
    elements.append(Paragraph(f"Date and time: {formatted_datetime}", submitter_paragraph_style))

    # Build the PDF
    pdf.build(elements)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Create an ERGA Assembly Report (EAR) from a YAML file. Visit https://github.com/ERGA-consortium/EARs for more information')
    parser.add_argument('yaml_file', type=str, help='Path to the YAML file')
    args = parser.parse_args()
    
    make_report(args.yaml_file)
