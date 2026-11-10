"""Convert Thunderbird message filters (msgFilterRules.dat) to a Sieve script.

The parsing follows Thunderbird's own reader (nsMsgFilterList::LoadValue,
nsMsgFilterList::ParseCondition, nsMsgSearchTerm::DeStreamNew), so quoted
values, escaped quotes and custom headers come through unchanged.
"""
import re
import sys
from urllib.parse import unquote, urlsplit

ADDRESS_HEADERS = {
    'from': ['from'],
    'to': ['to'],
    'cc': ['cc'],
    'to or cc': ['to', 'cc'],
    'all addresses': ['from', 'to', 'cc', 'bcc'],
}

# Thunderbird operator -> (negate, Sieve match type, key builder)
OPERATORS = {
    'contains': (False, ':contains', lambda v: v),
    "doesn't contain": (True, ':contains', lambda v: v),
    'is': (False, ':is', lambda v: v),
    "isn't": (True, ':is', lambda v: v),
    'begins with': (False, ':matches', lambda v: escape_wildcards(v) + '*'),
    'ends with': (False, ':matches', lambda v: '*' + escape_wildcards(v)),
    'is empty': (True, ':matches', lambda v: '?*'),
    "isn't empty": (False, ':matches', lambda v: '?*'),
}

FLAG_ACTIONS = {
    'Mark read': 'addflag "\\\\Seen";',
    'Mark unread': 'removeflag "\\\\Seen";',
    'Mark flagged': 'addflag "\\\\Flagged";',
}

REQUIRES = {
    'Move to folder': ['fileinto'],
    'Copy to folder': ['fileinto', 'copy'],
    'Delete': ['fileinto'],
    'Forward': ['copy'],
    'AddTag': ['imap4flags'],
    'Mark read': ['imap4flags'],
    'Mark unread': ['imap4flags'],
    'Mark flagged': ['imap4flags'],
}


class Unsupported(Exception):
    pass


