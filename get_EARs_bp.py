import glob
import yaml
import os
import argparse
from collections import defaultdict
import sys
import subprocess

def update_repository():
    try:
        subprocess.run(['git', 'stash'], check=True, capture_output=True)
        result = subprocess.run(['git', 'pull'], check=True, capture_output=True, text=True)
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
    return int(size_str.replace(',', ''))

def get_column_widths(files):
    widths = {
        'species': max(30, max(len(f['species']) for f in files)),
        'tolid': max(15, max(len(f['tolid']) for f in files)),
        'txid': max(10, max(len(str(f['txid'])) for f in files)),
        'class': max(15, max(len(f['class']) for f in files)),
        'order': max(20, max(len(str(f['order'])) for f in files))
    }
    return widths

def get_stats_by_field(files, field):
    stats = defaultdict(lambda: {'count': 0, 'size': 0})
    for file in files:
        key = file[field]
        if key == 'NA' or key == 'Unknown':
            continue
        stats[key]['count'] += 1
        stats[key]['size'] += file['size']
    return stats

def display_stats(stats, field_name):
    print(f"\n{field_name} Statistics:")
    print(f"{field_name}".ljust(20), "Species".ljust(10), "Total Haploid Size")
    print("-" * 50)
    for key, data in sorted(stats.items()):
        print(f"{str(key)[:19].ljust(20)} {str(data['count']).ljust(10)} {data['size']:,}")

def display_results(files, show_full=False):
    if not files:
        return

    widths = get_column_widths(files)
    print("\nDetailed Results:")
    
    if show_full:
        header = [
            "Species".ljust(widths['species']), 
            "ToLID".ljust(widths['tolid']), 
            "TxID".ljust(widths['txid']),
            "Class".ljust(widths['class']), 
            "Order".ljust(widths['order']), 
            "Observed Size (bp)"
        ]
        print(*header)
        separator_length = sum(widths.values()) + 20  # Extra space for the size column
        print("-" * separator_length)
        
        for file in sorted(files, key=lambda x: x['species']):
            print(f"{file['species'][:widths['species']].ljust(widths['species'])} "
                  f"{file['tolid'].ljust(widths['tolid'])} "
                  f"{str(file['txid']).ljust(widths['txid'])} "
                  f"{file['class'].ljust(widths['class'])} "
                  f"{str(file['order']).ljust(widths['order'])} "
                  f"{file['size']:,}")
    else:
        print("Species".ljust(30), "ToLID".ljust(15), "Observed Size (bp)")
        print("-" * 65)
        for file in sorted(files, key=lambda x: x['species']):
            print(f"{file['species'][:30].ljust(30)} {file['tolid'].ljust(15)} {file['size']:,}")

def process_all_tags(show_full=False, stats_class=False, stats_order=False):
    pattern = os.path.join("Assembly_Reports", "*", "*", "*.yaml")
    yaml_files = glob.glob(pattern)
    
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
                    
                    has_erga = False
                    for tag in tags:
                        if tag.startswith('ERGA'):
                            has_erga = True
                            all_tags.add(tag)
                    
                    if has_erga:
                        size = parse_size(data['Genome Traits']['Observed']['Haploid size (bp)'])
                        file_info = {
                            'species': data.get('Species', 'Unknown'),
                            'tolid': data.get('ToLID', 'Unknown'),
                            'size': size,
                            'txid': data.get('TxID', 'Unknown'),
                            'class': data.get('Class', 'Unknown'),
                            'order': data.get('Order', 'Unknown')
                        }
                        all_files.append(file_info)
                            
        except Exception as e:
            continue

    for tag in sorted(all_tags):
        print(f"EAR tag: {tag}")
    
    total_size = sum(f['size'] for f in all_files)
    print("\nSummary:")
    print(f"Total EARs processed: {len(all_files)}")
    print(f"Total observed haploid size: {total_size:,} bp")
    
    if stats_class:
        stats = get_stats_by_field(all_files, 'class')
        display_stats(stats, 'Class')
    elif stats_order:
        stats = get_stats_by_field(all_files, 'order')
        display_stats(stats, 'Order')
    elif show_full:
        display_results(all_files, show_full)
    else:
        display_results(all_files, show_full)

def process_single_tag(tag, show_full=False, stats_class=False, stats_order=False):
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
                    file_info = {
                        'species': data.get('Species', 'Unknown'),
                        'tolid': data.get('ToLID', 'Unknown'),
                        'size': size,
                        'txid': data.get('TxID', 'Unknown'),
                        'class': data.get('Class', 'Unknown'),
                        'order': data.get('Order', 'Unknown')
                    }
                    processed_files.append(file_info)
                
        except Exception as e:
            continue

    print(f"EAR tag: {tag}")
    print("\nSummary:")
    print(f"Total EARs processed: {len(processed_files)}")
    print(f"Total observed haploid size: {total_size:,} bp")
    
    if stats_class:
        stats = get_stats_by_field(processed_files, 'class')
        display_stats(stats, 'Class')
    elif stats_order:
        stats = get_stats_by_field(processed_files, 'order')
        display_stats(stats, 'Order')
    elif show_full:
        display_results(processed_files, show_full)
    else:
        display_results(processed_files, show_full)

def print_quick_help():
    print("Usage: python get_EARs_bp.py [--tag TAG | --all-tags | -h] [--full] [--stats-class | --stats-order]")
    print("\nUse -h or --help for detailed help")

def main():
    parser = argparse.ArgumentParser(
        description='Process YAML EAR files to get genome size information.',
        epilog='Example usage:\n'
               '  python get_EARs_bp.py --tag ERGA-BGE\n'
               '  python get_EARs_bp.py --tag ERGA-BGE --full\n'
               '  python get_EARs_bp.py --tag ERGA-BGE --stats-class\n'
               '  python get_EARs_bp.py --tag ERGA-BGE --stats-order\n'
               '  python get_EARs_bp.py --all-tags\n'
               '  python get_EARs_bp.py --all-tags --update-repo',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--tag', type=str, help='Project tag to filter YAML files, e.g.: ERGA-BGE')
    group.add_argument('--all-tags', action='store_true', help='Process and show results for all ERGA tags')
    parser.add_argument('--update-repo', action='store_true', help='Update the repository before processing files')
    parser.add_argument('--full', action='store_true', help='Show additional columns (TxID, Class, Order)')
    parser.add_argument('--stats-class', action='store_true', help='Show statistics grouped by Class')
    parser.add_argument('--stats-order', action='store_true', help='Show statistics grouped by Order')
    
    if len(sys.argv) == 1:
        print_quick_help()
        sys.exit(0)
    
    args = parser.parse_args()
    
    if args.update_repo:
        update_repository()
    
    if args.all_tags:
        process_all_tags(args.full, args.stats_class, args.stats_order)
    elif args.tag:
        process_single_tag(args.tag, args.full, args.stats_class, args.stats_order)

if __name__ == "__main__":
    main()
