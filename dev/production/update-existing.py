#!/usr/bin/env python3
"""Root-owned, fixed-repository application updater; never installs infrastructure."""
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path('/opt/director5-production')
REPOSITORY = 'makors/director5'
BRANCH = 'director5-handoff'
APPS = ('orchestrator', 'manager', 'celery', 'ssh')
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def validate_sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected one lowercase full commit SHA')
    return value


def extract_source(archive, destination, sha):
    """Reject links, traversal, duplicate files, devices, and oversized archives."""
    prefix = f'director5-{validate_sha(sha)}'
    with tarfile.open(archive, 'r:gz') as bundle:
        members = []
        expanded = 0
        for member in bundle:
            members.append(member)
            expanded += member.size
            if len(members) > 30000 or expanded > MAX_EXPANDED:
                raise ValueError('Archive exceeds extraction limits')
        seen = set()
        for item in members:
            path = PurePosixPath(item.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != prefix:
                raise ValueError('Archive path outside expected commit root')
            if not (item.isdir() or item.isfile()) or item.name in seen:
                raise ValueError('Archive contains unsupported or duplicate entry')
            seen.add(item.name)
            if len(path.parts) == 1:
                if not item.isdir():
                    raise ValueError('Archive root is not a directory')
                continue
            target = destination.joinpath(*path.parts[1:])
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(item) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if item.mode & 0o111 else 0o644)


def download(url, target):
    request = urllib.request.Request(url, headers={'User-Agent': 'Director5-deployer'})
    with urllib.request.urlopen(request, timeout=60) as response, target.open('xb') as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE:
                raise ValueError('Archive download exceeds limit')
            output.write(chunk)


