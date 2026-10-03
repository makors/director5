"""Updater boundaries without Docker, network access, or root side effects."""
import importlib.util
import io
import contextlib
import json
import subprocess
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


def module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), BASE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


updater = module('update-existing.py')
entrypoint = module('deploy-ssh-entrypoint.py')


class UpdateSafety(unittest.TestCase):
    def archive(self, folder, members):
        archive = folder / 'input.tar.gz'
        with tarfile.open(archive, 'w:gz') as handle:
            for name, kind, value in members:
                item = tarfile.TarInfo(name)
                item.type = kind
                if kind == tarfile.REGTYPE:
                    item.size = len(value)
                    handle.addfile(item, io.BytesIO(value))
                else:
                    item.linkname = value
                    handle.addfile(item)
        return archive

    def test_exact_commit_source_extracts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = self.archive(folder, [(f'director5-{SHA}/manager/code.py', tarfile.REGTYPE, b'valid')])
            updater.extract_source(archive, folder / 'source', SHA)
            self.assertEqual((folder / 'source/manager/code.py').read_bytes(), b'valid')

    def test_rejects_traversal_wrong_root_links_devices_and_duplicates(self):
        prefix = f'director5-{SHA}'
        invalid = [
            [(f'{prefix}/../../escape', tarfile.REGTYPE, b'x')],
            [('/absolute', tarfile.REGTYPE, b'x')],
            [('other-commit/code', tarfile.REGTYPE, b'x')],
            [(f'{prefix}/link', tarfile.SYMTYPE, '/data')],
            [(f'{prefix}/link', tarfile.LNKTYPE, '/data')],
            [(f'{prefix}/device', tarfile.CHRTYPE, '')],
            [(f'{prefix}/same', tarfile.REGTYPE, b'x'), (f'{prefix}/same', tarfile.REGTYPE, b'y')],
        ]
        for members in invalid:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                archive = self.archive(folder, members)
                with self.assertRaises(ValueError):
                    updater.extract_source(archive, folder / 'source', SHA)
                self.assertFalse((folder / 'escape').exists())

    def test_expanded_archive_limit_is_enforced_before_writes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, 'MAX_EXPANDED', 2):
            folder = Path(directory)
            archive = self.archive(folder, [(f'director5-{SHA}/file', tarfile.REGTYPE, b'123')])
            with self.assertRaises(ValueError):
                updater.extract_source(archive, folder / 'source', SHA)
            self.assertFalse((folder / 'source').exists())

    def test_forced_command_does_not_allow_shell_or_alternate_ref(self):
        self.assertEqual(entrypoint.deployment_sha('deploy ' + SHA), SHA)
        for command in ('', 'sh', 'deploy main', 'deploy ' + SHA + '; id',
                        'deploy ' + SHA + '\n', ' deploy ' + SHA, 'deploy ' + SHA + ' extra'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                entrypoint.deployment_sha(command)

    def test_override_changes_only_application_image(self):
        import json
        config = json.loads(updater.image_override('sha256:' + 'b' * 64))
        self.assertEqual(set(config['services']), {'init', 'manager', 'celery', 'ssh', 'orchestrator'})
        for service in config['services'].values():
            self.assertEqual(set(service), {'image'})

    def test_superseded_commit_is_rejected(self):
        response = io.BytesIO(b'{"sha":"' + b'b' * 40 + b'"}')
        with patch.object(updater.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(ValueError):
                updater.check_tip(SHA)


    def exercise_update(self, failure=None):
        folder_context = tempfile.TemporaryDirectory()
        self.addCleanup(folder_context.cleanup)
        root = Path(folder_context.name)
        (root / 'state').mkdir()
        (root / 'state/installation.json').write_text('{}')
        calls = []
        old_image = 'sha256:' + 'b' * 64

        def fake_download(url, target):
            with tarfile.open(target, 'w:gz') as handle:
                item = tarfile.TarInfo(f'director5-{SHA}/README.md')
                item.size = 3
                handle.addfile(item, io.BytesIO(b'new'))

        def fake_run(command, **kwargs):
            calls.append(command)
            if failure == 'build' and command[:2] == ['docker', 'build']:
                raise subprocess.CalledProcessError(1, command)
            if failure == 'migration' and 'run' in command and command[-1] == 'init':
                raise subprocess.CalledProcessError(1, command)
            captured = b''
            if command[-3:] == ['ps', '-q', 'manager']:
                captured = b'manager-container\n'
            elif command[:2] == ['docker', 'inspect']:
                captured = (old_image + '\n').encode()
            elif 'pg_dump' in command:
                kwargs['stdout'].write(b'PGDMP' + b'x' * 30)
            elif command[-5:] == ['-C', '/static', '-cf', '-', '.']:
                kwargs['stdout'].write(b'static-snapshot')
            return subprocess.CompletedProcess(command, 0, stdout=captured)

        with patch.object(updater, 'ROOT', root), patch.object(updater.os, 'geteuid', return_value=0), \
             patch.object(updater, 'check_tip'), patch.object(updater, 'download', side_effect=fake_download), \
             patch.object(updater.subprocess, 'run', side_effect=fake_run), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            previous_umask = updater.os.umask(0o077)
            try:
                if failure:
                    with self.assertRaises(subprocess.CalledProcessError):
                        updater.update(SHA)
                else:
                    updater.update(SHA)
            finally:
                updater.os.umask(previous_umask)
        return root, calls, old_image

    def test_build_failure_does_not_stop_services(self):
        root, calls, _ = self.exercise_update('build')
        self.assertFalse(any('stop' in call for call in calls))
        self.assertFalse((root / 'state/application-image.yaml').exists())

    def test_migration_failure_restores_old_image_without_database_restore(self):
        root, calls, old_image = self.exercise_update('migration')
        self.assertEqual(json.loads((root / 'state/application-image.yaml').read_text())['services']['manager']['image'], old_image)
        self.assertTrue(any('up' in call for call in calls))
        self.assertFalse(any('pg_restore' in call or 'down' in call for call in calls))
        self.assertFalse((root / 'state/deployed-commit.json').exists())

    def test_success_backs_up_before_migrating_and_only_starts_apps(self):
        root, calls, _ = self.exercise_update()
        backup = next(i for i, call in enumerate(calls) if 'pg_dump' in call)
        migrate = next(i for i, call in enumerate(calls) if 'run' in call and call[-1] == 'init')
        self.assertLess(backup, migrate)
        for call in calls:
            if 'up' in call:
                self.assertIn('--no-deps', call)
                self.assertEqual(call[-4:], list(updater.APPS))
        self.assertEqual(json.loads((root / 'state/deployed-commit.json').read_text())['sha'], SHA)


if __name__ == '__main__':
    unittest.main()
