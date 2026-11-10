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

from filter_converter import (account_uri, format_filter, parse_condition, parse_filters,
                              sieve_to_thunderbird, thunderbird_to_sieve)


def tb_filter(name, terms, actions, enabled=True):
    """terms: 'ALL' or (boolean, attribute, operator, value[, custom header])."""
    if terms != 'ALL':
        terms = [(b, a, bool(custom), op, v) for b, a, op, v, *custom in terms]
    return format_filter(name, [] if terms == 'ALL' else terms, actions, enabled)


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
        sieve, _ = self.convert([('AND', 'subject', 'contains', 'x')],
                                [('Move to folder', 'imap://u@h/INBOX/&A4gDuwO1A7MDxwO,A8I- &- R&-D'),
                                 ('Copy to folder', 'imap://u@h/R&D')])
        self.assertIn('fileinto "INBOX/Έλεγχος & R&D";', sieve)
        self.assertIn('fileinto :copy "R&D";', sieve)


ACCOUNT = 'imap://user%40example.com@imap.example.com'


class SieveToThunderbirdTest(unittest.TestCase):
    def test_round_trip(self):
        folder = ACCOUNT + '/INBOX/My Stuff/&A5E- &- (old~)'
        filters = [tb_filter(f'subject {i}', [('AND', 'subject', 'contains', v)], [('Move to folder', folder)])
                   for i, v in enumerate(TRICKY_VALUES)]
        filters += [
            tb_filter('ops', [('AND', 'subject', op, 'x*y') for op in ('is', "isn't", 'begins with', 'ends with',
                                                                      "doesn't contain")]
                      + [('AND', 'subject', 'is empty', ''), ('AND', 'cc', "isn't empty", '')],
                      [('Mark read', ''), ('Stop execution', '')]),
            tb_filter('addresses', [('OR', 'from', 'is', 'a@b.c'), ('OR', 'to or cc', 'contains', 'list@'),
                                    ('OR', 'all addresses', 'contains', 'me@'), ('OR', 'from', "isn't", 'x@y.z')],
                      [('Copy to folder', ACCOUNT + '/Archive'), ('Mark flagged', ''), ('AddTag', '$label1')]),
            tb_filter('other', [('AND', 'body', 'contains', 'secret'), ('AND', 'size', 'is greater than', '10'),
                                ('AND', 'size', 'is less than', '200'), ('AND', 'x-spam', 'begins with', 'yes', True)],
                      [('Forward', 'a@b.c'), ('Mark unread', '')]),
            tb_filter('all', 'ALL', [('Mark read', '')]),
        ]
        original = parse_filters(tb_file(*filters))
        warnings = []
        back = parse_filters(sieve_to_thunderbird(thunderbird_to_sieve(tb_file(*filters)), ACCOUNT, warnings))
        self.assertEqual(warnings, [])
        self.assertEqual(len(back), len(original))
        for a, b in zip(original, back):
            with self.subTest(filter=a['name']):
                self.assertEqual(a['name'], b['name'])
                self.assertEqual(sorted(a['actions']), sorted(b['actions']))
                self.assertEqual(parse_condition(a['condition']), parse_condition(b['condition']))

    def test_roundcube_script(self):
        script = '''require ["fileinto", "imap4flags", "vacation", "copy"];
/* multi-line
   comment */
# rule:[spam domain]
if address :domain :is "from" ["spam.com", "junk.org"] { discard; }
# rule:[big]
if allof (size :over 100K, exists "list-id") { fileinto "INBOX/Big"; addflag ["\\\\Seen", "\\\\Flagged"]; }
# rule:[away]
if header :contains "subject" "holiday" {
    vacation :days 7 text:
I am away.
..dot-stuffed line
.
;
    redirect "boss@corp.com";
}
if not header :contains "subject" ["a", "b"] { keep; fileinto :copy "Seen Folder"; }
# rule:[branches]
if true { stop; } elsif header :is "x" "y" { stop; }
# rule:[nested]
if allof (anyof (header :is "a" "1", header :is "a" "2"), header :is "b" "3") { stop; }
# rule:[disabled]
if false # anyof (header :contains "subject" "x")
{ stop; }
# rule:[case sensitive]
if header :comparator "i;octet" :is "subject" "X" { stop; }
'''
        warnings = []
        filters = parse_filters(sieve_to_thunderbird(script, ACCOUNT, warnings))
        got = {f['name']: (parse_condition(f['condition']), f['actions']) for f in filters}
        self.assertEqual(got, {
            'spam domain': ([('OR', 'from', False, 'ends with', '@spam.com'), ('OR', 'from', False, 'ends with', '@junk.org')],
                            [['Delete', '']]),
            'big': ([('AND', 'size', False, 'is greater than', '99'), ('AND', 'list-id', True, "isn't empty", '')],
                    [['Move to folder', ACCOUNT + '/INBOX/Big'], ['Mark read', ''], ['Mark flagged', '']]),
            'away': ([('OR', 'subject', False, 'contains', 'holiday')], [['Forward', 'boss@corp.com'], ['Delete', '']]),
            'Rule 4': ([('AND', 'subject', False, "doesn't contain", 'a'), ('AND', 'subject', False, "doesn't contain", 'b')],
                       [['Copy to folder', ACCOUNT + '/Seen Folder']]),
            'branches': ([], [['Stop execution', '']]),
        })
        self.assertEqual(warnings, [
            'away: skipped action "vacation"',
            'Rule 6: not converted, "elsif" branch',
            'nested: not converted, nested anyof/allof',
            'disabled: not converted, disabled rule (if false)',
            'case sensitive: not converted, comparator i;octet (Thunderbird ignores case)',
        ])

    def test_syntax_error(self):
        with self.assertRaises(ValueError):
            sieve_to_thunderbird('if header :is "a" "b" { stop; ', ACCOUNT)

    def test_account_uri(self):
        self.assertEqual(account_uri(ACCOUNT + '/INBOX/X'), ACCOUNT)
        with tempfile.NamedTemporaryFile('w', suffix='.dat', delete=False) as f:
            f.write(tb_file(tb_filter('n', 'ALL', [('Mark read', '')]),
                            tb_filter('m', 'ALL', [('Move to folder', ACCOUNT + '/INBOX/X')])))
        self.addCleanup(os.remove, f.name)
        self.assertEqual(account_uri(f.name), ACCOUNT)
        with open(f.name, 'w') as g:
            g.write(tb_file(tb_filter('n', 'ALL', [('Mark read', '')])))
        with self.assertRaises(ValueError):
            account_uri(f.name)


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

    def test_non_ascii_folder_is_delivered(self):
        folder = os.path.join(self.dir, 'Maildir', '.INBOX.&A4gDuwO1A7MDxwO,A8I-')
        for sub in ('cur', 'new', 'tmp'):
            os.makedirs(os.path.join(folder, sub))
        script = self.write('t.sieve', thunderbird_to_sieve(tb_file(tb_filter(
            'greek', 'ALL', [('Move to folder', 'imap://u@h/INBOX/&A4gDuwO1A7MDxwO,A8I-')]))))
        mail = self.write('m.eml', 'From: a@b.c\nSubject: x\n\nx\n')
        subprocess.run(['sieve-test', '-e', '-o', f'mail_location=maildir:{self.dir}/Maildir',
                        '-o', 'namespace/inbox/separator=/', script, mail], check=True, capture_output=True)
        self.assertEqual(len(os.listdir(os.path.join(folder, 'new'))), 1)

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
