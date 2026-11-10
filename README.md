# Thunderbird-Sieve-Converter
This Python script converts Thunderbird email filter rules (from `msgFilterRules.dat`) to Sieve script format, which can be used with email systems supporting Sieve filtering, such as Roundcube.

# Usage
```
python filter_converter.py /path/to/msgFilterRules.dat [output_file.sieve]
```
- The first argument is the path to the Thunderbird filter file (msgFilterRules.dat).
- The second argument (optional) specifies the output Sieve file. If omitted, it defaults to roundcube.sieve.

Requires Python 3.8 or later. No other dependencies.

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

