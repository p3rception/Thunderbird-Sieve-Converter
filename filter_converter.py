"""Convert Thunderbird message filters (msgFilterRules.dat) to a Sieve script.

The parsing follows Thunderbird's own reader (nsMsgFilterList::LoadValue,
nsMsgFilterList::ParseCondition, nsMsgSearchTerm::DeStreamNew), so quoted
values, escaped quotes and custom headers come through unchanged.
"""
import argparse
import base64
import binascii
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


def decode_imap_utf7(name: str) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501), which Thunderbird keeps in folder URIs; Sieve wants UTF-8.

    Names that are not valid modified UTF-7 (a bare "&") are returned unchanged.
    """
    if '&' in re.sub(r'&[A-Za-z0-9+,]*-', '', name):
        return name
    try:
        return re.sub(r'&([A-Za-z0-9+,]*)-', lambda m: base64.b64decode(
            m[1].replace(',', '/') + '=' * (-len(m[1]) % 4)).decode('utf-16-be') if m[1] else '&', name)
    except (binascii.Error, UnicodeDecodeError):
        return name


def encode_imap_utf7(name: str) -> str:
    return re.sub(r'&|[^\x20-\x7e]+', lambda m: '&-' if m[0] == '&' else '&' + base64.b64encode(
        m[0].encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-', name)


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


def format_condition(terms) -> str:
    """Inverse of parse_condition, following nsMsgSearchTerm::GetTermAsString and OutputValue."""
    if not terms:
        return 'ALL'
    out = []
    for boolean, attribute, custom, operator, value in terms:
        value = str(value)
        quoted = ')' in value or value[:1] in (' ', '"')
        value = value.replace('"', '\\"')
        attribute = f'"{attribute}"' if custom else attribute
        out.append(f'{boolean} ({attribute},{operator},' + (f'"{value}"' if quoted else value) + ')')
    return ' '.join(out)


def format_filter(name: str, terms, actions, enabled: bool = True) -> str:
    """Write one filter the way nsMsgFilter::SaveToTextFile does."""
    def esc(s):
        return s.replace('"', '\\"')
    lines = [f'name="{esc(name)}"', f'enabled="{"yes" if enabled else "no"}"', 'type="17"']
    for action, value in actions:
        lines.append(f'action="{action}"')
        if value:
            lines.append(f'actionValue="{esc(value)}"')
    lines.append(f'condition="{esc(format_condition(terms))}"')
    return '\n'.join(lines) + '\n'


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
        folder = decode_imap_utf7(unquote(url.path.lstrip('/')))
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


# --- Sieve to Thunderbird ---

class Tag(str):
    pass


SIEVE_TOKEN = re.compile(r"""
    \s+
  | \#\ rule:\[(?P<rule>[^\n]*)\][^\n]*
  | \#[^\n]* | /\*.*?\*/
  | text:[ \t]*(?:\#[^\n]*)?\r?\n(?P<text>.*?)^\.\r?$
  | "(?P<string>(?:[^"\\]|\\.)*)"
  | (?P<number>\d+)(?P<unit>[KMG]?)
  | (?P<tag>:[A-Za-z_]\w*)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<punct>[\[\](){},;])
""", re.X | re.S | re.M | re.I)


def tokenize_sieve(text: str):
    """Return the tokens and {token index: name} for Roundcube's '# rule:[name]' comments."""
    tokens, names, pos = [], {}, 0
    while pos < len(text):
        m = SIEVE_TOKEN.match(text, pos)
        if not m:
            raise ValueError(f'Sieve syntax error on line {text.count(chr(10), 0, pos) + 1}')
        pos = m.end()
        if m['rule'] is not None:
            names[len(tokens)] = m['rule']
        elif m['text'] is not None:
            tokens.append(('str', re.sub(r'^\.\.', '.', m['text'], flags=re.M)))
        elif m['string'] is not None:
            tokens.append(('str', re.sub(r'\\(.)', r'\1', m['string'], flags=re.S)))
        elif m['number']:
            tokens.append(('num', int(m['number']) * 1024 ** ' KMG'.index(m['unit'].upper() or ' ')))
        elif m['tag']:
            tokens.append(('tag', m['tag'].lower()))
        elif m['id']:
            tokens.append(('id', m['id'].lower()))
        elif m['punct']:
            tokens.append((m['punct'], m['punct']))
    return tokens, names


