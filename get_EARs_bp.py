# get_EARs_bp.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee

# This script is for running on a local cloned repo!
# The aim of it is to get the total number of base pairs of approved assemblies
# It uses the same EAR_env conda environment than the make_EAR.py script


import glob
import yaml
import os
import argparse
from collections import defaultdict
import sys
import subprocess

def update_repository():
    """Updates the git repository to the latest version"""
    try:
        # Stash any local changes
        subprocess.run(['git', 'stash'], check=True, capture_output=True)
        
        # Pull the latest changes
        result = subprocess.run(['git', 'pull'], check=True, capture_output=True, text=True)
        
        # Pop the stashed changes
        subprocess.run(['git', 'stash', 'pop'], capture_output=True)
        
        print("Repository updated successfully")
        if "Already up to date" not in result.stdout:
            print("New changes were pulled from remote")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error updating repository: {e}")
        print("Proceeding with existing files...")
        return False
    except Exception as e:
        print(f"Unexpected error while updating repository: {e}")
        print("Proceeding with existing files...")
        return False

def parse_size(size_str):
    """Convert size string to integer, removing commas"""
    return int(size_str.replace(',', ''))

def process_all_tags():
    pattern = os.path.join("Assembly_Reports", "*", "*", "*.yaml")
    yaml_files = glob.glob(pattern)
    
    # Process all files and collect unique tags
    all_tags = set()
    all_files = []
    
    for yaml_file in yaml_files:
        try:
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
                
                if (data and 
                    'Tags' in data and
                    'Genome Traits' in data and 
                    'Observed' in data['Genome Traits'] and 
                    'Haploid size (bp)' in data['Genome Traits']['Observed']):
                    
                    tags = data['Tags']
                    if isinstance(tags, str):
                        tags = [tags]
                    
                    # Check if file has any ERGA tag
                    has_erga = False
                    for tag in tags:
                        if tag.startswith('ERGA'):
                            has_erga = True
                            all_tags.add(tag)
                    
                    if has_erga:
                        size = parse_size(data['Genome Traits']['Observed']['Haploid size (bp)'])
                        all_files.append({
                            'species': data.get('Species', 'Unknown'),
                            'tolid': data.get('ToLID', 'Unknown'),
                            'size': size
                        })
                            
        except Exception as e:
            continue

    # Print all found ERGA tags
    for tag in sorted(all_tags):
        print(f"EAR tag: {tag}")
    
    # Print combined summary
    total_size = sum(f['size'] for f in all_files)
    print("\nSummary:")
    print(f"Total EARs processed: {len(all_files)}")
    print(f"Total observed haploid size: {total_size:,} bp")
    
    # Print detailed results
    if all_files:
        print("\nDetailed Results:")
        print("Species".ljust(30), "ToLID".ljust(15), "Observed Size (bp)")
        print("-" * 65)
        for file in sorted(all_files, key=lambda x: x['species']):
            print(f"{file['species'][:30].ljust(30)} {file['tolid'].ljust(15)} {file['size']:,}")

def process_single_tag(tag):
    pattern = os.path.join("Assembly_Reports", "*", "*", "*.yaml")
    yaml_files = glob.glob(pattern)
    
    total_size = 0
    processed_files = []
    
    for yaml_file in yaml_files:
        try:
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
                
                if (data and 
                    'Tags' in data and 
                    tag in data['Tags'] and
                    'Genome Traits' in data and 
                    'Observed' in data['Genome Traits'] and 
                    'Haploid size (bp)' in data['Genome Traits']['Observed']):
                    
                    size = parse_size(data['Genome Traits']['Observed']['Haploid size (bp)'])
                    total_size += size
                    processed_files.append({
                        'species': data.get('Species', 'Unknown'),
                        'tolid': data.get('ToLID', 'Unknown'),
                        'size': size
                    })
                
        except Exception as e:
            continue

    print(f"EAR tag: {tag}")
    print("\nSummary:")
    print(f"Total EARs processed: {len(processed_files)}")
    print(f"Total observed haploid size: {total_size:,} bp")
    
    if processed_files:
        print("\nDetailed Results:")
        print("Species".ljust(30), "ToLID".ljust(15), "Observed Size (bp)")
        print("-" * 65)
        for file in sorted(processed_files, key=lambda x: x['species']):
            print(f"{file['species'][:30].ljust(30)} {file['tolid'].ljust(15)} {file['size']:,}")

def print_quick_help():
    print("Usage: python get_EARs_bp.py [--tag TAG | --all-tags | -h]")
    print("\nUse -h or --help for detailed help")


def main():
    parser = argparse.ArgumentParser(
        description='Process YAML EAR files to get genome size information.',
        epilog='Example usage:\n'
               '  python get_EARs_bp.py --tag ERGA-BGE\n'
               '  python get_EARs_bp.py --all-tags\n'
               '  python get_EARs_bp.py --all-tags --update-repo',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--tag', type=str, help='Project tag to filter YAML files, e.g.: ERGA-BGE')
    group.add_argument('--all-tags', action='store_true', help='Process and show results for all ERGA tags')
    parser.add_argument('--update-repo', action='store_true', help='Update the repository before processing files')
    
    # If no arguments provided, print quick help and exit
    if len(sys.argv) == 1:
        print_quick_help()
        sys.exit(0)
    
    args = parser.parse_args()
    
    # Update repository if requested
    if args.update_repo:
        update_repository()
    
    if args.all_tags:
        process_all_tags()
    elif args.tag:
        process_single_tag(args.tag)

if __name__ == "__main__":
    main()
