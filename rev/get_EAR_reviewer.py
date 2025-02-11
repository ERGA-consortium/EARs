# get_EAR_reviewer.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee
version = "v25.02.11"

import requests
import random
from datetime import datetime
import argparse

def download_csv(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Failed to download data from the repo: {e}")
        return None

def parse_csv(csv_str):
    lines = csv_str.strip().split('\n')
    headers = lines[0].split(',')
    data = [dict(zip(headers, line.split(','))) for line in lines[1:]]
    return data

def normalize_institution(institution):
    institution = institution.lower()
    if 'cnag' in institution:
        return 'CNAG'
    elif any(name in institution for name in ['sanger', 'welcome sanger institute', 'wsi']):
        return 'Sanger'
    elif 'genoscope' in institution:
        return 'Genoscope'
    elif 'scilifelab' in institution:
        return 'SciLifeLab'
    return institution

def adjust_score(reviewer, tags):
    score = int(reviewer['Calling Score'])
    if reviewer['Last Review'] == 'NA':
        score += 50
    normalized_institution = normalize_institution(reviewer['Institution'])
    if 'ERGA-BGE' in tags and normalized_institution in ['CNAG', 'Sanger', 'Genoscope', 'SciLifeLab']:
        score += 50  # Additional 50 points for reviewers from BGE institutions if 'ERGA-BGE' tag is used
    if reviewer['Supervisor'] == 'Y':
        score -= 5  # Subtract 5 points if reviewer is also supervisor to decrease the chance of selection
    return score

def parse_date(date_str):
    if date_str == 'NA':
        return datetime.max
    return datetime.strptime(date_str, '%Y-%m-%d')

def print_csv(data_list, selected_reviewer):
    if not data_list:
        print("No data to print.")
        return
    headers = ['Github ID', 'Full Name', 'Institution', 'Total Reviews', 'Last Review', 'Active', 'Busy', 'Calling Score', 'Adjusted Score']
    col_widths = {header: max(len(header), max((len(str(row[header])) for row in data_list), default=len(header))) for header in headers}
    header_row = ' | '.join(header.ljust(col_widths[header]) for header in headers)
    print(header_row)
    print('-' * len(header_row))
    
    if selected_reviewer:
        data_list = [selected_reviewer] + [d for d in data_list if d != selected_reviewer]
    
    for data in data_list:
        data_row = ' | '.join(str(data[header]).ljust(col_widths[header]) for header in headers)
        print(data_row)

def select_best_reviewer(data, calling_institution, use_bge):
    eligible_candidates = [
        {
            **reviewer,
            'Adjusted Score': adjust_score(reviewer, use_bge),
            'Parsed Last Review': parse_date(reviewer['Last Review']),
            'Total Reviews': int(reviewer['Total Reviews'])
        } for reviewer in data
        if reviewer['Active'] == 'Y' and reviewer['Busy'] == 'N' and normalize_institution(reviewer['Institution']) != normalize_institution(calling_institution)
    ]

    eligible_candidates.sort(key=lambda x: (-x['Adjusted Score'], x['Total Reviews'], x['Parsed Last Review']))
    
    top_score = eligible_candidates[0]['Adjusted Score'] if eligible_candidates else None
    top_candidates = [c for c in eligible_candidates if c['Adjusted Score'] == top_score] if top_score is not None else []

    if len(top_candidates) == 1:
        return eligible_candidates, top_candidates, "highest adjusted calling score in this particular selection"

    # Check if there is a tie on 'Parsed Last Review' and 'Total Reviews'
    oldest_review = top_candidates[0]['Parsed Last Review']
    fewest_reviews = top_candidates[0]['Total Reviews']
    final_candidates = [c for c in top_candidates if c['Parsed Last Review'] == oldest_review and c['Total Reviews'] == fewest_reviews]

    if len(final_candidates) == 1:
        return eligible_candidates, final_candidates, "oldest review and fewest reviews among the finalists"

    # If still tied, randomly select one
    selected = random.choice(final_candidates)
    return eligible_candidates, [selected], "random selection to break a tie among the finalists"

def select_random_supervisor(data, exclude_id, calling_institution):
    supervisors = [
        reviewer for reviewer in data
        if reviewer['Supervisor'] == 'Y' 
           and reviewer['Github ID'] != exclude_id 
           and normalize_institution(reviewer['Institution']) != normalize_institution(calling_institution)
    ]

    if not supervisors:
        return None

    return random.choice(supervisors)

def main():
    parser = argparse.ArgumentParser(description="Select a candidate for EAR reviewing. Also can select a supervisor.")
    parser.add_argument("-i", "--institution", required=True, help="Institution of the person requesting the review.")
    parser.add_argument("-t", "--tag", nargs='*', default=[], help="Specify one or more tags to influence selection criteria, such as 'ERGA-BGE' to favor reviewers from the BGE project.")
    parser.add_argument("-u", "--user", help="Github user ID of the person requesting the review. Only required for selecting a supervisor.")
    parser.add_argument("-s", "--supervisor", action="store_true", help="Flag to select a supervisor randomly.")
    parser.add_argument("-v", "--version", action="version", version=version, help="Show script's version and exit.")
    args = parser.parse_args()

    url = "https://raw.githubusercontent.com/ERGA-consortium/EARs/main/rev/reviewers_list.csv"
    csv_data = download_csv(url)
    if csv_data:
        data = parse_csv(csv_data)
        if not data:
            print("No data available from the source.")
            return

        if args.supervisor:
            if not args.user:
                print("Github user ID must be provided with --user when using --supervisor.")
                return
            selected_supervisor = select_random_supervisor(data, args.user, args.institution)
            if selected_supervisor:
                print(f"Selected supervisor: {selected_supervisor['Full Name']} ({selected_supervisor['Github ID']})")
            else:
                print("No eligible supervisors found.")
            return

        all_eligible_candidates, top_candidates, selection_reason = select_best_reviewer(data, args.institution, args.tag)
        print("*****")
        print("EAR Reviewer Selection Process")
        print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        
        if all_eligible_candidates:
            print("All Eligible Candidates:\n")
            selected_reviewer = top_candidates[0] if top_candidates else None
            print_csv(all_eligible_candidates, selected_reviewer)

            if top_candidates:
                print(f"\nSelected reviewer: {selected_reviewer['Full Name']} ({selected_reviewer['Github ID']})")
                print("The decision was based on:")
                print(f"- different institution ('{selected_reviewer['Institution']}')")
                print(f"- active ('{selected_reviewer['Active']}')")
                print(f"- not busy ('{selected_reviewer['Busy']}')")
                print(f"- {selection_reason} ({selected_reviewer['Adjusted Score']})")
            else:
                print("No suitable reviewer found at the moment.")
        else:
            print("No eligible candidates found.")
    else:
        print("Failed to process reviewers data.")

if __name__ == "__main__":
    main()
