# get_EAR_reviewer.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee
version = "v24.04.12_beta"

import requests
import random
from datetime import datetime
import argparse

def download_tsv(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Failed to download data from the repo: {e}")
        return None

def parse_tsv(tsv_str):
    lines = tsv_str.strip().split('\n')
    headers = lines[0].split(',')
    data = [dict(zip(headers, line.split(','))) for line in lines[1:]]
    return data

def adjust_score(reviewer):
    score = int(reviewer['Calling Score'])
    if reviewer['Last Review'] == 'NA':
        score += 50
    return score

def parse_date(date_str):
    if date_str == 'NA':
        return datetime.max  # Using max date for 'NA' to ensure that are not favored in date comparison
    return datetime.strptime(date_str, '%Y-%m-%d')

def select_best_reviewer(data, calling_institution):
    candidates = [
        {
            **reviewer,
            'Adjusted Score': adjust_score(reviewer),
            'Parsed Last Review': parse_date(reviewer['Last Review'])
        } for reviewer in data
        if reviewer['Institution'] != calling_institution and reviewer['Active'] == 'Y' and reviewer['Busy'] == 'N'
    ]

    if not candidates:
        return None, "No candidates were eligible based on the criteria at the moment."

    candidates.sort(
        key=lambda x: (-x['Adjusted Score'], x['Parsed Last Review'], int(x['Total Reviews']))
    )

    top_candidates = [candidates[0]]
    for candidate in candidates[1:]:
        if candidate['Adjusted Score'] == top_candidates[0]['Adjusted Score']:
            top_candidates.append(candidate)
        else:
            break

    if len(top_candidates) == 1:
        return top_candidates[0], None  # No tie to mention

    # Tie? Break by Last Review
    top_candidates.sort(key=lambda x: x['Parsed Last Review'])
    if top_candidates[0]['Parsed Last Review'] != top_candidates[1]['Parsed Last Review']:
        return top_candidates[0], "Latest review older than the others."

    # Tie? Break by Total Reviews
    top_candidates.sort(key=lambda x: int(x['Total Reviews']))
    if int(top_candidates[0]['Total Reviews']) != int(top_candidates[1]['Total Reviews']):
        return top_candidates[0], "Less reviews than the others."

    # Tie? Random selection among tied candidates
    chosen = random.choice(top_candidates)
    return chosen, "Randomly chosen to break a tie among the finalists."


def print_tsv(data):
    if not data:
        print("No data to print.")
        return
    headers = ['Github ID', 'Full Name', 'Institution', 'Total Reviews', 'Last Review', 'Active', 'Busy', 'Calling Score', 'Adjusted Score']
    
    # Set max width for each column
    col_widths = {header: len(header) for header in headers}
    for header in headers:
        if len(str(data[header])) > col_widths[header]:
            col_widths[header] = max(col_widths[header], len(str(data[header])))

    # set header
    header_row = ' | '.join(header.ljust(col_widths[header]) for header in headers)
    print(header_row)
    print('-' * len(header_row))  # Print a separator line

    # create data rows
    data_row = ' | '.join(str(data[header]).ljust(col_widths[header]) for header in headers)
    print(data_row)


def explain_selection(reviewer, reason):
    explanation = f"\nSelected reviewer: {reviewer['Full Name']} ({reviewer['Github ID']})\n"
    explanation += "The decision was based on:\n"
    explanation += f"- different institution ('{reviewer['Institution']}')\n"
    explanation += f"- active ('{reviewer['Active']}')\n"
    explanation += f"- not busy ('{reviewer['Busy']}')\n"
    explanation += f"- highest adjusted calling score in this particular selection ({reviewer['Adjusted Score']})"
    if reason:
        explanation += f"\n- {reason}"
    return explanation

def main():
    parser = argparse.ArgumentParser(description="Select a candidate for EAR reviewing.")
    parser.add_argument("-i", "--institution", help="Institution of the person requesting the review.", required=False)
    parser.add_argument("-v", "--version", action="version", version=f'{version}', help="Show script's version and exit.")
    args = parser.parse_args()
    
    if not args.institution:
        parser.print_help()
    else:
        url = "https://raw.githubusercontent.com/ERGA-consortium/EARs/main/rev/reviewers_list.csv"
        tsv_data = download_tsv(url)
        if tsv_data:
            data = parse_tsv(tsv_data)
            selected_reviewer, reason = select_best_reviewer(data, args.institution)
            print("*****")
            print("EAR Reviewer Selection Process")
            print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            if selected_reviewer:
                print_tsv(selected_reviewer)
                print(explain_selection(selected_reviewer, reason))
            else:
                print("No suitable reviewer found at the moment.")
        else:
            print("Failed to process reviewers data.")

if __name__ == "__main__":
    main()

