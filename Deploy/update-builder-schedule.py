#!/usr/bin/env python3
"""Apply explicit settings through the same preview/save API as the builder UI.

Run locally after deployment: credentials stay on this machine. Refuses stale
versions, externally edited sources, and pre-v6 builders. Never starts training.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains(actual[key], value) for key, value in expected.items())
    return actual == expected


def payload(session, url):
    response = session.get(url, timeout=30, allow_redirects=False)
    response.raise_for_status()
    match = re.search(r'<script id="schedule-builder-data" type="application/json">(.*?)</script>', response.text, re.S)
    if not match:
        raise RuntimeError('Builder payload absent. Check login/session expiry.')
    return json.loads(match[1])


def update(session, url, csrf, expected_version, patch, backup, check=False):
    before = payload(session, url)
    if contains(before['spec'], patch):
        print('Schedule already has the requested settings; no changes made.')
        return
    if before['version'] != expected_version or not before['id'] or before['notice']:
        raise RuntimeError('Schedule changed or is not builder-editable. Review it before updating.')
    if 'feature_export' not in before['spec'] or 'optimizer' not in before['spec']:
        raise RuntimeError('Deploy the generic export/Ranger builder before updating this schedule.')
    if before['post_url'] != urlsplit(url).path:
        raise RuntimeError('Builder save target differs from the requested schedule.')
    spec = {**copy.deepcopy(before['spec']), **patch}
    headers = {'X-CSRFToken': csrf, 'Referer': url}

    def post(body):
        response = session.post(url, json=body, headers=headers, timeout=60, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError('Builder request failed (HTTP %d); no automatic retry.' % response.status_code)
        return response.json()

    post({'action': 'preview', 'spec': spec})
    if check:
        print('Builder preview accepted; schedule has not been changed.')
        return
    # Exclusive creation prevents accidentally overwriting a previous backup.
    with open(backup, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(before, stream, indent=2)
    saved = post({'action': 'save', 'spec': spec, 'version': before['version'],
                  **{key: before[key] for key in ('name', 'scope', 'engine')}})
    after = payload(session, url)
    if saved['id'] != before['id'] or after['version'] != before['version'] + 1 or not contains(after['spec'], patch):
        raise RuntimeError('Save returned but read-back verification failed. Inspect the schedule and backup.')
    if any(after[key] != before[key] for key in ('id', 'name', 'scope', 'engine')):
        raise RuntimeError('Schedule identity changed unexpectedly; inspect the saved schedule.')
    if any(after['spec'][key] != value for key, value in before['spec'].items() if key not in patch):
        raise RuntimeError('Unrequested schedule settings changed; inspect the saved schedule.')
    print('Updated %s to version %d through the builder. Backup: %s' % (after['name'], after['version'], backup))
    print('Read-back verified. No training run was started.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('patch', type=Path)
    parser.add_argument('--session-file', type=Path, required=True)
    parser.add_argument('--expected-version', type=int, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != 'https' or target.username or target.query or target.fragment:
        parser.error('Use the HTTPS schedule builder URL without credentials/query/fragment.')
    cookies = dict(re.findall(r'(?im)\b(sessionid|csrftoken)\s*[:=]\s*["\x27]?([A-Za-z0-9]+)', args.session_file.read_text()))
    if set(cookies) != {'sessionid', 'csrftoken'}:
        parser.error('Session file must contain sessionid and csrftoken.')
    patch = json.loads(args.patch.read_text())
    if not isinstance(patch, dict) or not patch:
        parser.error('Patch must be a nonempty JSON object of builder settings.')
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=target.hostname, secure=True)
    update(session, args.url, cookies['csrftoken'], args.expected_version, patch, args.backup, args.check)


if __name__ == '__main__':
    main()