def parse_sieve(text: str) -> list:
    """Parse RFC 5228 syntax into top-level commands (rule name, name, args, tests, block).

    A test is (name, args, tests); args holds str, int, Tag and lists of str.
    """
    tokens, names = tokenize_sieve(text)
    pos = 0

    def peek():
        return tokens[pos][0] if pos < len(tokens) else None

    def take(kind=None):
        nonlocal pos
        if peek() is None or (kind and peek() != kind):
            found = tokens[pos][1] if pos < len(tokens) else 'end of script'
            raise ValueError(f'Sieve syntax error: expected {kind}, found "{found}"')
        pos += 1
        return tokens[pos - 1][1]

    def arguments():
        args = []
        while peek() in ('str', 'num', 'tag', '['):
            if peek() == '[':
                take('[')
                items = [take('str')]
                while peek() == ',':
                    take(',')
                    items.append(take('str'))
                take(']')
                args.append(items)
            else:
                kind = peek()
                args.append(Tag(take()) if kind == 'tag' else take())
        return args

    def tests():
        if peek() == 'id':
            return [(take('id'), arguments(), tests())]
        if peek() != '(':
            return []
        take('(')
        result = tests()
        while peek() == ',':
            take(',')
            result += tests()
        take(')')
        return result

    def commands():
        result = []
        while peek() not in (None, '}'):
            name = names.get(pos)
            command, args, test = take('id'), arguments(), tests()
            block = []
            if peek() == '{':
                take('{')
                block = commands()
                take('}')
            else:
                take(';')
            result.append((name, command, args, test, block))
        return result

    result = commands()
    if peek() is not None:
        raise ValueError('Sieve syntax error: unexpected "}"')
    return result


def split_args(args):
    """Return ({tag: True or its value}, positional args) with strings wrapped in lists."""
    tags, positional, it = {}, [], iter(args)
    for a in it:
        if isinstance(a, Tag):
            value = next(it, []) if a in (':comparator', ':flags') else True
            tags[a] = [value] if isinstance(value, str) else value
        else:
            positional.append([a] if isinstance(a, str) else a)
    return tags, positional


SIEVE_ATTRIBUTES = {
    ('subject',): 'subject',
    ('from',): 'from',
    ('to',): 'to',
    ('cc',): 'cc',
    ('cc', 'to'): 'to or cc',
    ('bcc', 'cc', 'from', 'to'): 'all addresses',
}
NEGATED = {'contains': "doesn't contain", 'is': "isn't", "isn't empty": 'is empty'}
WILDCARD_OPERATORS = {(False, False): 'is', (False, True): 'begins with', (True, False): 'ends with', (True, True): 'contains'}


def sieve_attribute(headers):
    key = tuple(sorted(h.lower() for h in headers))
    if key in SIEVE_ATTRIBUTES:
        return SIEVE_ATTRIBUTES[key], False
    if len(key) == 1:
        return key[0], True
    raise Unsupported(f'test on headers {", ".join(headers)}')


def sieve_operator(match: str, key: str, negate: bool):
    """Map a Sieve match type and key to a Thunderbird operator and value."""
    if match == ':matches':
        if key in ('?*', '*?'):
            operator, key = "isn't empty", ''
        else:
            parts = [('*', '') if w == '*' else ('?', '') if w else ('', e or t)
                     for e, w, t in re.findall(r'\\(.)|([*?])|([^\\*?]+)', key, re.S)]
            lead = parts[:1] == [('*', '')]
            parts = parts[lead:]
            trail = parts[-1:] == [('*', '')]
            parts = parts[:len(parts) - trail]
            if not parts or any(w for w, _ in parts):
                raise Unsupported(f':matches "{key}"')
            operator, key = WILDCARD_OPERATORS[lead, trail], ''.join(v for _, v in parts)
    elif match in (':contains', ':is'):
        operator = match[1:]
    else:
        raise Unsupported(f'match type {match}')
    if negate:
        if operator not in NEGATED:
            raise Unsupported(f'not {match} "{key}"')
        operator = NEGATED[operator]
    return operator, key


