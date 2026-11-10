"""Tests for filter_converter.

Run: python3 -m unittest -v
The Sieve engine tests need Dovecot Pigeonhole (sievec, sieve-test) and are
skipped without it. To run them in Docker, see the README.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from filter_converter import parse_condition, parse_filters, thunderbird_to_sieve


# Port of Thunderbird's writer (nsMsgSearchTerm::GetTermAsString, OutputValue,
# EscapeQuotesInStr, MsgTermListToString, nsMsgFilterList::WriteStrAttr), so the
# inputs below are exactly what Thunderbird puts in msgFilterRules.dat.
def tb_escape(s):
    return s.replace('"', '\\"')


def tb_term(boolean, attribute, operator, value, custom=False):
    attr = f'"{attribute}"' if custom else attribute
    quoted = ')' in value or value[:1] in (' ', '"')
    value = tb_escape(value)
    return f'{boolean} ({attr},{operator},' + (f'"{value}"' if quoted else value) + ')'


def tb_filter(name, terms, actions, enabled=True):
    lines = [f'name="{tb_escape(name)}"', f'enabled="{"yes" if enabled else "no"}"', 'type="17"']
    for action, value in actions:
        lines.append(f'action="{action}"')
        if value:
            lines.append(f'actionValue="{tb_escape(value)}"')
    condition = terms if isinstance(terms, str) else ' '.join(tb_term(*t) for t in terms)
    lines.append(f'condition="{tb_escape(condition)}"')
    return '\n'.join(lines) + '\n'


def tb_file(*filters):
    return 'version="9"\nlogging="no"\n' + ''.join(filters)


MOVE = ('Move to folder', 'imap://user%40example.com@imap.example.com/INBOX/Target')

TRICKY_VALUES = [
    'plain',
    'say "hi"',
    '"received"',
    'a (b)',
    'x)',
    ' leading space',
    'a, b, c',
    'AND (subject,contains,x)',
    'back\\slash',
    '*star? and ?',
    'Ελληνικά',
]


class ParseTest(unittest.TestCase):
    def test_values_survive_thunderbird_escaping(self):
        for value in TRICKY_VALUES:
            with self.subTest(value=value):
                f = parse_filters(tb_file(tb_filter('n', [('AND', 'subject', 'contains', value)], [MOVE])))[0]
                self.assertEqual(parse_condition(f['condition']), [('AND', 'subject', False, 'contains', value)])

    def test_custom_header_and_or(self):
        f = parse_filters(tb_file(tb_filter('n', [
            ('OR', 'X-Spam-Flag', 'is', 'YES', True),
            ('OR', 'received', 'contains', '"quoted"', True),
        ], [MOVE])))[0]
        self.assertEqual(parse_condition(f['condition']), [
            ('OR', 'X-Spam-Flag', True, 'is', 'YES'),
            ('OR', 'received', True, 'contains', '"quoted"'),
        ])

    def test_filter_fields(self):
        f = parse_filters(tb_file(tb_filter('say "hi"', [('AND', 'from', 'contains', 'x')],
                                            [MOVE, ('Mark read', '')], enabled=False)))[0]
        self.assertEqual(f['name'], 'say "hi"')
        self.assertFalse(f['enabled'])
        self.assertEqual(f['actions'], [list(MOVE), ['Mark read', '']])


class ConvertTest(unittest.TestCase):
    def convert(self, terms, actions=(MOVE,), **kw):
        warnings = []
        return thunderbird_to_sieve(tb_file(tb_filter('n', terms, list(actions), **kw)), warnings), warnings

    def test_quotes_are_escaped(self):
        sieve, _ = self.convert([('AND', 'received', 'contains', '"x" \\ y', True)])
        self.assertIn('header :contains "received" "\\"x\\" \\\\ y"', sieve)

    def test_operators(self):
        cases = {
            ('subject', 'is', 'x'): 'header :is "subject" "x"',
            ('subject', "isn't", 'x'): 'not header :is "subject" "x"',
            ('subject', 'begins with', 'a*'): 'header :matches "subject" "a\\\\**"',
            ('subject', 'ends with', 'z'): 'header :matches "subject" "*z"',
            ('subject', 'is empty', ''): 'not header :matches "subject" "?*"',
            ('to or cc', "doesn't contain", 'x'): 'not header :contains ["to", "cc"] "x"',
            ('from', 'is', 'a@b.c'): 'address :all :is "from" "a@b.c"',
            ('body', 'contains', 'x'): 'body :text :contains "x"',
            ('size', 'is greater than', '10'): 'size :over 11263',
            ('size', 'is less than', '10'): 'size :under 10240',
        }
        for (attribute, operator, value), expected in cases.items():
            with self.subTest(attribute=attribute, operator=operator):
                sieve, warnings = self.convert([('AND', attribute, operator, value)])
                self.assertIn(expected, sieve)
                self.assertEqual(warnings, [])

    def test_unsupported_condition_comments_out_rule(self):
        sieve, warnings = self.convert([('AND', 'subject', 'contains', 'x'), ('AND', 'date', 'is before', '1-Jan-2020')])
        self.assertNotIn('if ', sieve)
        self.assertIn('# Not converted (operator "is before" on "date"): n', sieve)
        self.assertEqual(len(warnings), 1)

    def test_disabled_filter_is_commented_out(self):
        sieve, _ = self.convert([('AND', 'subject', 'contains', 'x')], enabled=False)
        self.assertNotIn('if ', sieve)

    def test_all_condition_and_flag_order(self):
        sieve, _ = self.convert('ALL', [MOVE, ('Mark read', ''), ('Stop execution', '')])
        self.assertIn('if true\n{\n\taddflag "\\\\Seen";\n\tfileinto "INBOX/Target";\n\tstop;\n}', sieve)

    def test_folder_outside_inbox_is_decoded(self):
        sieve, _ = self.convert([('AND', 'subject', 'contains', 'x')],
                                [('Move to folder', 'imap://u@h/Archive/My%20Stuff/%CE%91')])
        self.assertIn('fileinto "Archive/My Stuff/Α";', sieve)


ENGINE = shutil.which('sievec') and shutil.which('sieve-test')

# (subject, from, to, cc, body) -> folders sieve-test must file the message into
MESSAGES = [
    (('say "hi" to me', 'a@x.org', 'me@x.org', '', 'hello'), {'Quotes'}),
    (('Report (weekly)', 'a@x.org', 'me@x.org', '', 'hello'), {'Parens'}),
    (('hello', 'boss@corp.com', 'me@x.org', '', 'hello'), {'Boss'}),
    (('hello', 'a@x.org', 'me@x.org', 'list@x.org', 'hello'), {'Lists'}),
    (('hello', 'a@x.org', 'me@x.org', '', 'contains secret word'), {'Body'}),
    (('[SPAM] buy', 'a@x.org', 'me@x.org', '', 'hello'), {'Spam'}),
    (('nothing', 'a@x.org', 'me@x.org', '', 'hello'), set()),
]

ENGINE_FILTERS = tb_file(
    tb_filter('quotes', [('AND', 'subject', 'contains', '"hi"')], [('Move to folder', 'imap://u@h/INBOX/Quotes')]),
    tb_filter('parens', [('AND', 'subject', 'is', 'Report (weekly)')], [('Move to folder', 'imap://u@h/INBOX/Parens')]),
    tb_filter('boss', [('AND', 'from', 'is', 'boss@corp.com')],
              [('Move to folder', 'imap://u@h/INBOX/Boss'), ('Mark read', '')]),
    tb_filter('lists', [('OR', 'to or cc', 'contains', 'list@')], [('Move to folder', 'imap://u@h/INBOX/Lists')]),
    tb_filter('body', [('AND', 'body', 'contains', 'secret')], [('Move to folder', 'imap://u@h/INBOX/Body')]),
    tb_filter('spam', [('AND', 'subject', 'begins with', '[SPAM]')], [('Move to folder', 'imap://u@h/INBOX/Spam')]),
    tb_filter('off', [('AND', 'subject', 'contains', 'hello')], [MOVE], enabled=False),
    tb_filter('unsupported', [('AND', 'date', 'is before', '1-Jan-2020')], [MOVE]),
)


@unittest.skipUnless(ENGINE, 'Dovecot Pigeonhole sievec/sieve-test not installed')
class SieveEngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_tricky_values_compile(self):
        filters = [tb_filter(v, [('AND', 'subject', op, v), ('AND', 'X-Hdr', op, v, True)], [MOVE])
                   for v in TRICKY_VALUES for op in ('contains', 'begins with', 'ends with', "isn't")]
        script = self.write('t.sieve', thunderbird_to_sieve(tb_file(*filters)))
        subprocess.run(['sievec', script], check=True, capture_output=True, text=True)

    def test_messages_are_filed(self):
        script = self.write('t.sieve', thunderbird_to_sieve(ENGINE_FILTERS))
        for (subject, sender, to, cc, body), expected in MESSAGES:
            with self.subTest(subject=subject):
                mail = self.write('m.eml', f'From: {sender}\nTo: {to}\n' + (f'Cc: {cc}\n' if cc else '')
                                  + f'Subject: {subject}\n\n{body}\n')
                out = subprocess.run(['sieve-test', script, mail], check=True, capture_output=True, text=True).stdout
                self.assertEqual(set(re.findall(r'store message in folder: INBOX/(\S+)', out)), expected, out)
                if subject == 'hello' and sender.startswith('boss'):
                    self.assertIn('\\seen', out.lower())


if __name__ == '__main__':
    unittest.main()
