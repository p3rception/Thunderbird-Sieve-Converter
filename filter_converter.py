import re
import sys
from typing import List, Dict, Tuple

def read_thunderbird_filters(file_path: str) -> str:
    """Read the contents of a Thunderbird filter file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return file.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Error: File '{file_path}' not found.")
    except IOError as e:
        raise IOError(f"Error reading file '{file_path}': {e}")

def parse_thunderbird_filters(thunderbird_filters: str) -> List[Dict[str, str]]:
    """Parse Thunderbird filters into a list of dictionaries."""
    filters = []
    current_filter = {}
    for line in thunderbird_filters.split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('name='):
            if current_filter and 'name' in current_filter and 'condition' in current_filter:
                filters.append(current_filter)
            current_filter = {}
        if '=' in line:
            key, value = line.split('=', 1)
            value = value.strip('"')
            if key == 'action':
                if 'actions' not in current_filter:
                    current_filter['actions'] = []
                current_filter['actions'].append(value)
            elif key == 'name':
                current_filter['name'] = value
            else:
                current_filter[key] = value

    # Check that the current filter has a name and condition before appending
    if current_filter and 'name' in current_filter and 'condition' in current_filter:
        filters.append(current_filter)
        
    return filters


def clean_header(header: str) -> str:
    """Remove unnecessary escaping from the header."""
    # Only unescape inner quotes if they are double-escaped
    return header.replace('\\"', '"').strip('"')

def convert_condition(_condition: str) -> Tuple[str, List[str]]:
    """Convert a Thunderbird condition to Sieve format."""

    condition = _condition.strip()
    if condition.startswith('OR '):
        operator = 'anyof'
        condition = condition[3:]
    elif condition.startswith('AND '):
        operator = 'allof'
        condition = condition[4:]
    else:
        operator = 'anyof'

    # Regex to match the parts, including escaped quotes
    parts = re.findall(r'\(([^,]+),\s*([^,]+),\s*(.+?)\)', condition)

    sieve_conditions = []
    for header, operation, value in parts:
        # Clean header and value
        header = clean_header(header)
        value = clean_header(value)

        simple_headers =["from","to","cc","subject"]
        print(f"    ℹ️️ header operation value: h'{header}' o'{operation}' v'{value}'")
        if operation == 'is':
            if header in simple_headers:
                sieve_conditions.append(f'header :is "{header.capitalize()}" "{value}"')
        elif operation =='contains':
            if header in simple_headers:
                sieve_conditions.append(f'header :contains "{header.capitalize()}" "{value}"')
            else:
                if ' or ' in header:
                    or_args = header.split(' or ')
                    capitalized = [w.capitalize() for w in or_args]
                    header_list = '["' + '","'.join(capitalized)+'"]'
                    sieve_conditions.append(f'header :contains {header_list} "{value}"')
                else:
                    sieve_conditions.append(f'header :contains "{header}" "{value}"')
        elif operation =='begins with':
            if header in simple_headers:
                sieve_conditions.append(f'header :matches "{header.capitalize()}" "{value}*"')
        elif operation =='ends with':
            if header in simple_headers:
                sieve_conditions.append(f'header :contains "{header.capitalize()}" "*{value}"')
        else:
            print(f"    ☢️ unhandled header/operation: h'{header}' o'{operation}'")

    if len(sieve_conditions) == 0:
        print(f"☢️ unhandled condition: {_condition}")

    return operator, sieve_conditions


def convert_to_sieve(thunderbird_filter: Dict[str, str]) -> str:
    """Convert a single Thunderbird filter to a Sieve rule."""
    name = thunderbird_filter.get('name', 'Unnamed Filter')
    condition = thunderbird_filter.get('condition', '')
    actions = []
    hint=''

    thunder_actions = thunderbird_filter['actions'] if 'actions' in thunderbird_filter else []
    thunder_value = thunderbird_filter['actionValue'] if 'actionValue' in thunderbird_filter else None

    if 'actions' in thunderbird_filter:
        if "Mark read" in thunder_actions:
            actions.append('\tsetflag "\\\\Seen";')
        if "Mark flagged" in thunder_actions:
            actions.append('\tsetflag "\\\\Flagged";')
        if "Stop execution" in thunder_actions:
            actions.append('\tstop;')
        if "Move to folder" in thunder_actions:
            full_path = thunder_value
            folder_match = re.search(r'inbox/(.+)$', full_path.lower())
            if folder_match:
                folder = folder_match.group(1)
                actions.append(f'\tfileinto "INBOX.{folder.replace('.','-').replace('/','.')}";')
            elif '/Trash/' in full_path:
                hint = f"rule deactivated because target is Trash '{full_path}'"


    operator, sieve_conditions = convert_condition(condition)

    if len(sieve_conditions) == 0 :
        print(f"⚠️ see rule '{name}' (unhandled condition)")
        sieve_rule = f"# WARNING: condition not convertable\n"
        sieve_rule += f"# {condition}\n"
        sieve_rule += f"# rule:[{name}]\n"

        sieve_rule += f"#if {operator} (\n#    "
        sieve_rule += "#,\n#    ".join(sieve_conditions)
        sieve_rule += "\n#)\n#{\n#" + "\n#".join(actions) + "\n#}"
    elif len(actions)==0:
        print(f"⚠️ see rule '{name}' (missing action)")
        sieve_rule = f"# WARNING: rule has no action\n"
        sieve_rule += f"# rule:[{name}]\n"
        if hint:
            sieve_rule += f"# hint {hint}\n"
        sieve_rule += f"# conditions {condition}\n"
        sieve_rule += f"# actions {thunder_actions}\n"
        sieve_rule += f"# values {thunder_value}\n"
    else:
        sieve_rule = f"# rule:[{name}]\n"
        sieve_rule += f"if {operator} (\n    "
        sieve_rule += ",\n    ".join(sieve_conditions)
        sieve_rule += "\n)\n{\n" + "\n".join(actions) + "\n}"

    return sieve_rule

def thunderbird_to_sieve(thunderbird_filters: str) -> str:
    """Convert Thunderbird filters to Sieve rules."""
    filters = parse_thunderbird_filters(thunderbird_filters)
    sieve_rules = []
    for filter in filters:
        rule = convert_to_sieve(filter)
        sieve_rules.append(rule)
    return 'require ["fileinto", "imap4flags"];\n\n' + "\n\n".join(sieve_rules)

def main():
    """Main function to handle command-line arguments and file operations."""
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python script.py path/to/msgFilterRules.dat [output_file.sieve]")
        sys.exit(1)

    input_file_path = sys.argv[1]
    output_file_path = sys.argv[2] if len(sys.argv) == 3 else "roundcube.sieve"

    try:
        thunderbird_filters = read_thunderbird_filters(input_file_path)
        sieve_rules = thunderbird_to_sieve(thunderbird_filters)

        with open(output_file_path, 'w', encoding='utf-8') as f:
            f.write(sieve_rules)

        print(f"Sieve rules have been successfully written to {output_file_path}")
        print(f"Total rules converted: {sieve_rules.count('# rule:')}")
    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
