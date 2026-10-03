#!/usr/bin/env python3
"""Forced SSH command: no shell, forwarding, uploads, or arbitrary arguments."""
import os
import re
import subprocess
import sys


def deployment_sha(command):
    match = re.fullmatch(r'deploy ([0-9a-f]{40})', command)
    if match is None:
        raise ValueError('Only deploy followed by a full commit SHA is accepted')
    return match[1]


if __name__ == '__main__':
    try:
        sha = deployment_sha(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError:
        print('Unsupported deployment command.', file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(subprocess.call(['/usr/bin/sudo', '-n', '/usr/local/sbin/director5-update', sha], stdin=subprocess.DEVNULL))