def quote(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def quote_list(items) -> str:
    return quote(items[0]) if len(items) == 1 else '[' + ', '.join(map(quote, items)) + ']'


def escape_wildcards(s: str) -> str:
    return re.sub(r'([*?\\])', r'\\\1', s)


def parse_filters(text: str) -> list:
    """Return filters as dicts with name, enabled, actions [(action, value)] and condition."""
    filters = []
    for line in text.splitlines():
        m = re.fullmatch(r'\s*(\w+)="(.*)"\s*', line)
        if not m:
            continue
        key, value = m.group(1), re.sub(r'\\([\\"])', r'\1', m.group(2))
        if key == 'name':
            filters.append({'name': value, 'enabled': True, 'actions': [], 'condition': ''})
        elif not filters:
            continue  # version= and logging= come before the first filter
        elif key == 'enabled':
            filters[-1]['enabled'] = value == 'yes'
        elif key == 'action':
            filters[-1]['actions'].append([value, ''])
        elif key == 'actionValue' and filters[-1]['actions']:
            filters[-1]['actions'][-1][1] = value
        elif key == 'condition':
            filters[-1]['condition'] = value
    return filters


def parse_condition(condition: str) -> list:
    """Split 'AND (a,op,v) OR (...)' into (boolean, attribute, is_custom_header, operator, value)."""
    terms = []
    pos = 0
    while (start := condition.find('(', pos)) != -1:
        boolean = 'OR' if 'O' in condition[pos:start] else 'AND'
        end, in_quote = start + 1, False
        while end < len(condition):
            if condition.startswith('\\"', end):
                end += 1
            elif condition[end] == ')' and not in_quote:
                break
            elif condition[end] == '"':
                in_quote = not in_quote
            end += 1
        else:
            raise Unsupported(f'unterminated condition: {condition}')
        term = condition[start + 1:end].replace('\\"', '"')
        attribute, operator, value = (term.split(',', 2) + ['', ''])[:3]
        attribute = attribute.strip()
        custom = attribute.startswith('"')
        attribute = attribute[1:].split('"')[0] if custom else attribute.lower()
        value = value.lstrip()
        if value.startswith('"'):
            value = value[1:-1] if value.endswith('"') and len(value) > 1 else value[1:]
        terms.append((boolean, attribute, custom, operator.strip().lower(), value))
        pos = end + 1
    return terms


def convert_term(attribute: str, custom: bool, operator: str, value: str, requires: set) -> str:
    if attribute == 'size' and not custom and operator in ('is greater than', 'is less than'):
        # Thunderbird compares whole KB: floor(bytes / 1024) against the value.
        if not value.isdigit():
            raise Unsupported(f'size "{value}"')
        kb = int(value)
        return f'size :over {(kb + 1) * 1024 - 1}' if operator == 'is greater than' else f'size :under {kb * 1024}'
    if operator not in OPERATORS:
        raise Unsupported(f'operator "{operator}" on "{attribute}"')
    negate, match, key = OPERATORS[operator]
    if custom or attribute == 'subject':
        test = f'header {match} {quote(attribute)}'
    elif attribute in ADDRESS_HEADERS:
        headers = quote_list(ADDRESS_HEADERS[attribute])
        # Thunderbird matches "contains" against the whole header, other operators per address.
        kind = 'header' if match == ':contains' or operator.endswith('empty') else 'address :all'
        test = f'{kind} {match} {headers}'
    elif attribute == 'body':
        requires.add('body')
        test = f'body :text {match}'
    else:
        raise Unsupported(f'condition on "{attribute}"')
    return ('not ' if negate else '') + f'{test} {quote(key(value))}'


def convert_condition(condition: str, requires: set) -> str:
    if condition == 'ALL':
        return 'true'
    terms = parse_condition(condition)
    if not terms:
        raise Unsupported('empty condition')
    # Thunderbird writes the boolean on every term but ignores the first one's.
    booleans = {t[0] for t in terms[1:]} or {terms[0][0]}
    if len(booleans) > 1:
        raise Unsupported('mixed AND/OR conditions')
    tests = [convert_term(*t[1:], requires) for t in terms]
    return ('anyof' if booleans == {'OR'} else 'allof') + ' (\n    ' + ',\n    '.join(tests) + '\n)'


def convert_action(action: str, value: str) -> str:
    if action in ('Move to folder', 'Copy to folder'):
        url = urlsplit(value)
        if url.scheme != 'imap':
            raise Unsupported(f'{action} "{value}" (only IMAP folders exist on the server)')
        folder = unquote(url.path.lstrip('/'))
        return ('fileinto ' if action == 'Move to folder' else 'fileinto :copy ') + quote(folder) + ';'
    if action in FLAG_ACTIONS:
        return FLAG_ACTIONS[action]
    if action == 'AddTag':
        return f'addflag {quote(value)};'
    if action == 'Forward':
        return f'redirect :copy {quote(value)};'
    if action == 'Delete':
        # ponytail: assumes the server's trash is called "Trash". If it does not
        # exist, fileinto fails and the message stays in INBOX, so nothing is lost.
        return 'fileinto "Trash";'
    if action == 'Stop execution':
        return 'stop;'
    raise Unsupported(f'action "{action}"')


def convert_filter(f: dict, requires: set, warnings: list) -> str:
    """Return one Sieve rule; filters that cannot be converted come back commented out."""
    rule_requires = set()
    try:
        if not f['enabled']:
            raise Unsupported('disabled in Thunderbird')
        test = convert_condition(f['condition'], rule_requires)
        lines = []
        # imap4flags only marks messages filed after the flag is set, and stop ends the script.
        order = lambda a: 0 if a[0] in FLAG_ACTIONS or a[0] == 'AddTag' else 2 if a[0] == 'Stop execution' else 1
        for action, value in sorted(f['actions'], key=order):
            try:
                lines.append('\t' + convert_action(action, value))
                rule_requires.update(REQUIRES.get(action, []))
            except Unsupported as e:
                lines.append(f'\t# not converted: {e}')
                warnings.append(f'{f["name"]}: skipped {e}')
        if all(line.startswith('\t#') for line in lines):
            raise Unsupported('no convertible actions')
    except Unsupported as e:
        warnings.append(f'{f["name"]}: rule commented out, {e}')
        original = [f'action="{a}" actionValue="{v}"' for a, v in f['actions']] + [f'condition="{f["condition"]}"']
        return f'# Not converted ({e}): {f["name"]}\n' + '\n'.join('# ' + line for line in original)
    requires.update(rule_requires)
    return f'# rule:[{f["name"]}]\nif {test}\n{{\n' + '\n'.join(lines) + '\n}'


def thunderbird_to_sieve(text: str, warnings: list = None) -> str:
    """Convert the contents of msgFilterRules.dat to a Sieve script."""
    warnings = [] if warnings is None else warnings
    requires = set()
    rules = [convert_filter(f, requires, warnings) for f in parse_filters(text)]
    head = f'require {quote_list(sorted(requires))};\n\n' if requires else ''
    return head + '\n\n'.join(rules) + '\n'


def main():
    if len(sys.argv) not in (2, 3):
        print("Usage: python filter_converter.py path/to/msgFilterRules.dat [output_file.sieve]")
        sys.exit(1)
    output_path = sys.argv[2] if len(sys.argv) == 3 else 'roundcube.sieve'
    try:
        with open(sys.argv[1], encoding='utf-8') as f:
            text = f.read()
        warnings = []
        script = thunderbird_to_sieve(text, warnings)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(script)
    except OSError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    for w in warnings:
        print(f'Warning: {w}', file=sys.stderr)
    converted = script.count('# rule:[')
    print(f'Sieve rules have been successfully written to {output_path}')
    print(f'Total rules converted: {converted} of {converted + script.count("# Not converted (")}')


if __name__ == '__main__':
    main()