def sieve_test_terms(name: str, args, negate: bool):
    """Return (boolean, [(attribute, custom, operator, value)]) for one header/address/body/size/exists test."""
    tags, positional = split_args(args)
    if tags.get(':comparator', ['i;ascii-casemap'])[0] not in ('i;ascii-casemap', 'i;unicode-casemap'):
        raise Unsupported(f'comparator {tags[":comparator"][0]} (Thunderbird ignores case)')
    if name == 'size' and not negate and len(positional) == 1 and isinstance(positional[0], int):
        size = positional[0]
        if ':over' in tags:
            return 'AND', [('size', False, 'is greater than', max((size + 1) // 1024 - 1, 0))]
        if ':under' in tags:
            return 'AND', [('size', False, 'is less than', size // 1024)]
    if name == 'exists' and len(positional) == 1 and len(positional[0]) == 1:
        return 'AND', [(*sieve_attribute(positional[0]), 'is empty' if negate else "isn't empty", '')]
    match = next((t for t in (':is', ':contains', ':matches', ':regex', ':value', ':count') if t in tags), ':is')
    if name == 'body' and len(positional) == 1 and not set(tags) - {':text', ':comparator', match}:
        attribute, keys = ('body', False), positional[0]
    elif name in ('header', 'address') and len(positional) == 2:
        attribute, keys = sieve_attribute(positional[0]), positional[1]
    else:
        raise Unsupported(f'test "{name}"' + ''.join(f' {t}' for t in tags))
    part = next((t for t in (':localpart', ':domain', ':user', ':detail') if t in tags), None)
    terms = []
    for key in keys:
        if part == ':domain' and match == ':is' and not negate:
            operator, key = 'ends with', '@' + key
        elif part:
            raise Unsupported(f'address {part} {match}')
        else:
            operator, key = sieve_operator(match, key, negate)
        terms.append((*attribute, operator, key))
    # Several keys mean any of them; Thunderbird's default for a single condition is AND.
    return ('OR' if len(terms) > 1 and not negate else 'AND'), terms


def sieve_condition(test):
    """Return (boolean, terms) for a rule's test; no terms means every message."""
    name, args, tests = test
    if name == 'true':
        return 'AND', []
    if name == 'false':
        raise Unsupported('disabled rule (if false)')
    if name in ('anyof', 'allof'):
        boolean, terms = ('OR' if name == 'anyof' else 'AND'), []
        for sub in tests:
            sub_boolean, sub_terms = sieve_condition(sub)
            if not sub_terms or (len(sub_terms) > 1 and sub_boolean != boolean):
                raise Unsupported('nested anyof/allof')
            terms += sub_terms
        return boolean, terms
    if name == 'not':
        (name, args, tests), = tests
        if tests:
            raise Unsupported(f'not {name}')
        return sieve_test_terms(name, args, negate=True)
    return sieve_test_terms(name, args, negate=False)


def sieve_flags(flags, remove=False) -> list:
    actions = []
    for flag in ' '.join(flags).split():
        if flag.lower() == '\\seen':
            actions.append(['Mark unread' if remove else 'Mark read', ''])
        elif flag.lower() == '\\flagged' and not remove:
            actions.append(['Mark flagged', ''])
        elif not flag.startswith('\\') and not remove:
            actions.append(['AddTag', flag])
        else:
            raise Unsupported(f'{"removeflag" if remove else "addflag"} {flag}')
    return actions


def sieve_actions(name: str, args, account: str) -> list:
    tags, positional = split_args(args)
    if name == 'fileinto' and len(positional) == 1:
        folder = account + '/' + encode_imap_utf7(positional[0][0])
        return sieve_flags(tags.get(':flags', [])) + [['Copy to folder' if ':copy' in tags else 'Move to folder', folder]]
    if name in ('addflag', 'setflag') and len(positional) == 1:
        return sieve_flags(positional[0])
    if name == 'removeflag' and len(positional) == 1:
        return sieve_flags(positional[0], remove=True)
    if name == 'redirect' and len(positional) == 1:
        # Without :copy the message is not kept, which Thunderbird does with Delete.
        return [['Forward', positional[0][0]]] + ([] if ':copy' in tags else [['Delete', '']])
    if name == 'discard':
        return [['Delete', '']]
    if name == 'keep':
        return []
    if name == 'stop':
        return [['Stop execution', '']]
    raise Unsupported(f'action "{name}"')


def sieve_to_thunderbird(text: str, account: str, warnings: list = None) -> str:
    """Convert a Sieve script to msgFilterRules.dat content; account is imap://user@server."""
    warnings = [] if warnings is None else warnings
    filters = []
    commands = [c for c in parse_sieve(text) if c[1] != 'require']
    for number, (rule_name, name, args, tests, block) in enumerate(commands, 1):
        label = rule_name or f'Rule {number}'
        try:
            if name != 'if' or len(tests) != 1:
                raise Unsupported(f'"{name}" branch' if name in ('elsif', 'else') else f'top-level "{name}"')
            boolean, terms = sieve_condition(tests[0])
            if any(re.search(r'[\r\n]', str(t[3])) for t in terms):
                raise Unsupported('multi-line value')
            actions = []
            for command, command_args, _, _ in (c[1:] for c in block):
                try:
                    actions += sieve_actions(command, command_args, account)
                except Unsupported as e:
                    warnings.append(f'{label}: skipped {e}')
            if not actions:
                raise Unsupported('no convertible actions')
        except Unsupported as e:
            warnings.append(f'{label}: not converted, {e}')
            continue
        filters.append(format_filter(label, [(boolean, *t) for t in terms], actions))
    return 'version="9"\nlogging="no"\n' + ''.join(filters)


def account_uri(value: str) -> str:
    """Return imap://user@server from a folder URI or from an existing msgFilterRules.dat."""
    if value.startswith('imap://'):
        return 'imap://' + urlsplit(value).netloc
    with open(value, encoding='utf-8') as f:
        for flt in parse_filters(f.read()):
            for _, target in flt['actions']:
                if target.startswith('imap://'):
                    return 'imap://' + urlsplit(target).netloc
    raise ValueError(f'no IMAP folder found in {value}; pass --account imap://user@server instead')


def main():
    parser = argparse.ArgumentParser(description='Convert Thunderbird filters to Sieve, or Sieve to Thunderbird filters.')
    parser.add_argument('input', help='msgFilterRules.dat, or a Sieve script with --to-thunderbird')
    parser.add_argument('output', nargs='?', help='default: roundcube.sieve, or msgFilterRules.dat with --to-thunderbird')
    parser.add_argument('--to-thunderbird', action='store_true', help='convert a Sieve script to msgFilterRules.dat')
    parser.add_argument('--account', help="the account's existing msgFilterRules.dat, or imap://user@server")
    args = parser.parse_args()
    if args.to_thunderbird and not args.account:
        parser.error('--to-thunderbird needs --account')
    output_path = args.output or ('msgFilterRules.dat' if args.to_thunderbird else 'roundcube.sieve')
    warnings = []
    try:
        with open(args.input, encoding='utf-8') as f:
            text = f.read()
        if args.to_thunderbird:
            result = sieve_to_thunderbird(text, account_uri(args.account), warnings)
        else:
            result = thunderbird_to_sieve(text, warnings)
        # Never overwrite a filter file: it may be the one Thunderbird is using.
        with open(output_path, 'x' if args.to_thunderbird else 'w', encoding='utf-8') as f:
            f.write(result)
    except (OSError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    for w in warnings:
        print(f'Warning: {w}', file=sys.stderr)
    if args.to_thunderbird:
        converted = result.count('\nname="')
        total = converted + sum(': not converted, ' in w for w in warnings)
    else:
        converted = result.count('# rule:[')
        total = converted + result.count('# Not converted (')
    print(f'{"Thunderbird filters" if args.to_thunderbird else "Sieve rules"} have been successfully written to {output_path}')
    print(f'Total rules converted: {converted} of {total}')


if __name__ == '__main__':
    main()
