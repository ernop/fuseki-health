#!/usr/bin/env python3
"""External health monitor for fuseki.net, run by GitHub Actions every 5 minutes.

    python3 monitor.py <config.json> <status.json>

config.json is written by the workflow from the FUSEKI_HEALTH_CONFIG secret:
    smtp_login, smtp_password  the Gmail account that sends alerts
    from_email, to_email       sender and recipient
    editor_prefix              the private editor's path prefix, e.g. "/abc"
    disk_reader_key            private key of diskread@tpbeta, which can only run df
status.json is this repository's public record of each check's state. It
holds only values that change with the state, so it is committed only when
something happens (and once a month, which keeps GitHub's schedule enabled).
"""
import json
import smtplib
import subprocess
import sys
import tempfile
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

HERE = Path(__file__).resolve().parent
KNOWN_HOSTS = HERE / 'known_hosts'
SERVER = 'diskread@146.190.147.109'
EDITOR_HOST = 'edit.fuseki.net'
SMTP_HOST, SMTP_PORT = 'smtp.gmail.com', 587
REMINDER_SECONDS = 6 * 60 * 60
MIN_FREE_GIB = 20
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
EMPTY_GROUP_STATE = {'status': 'unknown', 'changed_at': 0, 'notified_at': 0}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, expected_status, marker=None, follow_redirects=True):
    """Returns an error string, or None when the URL answers as expected."""
    request = Request(url, headers={'User-Agent': 'fuseki-health-monitor/2.0'})
    try:
        opener = urlopen if follow_redirects else build_opener(NoRedirects).open
        with opener(request, timeout=20) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    except URLError as error:
        return f'{url}: request failed: {error.reason}'
    if status not in expected_status:
        return f'{url}: expected HTTP {sorted(expected_status)}, got {status}'
    if marker and marker not in body:
        return f'{url}: HTTP {status}, but required content marker is absent'
    return None


def availability_errors(config):
    prefix = config['editor_prefix'].rstrip('/')
    if not prefix.strip('/'):
        raise ValueError('editor_prefix is empty; the editor route cannot be checked')
    checks = [
        ('https://fuseki.net/', {200}, b'<title>', True),
        ('https://fuseki.net/feed.xml', {200}, b'<feed', True),
        (f'https://{EDITOR_HOST}{prefix}/articles/', REDIRECT_STATUSES, None, False),
    ]
    errors = [error for url, statuses, marker, follow in checks
              if (error := fetch(url, statuses, marker, follow))]
    # The prefix is private; status.json and the run log are public.
    return [error.replace(prefix, '/<editor prefix>') for error in errors]


def read_disk(config):
    """Returns (free_bytes, total_bytes) of tpbeta's root filesystem."""
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / 'reader'
        key.write_text(config['disk_reader_key'].rstrip('\n') + '\n')
        key.chmod(0o600)
        result = subprocess.run(
            ['ssh', '-T', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={KNOWN_HOSTS}',
             '-o', 'ConnectTimeout=20', SERVER],
            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or f'ssh exited with {result.returncode}')
    return parse_df(result.stdout)


def parse_df(output):
    """Parses `df -B1 --output=avail,size /`: a header line, then two numbers."""
    lines = output.strip().splitlines()
    if len(lines) != 2 or lines[0].split() != ['Avail', '1B-blocks']:
        raise ValueError(f'unexpected df output: {output!r}')
    free, total = (int(value) for value in lines[1].split())
    return free, total


def disk_errors(config):
    try:
        free, total = read_disk(config)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return [f'could not read free space: {error}']
    free_gib = free / 1024 ** 3
    if free_gib < MIN_FREE_GIB:
        return [
            f'/ has {free_gib:.1f} GiB free of {total / 1024 ** 3:.1f} GiB (warning threshold '
            f'{MIN_FREE_GIB} GiB). The camera inbox, media and tp2021 database dumps grow '
            'without automatic pruning; free space by hand.'
        ]
    return []


def disk_subject(errors):
    if all(error.startswith('could not read') for error in errors):
        return 'tpbeta disk space could not be checked'
    return 'tpbeta disk space is LOW'


# Each group runs the same alert/reminder/recovery state machine but keeps
# independent state and honest subjects: a low disk is not "fuseki.net DOWN".
CHECK_GROUPS = [
    {
        'name': 'availability',
        # Late-bound so tests can patch the module-level functions.
        'errors': lambda config: availability_errors(config),
        'down_subject': lambda errors: 'fuseki.net is DOWN',
        'up_subject': 'fuseki.net has RECOVERED',
        'up_body': 'The homepage, feed, and private editor route are responding again.',
    },
    {
        'name': 'disk',
        'errors': lambda config: disk_errors(config),
        'down_subject': lambda errors: disk_subject(errors),
        'up_subject': 'tpbeta disk space has RECOVERED',
        'up_body': f'The host has at least {MIN_FREE_GIB} GiB free again.',
    },
]


def send_email(config, subject, body):
    message = MIMEText(body, 'plain', 'utf-8')
    message['Subject'] = subject
    message['From'] = config['from_email']
    message['To'] = config['to_email']
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(config['smtp_login'], config['smtp_password'])
        server.sendmail(config['smtp_login'], [config['to_email']], message.as_string())


def check_group(group, config, previous, now):
    """Runs one check group's state machine. Returns (new_state, errors)."""
    errors = group['errors'](config)
    status = 'down' if errors else 'up'
    previous_status = previous.get('status', 'unknown')
    changed = status != previous_status
    notified_at = previous.get('notified_at', 0)

    should_notify = changed and (previous_status != 'unknown' or status == 'down')
    if status == 'down' and now - notified_at >= REMINDER_SECONDS:
        should_notify = True

    if should_notify:
        if status == 'down':
            subject = group['down_subject'](errors)
            body = ('Fuseki monitoring detected a problem:\n\n'
                    + '\n'.join(f'- {error}' for error in errors)
                    + '\n\nThe monitor will send a recovery message when the check passes.')
        else:
            duration = max(0, now - previous.get('changed_at', now))
            subject = group['up_subject']
            body = group['up_body'] + f'\n\nObserved duration: approximately {duration // 60} minutes.'
        send_email(config, subject, body)
        notified_at = now

    state = {'status': status, 'changed_at': now if changed else previous.get('changed_at', now),
             'notified_at': notified_at}
    if status == 'down':
        state['problem'] = group['down_subject'](errors)
    return state, errors


def run(config_path, status_path):
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    status_file = Path(status_path)
    record = json.loads(status_file.read_text(encoding='utf-8')) if status_file.exists() else {'groups': {}}
    now = int(time.time())
    for group in CHECK_GROUPS:
        previous = record['groups'].get(group['name'], EMPTY_GROUP_STATE)
        record['groups'][group['name']], errors = check_group(group, config, previous, now)
        print(f"{group['name']}: " + ('; '.join(errors) if errors else 'ok'))
    record['checked_month'] = time.strftime('%Y-%m', time.gmtime(now))
    status_file.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: monitor.py <config.json> <status.json>')
    run(sys.argv[1], sys.argv[2])