def check_tip(sha):
    request = urllib.request.Request(
        f'https://api.github.com/repos/{REPOSITORY}/commits/{BRANCH}',
        headers={'User-Agent': 'Director5-deployer', 'Accept': 'application/vnd.github+json'},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if json.load(response)['sha'] != sha:
            raise ValueError('Tested commit is no longer the deployment branch tip')


def image_override(image):
    # JSON is a YAML subset; avoid formatting attacker-controlled YAML strings.
    return json.dumps({'services': {name: {'image': image} for name in (*APPS, 'init')}}) + '\n'


def atomic_write(path, text):
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(text)
    temporary.replace(path)


def update(sha):
    validate_sha(sha)
    if os.geteuid() != 0 or not (ROOT / 'state/installation.json').is_file():
        raise RuntimeError('Run as root on an existing Director installation')
    os.umask(0o077)
    state = ROOT / 'state'
    lock = (state / 'update.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    check_tip(sha)
    release = Path(tempfile.mkdtemp(prefix=sha + '-', dir=ensure_directory(state / 'releases')))
    log = (release / 'update.log').open('ab', buffering=0)
    override = state / 'application-image.yaml'
    common = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(ROOT / '.env'),
              '-f', str(ROOT / 'compose.yaml')]
    if override.exists():
        common += ['-f', str(override)]

    def run(args, *, capture=False, output=None, input_file=None, timeout=600):
        return subprocess.run(args, cwd=ROOT, check=True, timeout=timeout,
                              stdout=subprocess.PIPE if capture else output or log,
                              stderr=log, stdin=input_file or subprocess.DEVNULL).stdout

    def compose(args, **kwargs):
        return run(common + args, **kwargs)

    previous = None
    quiesced = False
    migrations_started = False
    try:
        compose(['config', '--quiet'])
        manager_id = compose(['ps', '-q', 'manager'], capture=True).decode().strip()
        if not manager_id:
            raise RuntimeError('Existing Manager must be running before automatic update')
        previous = run(['docker', 'inspect', '--format', '{{.Image}}', manager_id], capture=True).decode().strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', previous):
            raise ValueError('Unexpected current application image identity')
        (release / 'previous-image.txt').write_text(previous + '\n')
        archive = release / 'source.tar.gz'
        download(f'https://codeload.github.com/{REPOSITORY}/tar.gz/{sha}', archive)
        source = release / 'source'
        source.mkdir()
        extract_source(archive, source, sha)
        image = f'director5-production:{sha}'
        print(f'Building tested commit {sha}.', flush=True)
        run(['docker', 'build', '-f', str(source / 'dev/production/Dockerfile'), '-t', image, str(source)], timeout=1800)
        check_tip(sha)  # A newer push during the build must not deploy an obsolete commit.
        # Save static files before collection; rollback restores prior files in place.
        with (release / 'static-before.tar').open('wb') as output:
            compose(['run', '--rm', '-T', '--no-deps', 'init', 'tar', '-C', '/static', '-cf', '-', '.'], output=output)
        print('Stopping only Director application services; hosted sites keep running.', flush=True)
        quiesced = True
        compose(['stop', '-t', '90', *APPS], timeout=150)
        with (release / 'manager-before.dump').open('wb') as output:
            compose(['exec', '-T', 'postgres', 'pg_dump', '-U', 'director', '-d', 'director', '-Fc'], output=output)
        if (release / 'manager-before.dump').stat().st_size < 20:
            raise RuntimeError('Database backup was empty')
        atomic_write(override, image_override(image))
        if '-f' not in common[-2:] or common[-1] != str(override):
            common.extend(['-f', str(override)])
        compose(['config', '--quiet'])
        migrations_started = True
        # Current root-owned init command runs migrate, collectstatic and bootstrap.
        compose(['run', '--rm', '-T', '--no-deps', 'init'], timeout=900)
        compose(['run', '--rm', '-T', '--no-deps', 'init', 'python', 'manage.py', 'check', '--deploy'])
        compose(['up', '-d', '--no-deps', '--wait', '--wait-timeout', '240', *APPS], timeout=300)
        # Public TLS is checked by GitHub; avoid host hairpin networking here.
        compose(['exec', '-T', 'manager', 'python', '-c',
                 "import urllib.request; r=urllib.request.Request('http://127.0.0.1:8080/accounts/login/',headers={'Host':'director.makors.xyz','X-Forwarded-Proto':'https'}); assert urllib.request.urlopen(r,timeout=10).status==200"])
        atomic_write(state / 'deployed-commit.json', json.dumps({'sha': sha, 'image': image, 'time': time.time(), 'backup': str(release)}) + '\n')
        print(f'Deployed {sha}. Backup and private log: {release}', flush=True)
    except Exception:
        print(f'Update failed. Private diagnostics and backup: {release}', file=sys.stderr, flush=True)
        if quiesced and previous:
            # Revert only executable code/static files, never a live database.
            atomic_write(override, image_override(previous))
            if common[-1] != str(override):
                common.extend(['-f', str(override)])
            try:
                with (release / 'static-before.tar').open('rb') as source:
                    compose(['run', '--rm', '-T', '--no-deps', 'init', 'tar', '-C', '/static', '-xf', '-'], input_file=source)
                compose(['up', '-d', '--no-deps', '--wait', '--wait-timeout', '240', *APPS], timeout=300)
                print('Previous application image/static files restored.', file=sys.stderr)
            except Exception:
                print('Previous image recovery failed; administrator intervention required.', file=sys.stderr)
            if migrations_started:
                print('Database migrations may have committed. No database downgrade/restore attempted; verify schema compatibility before further use.', file=sys.stderr)
        raise
    finally:
        log.close()
        lock.close()


def ensure_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def interrupted(signum, frame):
    signal.alarm(0)
    raise TimeoutError('Deployment interrupted or deadline exceeded')


if __name__ == '__main__':
    for stop_signal in (signal.SIGHUP, signal.SIGTERM, signal.SIGALRM):
        signal.signal(stop_signal, interrupted)
    signal.alarm(2400)
    try:
        if len(sys.argv) != 2:
            raise ValueError('Expected one commit SHA')
        update(sys.argv[1])
    except Exception as error:
        print(f'Deployment refused or failed ({type(error).__name__}).', file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        signal.alarm(0)
