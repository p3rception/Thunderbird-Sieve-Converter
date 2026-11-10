# Thunderbird-Sieve-Converter
This Python script converts Thunderbird email filter rules (from `msgFilterRules.dat`) to Sieve script format, which can be used with email systems supporting Sieve filtering, such as Roundcube. It can also convert a Sieve script back to Thunderbird filters.

# Usage
```
python filter_converter.py /path/to/msgFilterRules.dat [output_file.sieve]
```
- The first argument is the path to the Thunderbird filter file (msgFilterRules.dat).
- The second argument (optional) specifies the output Sieve file. If omitted, it defaults to roundcube.sieve.

Requires Python 3.8 or later. No other dependencies.

## Sieve to Thunderbird
```
python filter_converter.py --to-thunderbird --account ACCOUNT script.sieve [msgFilterRules.dat]
```
- `ACCOUNT` is either the account's current `msgFilterRules.dat` (the account is read from its folder targets) or the account URI, such as `imap://me%40uni.gr@mail.uni.gr`.
- The output defaults to `msgFilterRules.dat` in the current directory. An existing file is never overwritten.

To find the account URI, right-click any folder of the account in Thunderbird, choose Properties and copy the start of "Location", up to the first `/` after the server name. Alternatively, Account Settings > Server Settings shows the "User Name" and "Server Name": the URI is `imap://USER@SERVER`, with any `@` inside the user name written as `%40`.

The account's current filter file is in your Thunderbird profile, in `ImapMail/<server name>/msgFilterRules.dat`. Help > Troubleshooting Information > Profile Folder opens the profile. To install the converted filters, close Thunderbird, back up that file and replace it with the new one.

Rules using `elsif`/`else`, nested `anyof`/`allof`, `:regex`, case-sensitive comparators or tests Thunderbird does not have are left out and listed as warnings. Actions such as `vacation` and `reject` are skipped.

# What gets converted
- Conditions on subject, from, to, cc, "to or cc", "all addresses", body, size and custom headers
- Operators: contains, doesn't contain, is, isn't, begins with, ends with, is empty, isn't empty (size: is greater than, is less than)
- Actions: move or copy to an IMAP folder, mark read, mark unread, mark flagged, add tag, forward, delete (moves to `Trash`), stop execution

Filters that cannot be converted (disabled filters, conditions on date, status, tags, junk status and so on, local folders) are written to the output as comments with the reason, and listed as warnings. Unsupported actions such as "Change priority" are skipped inside an otherwise converted rule.

Folder paths are written with `/` as separator, as Thunderbird stores them. If your server uses `.` (for example `INBOX.Archive`), adjust the `fileinto` lines.

# Tests
```
python3 -m unittest -v
```
The tests that compile and run the generated script in a real Sieve engine (Dovecot Pigeonhole) are skipped unless `sievec` and `sieve-test` are installed. To run them in Docker:
```
docker build -t tb-sieve-test - <<'EOF'
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends dovecot-sieve python3
EOF
docker run --rm --network none -v "$PWD:/src:ro" tb-sieve-test sh -c \
  'useradd -m t && cp -r /src /home/t/src && chown -R t /home/t/src && su t -c "cd ~/src && python3 -m unittest -v"'
```

# Contributing
Feel free to open issues or submit pull requests if you have suggestions for improvements or find any bugs.

# License
This project is licensed under the MIT License. See the [LICENSE](/LICENSE) file for details.

