import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from remote import Deployment, extract_archive, switch_link, write_json
from publication import publish, restore_public, snapshot_public

ROOT = Path(__file__).resolve().parent.parent


class Controller:
    def __init__(self):
        self.active = set()
        self.enabled = set()
        self.fail = None

    def legacy_active(self):
        return False

    def state(self, operation, unit):
        return unit in (self.active if operation == 'is-active' else self.enabled)

    def run(self, operation, *args):
        if operation == 'stop':
            self.active.discard(args[0])
        elif operation == 'disable':
            self.enabled.discard(args[0])
        elif operation == 'enable':
            self.enabled.add(args[0])

    def restart(self, unit):
        if self.fail == unit:
            self.fail = None
            raise RuntimeError('simulated worker failure')
        self.active.add(unit)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bundle = self.home / 'bundle'
        self.bundle.mkdir()
        (self.bundle / 'launch.py').write_bytes((ROOT / 'scripts/launch.py').read_bytes())
        with tarfile.open(self.bundle / 'release.tar.gz', 'w:gz') as tar:
            data = b'pass'
            info = tarfile.TarInfo('backend/main.py'); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        profile = {'repository': 'FastR-D/FastRead', 'legacy_app': 'read',
            'unit': 'api.service', 'command': ['/usr/bin/python3', 'backend/main.py'],
            'services': [{'unit': 'api.service', 'command': ['/usr/bin/python3', 'main.py'], 'cwd': '{release}/backend'},
                         {'unit': 'worker.service', 'command': ['/usr/bin/python3', 'backend/main.py']}],
            'health_url': 'http://127.0.0.1/health', 'shared_paths': [],
            'required_files': ['backend/main.py'], 'artifact_roots': ['backend'],
            'required_env': [], 'fixed_env': {},
            'release_env': {'PYTHONPATH': '{release}/vendor'},
            'runtime_check': [sys.executable, '-c', 'pass']}
        self.request = {'app': 'fastread', 'profile': profile, 'repository': profile['repository'],
            'tag': 'v2.0.0', 'sha': 'a'*40, 'run_id': 20,
            'archive_sha256': hashlib.sha256((self.bundle / 'release.tar.gz').read_bytes()).hexdigest(),
            'env': {'PASSWORD': 'literal {release} $(echo nope)'}}
        self.controller = Controller()
        self.dep = Deployment(self.home, self.request, self.controller, lambda url: None)

    def test_group_publish_rollback_and_first_install_cleanup(self):
        self.dep.deploy(self.bundle)
        self.assertEqual(self.controller.active, {'api.service', 'worker.service'})
        self.assertEqual(self.controller.enabled, self.controller.active)
        runtime = json.loads((self.dep.current / '.fastdeploy/runtime.json').read_text())
        self.assertEqual(runtime['env']['PASSWORD'], self.request['env']['PASSWORD'])
        self.assertEqual(runtime['services']['api.service']['cwd'], str(self.dep.current.resolve() / 'backend'))
        self.assertEqual(runtime['env']['PYTHONPATH'], str(self.dep.current.resolve() / 'vendor'))
        name = json.loads(self.dep.active.read_text())['release']
        self.dep.rollback(name)
        self.assertFalse(self.dep.current.exists())
        self.assertFalse(self.controller.active)
        self.assertFalse(self.controller.enabled)
        self.assertFalse(self.dep.unit_path({'unit':'api.service'}).exists())

    def test_second_service_failure_restores_entire_group(self):
        self.controller.fail = 'worker.service'
        with self.assertRaises(RuntimeError):
            self.dep.deploy(self.bundle)
        self.assertFalse(self.controller.active)
        self.assertFalse(self.dep.transaction.exists())
        self.assertFalse(self.dep.active.exists())

    def test_existing_units_and_configuration_are_restored(self):
        for service in self.dep.services:
            path = self.dep.unit_path(service); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('old unit ' + service['unit'])
            self.controller.active.add(service['unit'])
        self.controller.enabled.add('api.service')
        before = self.dep.snapshot()
        self.dep.deploy(self.bundle)
        self.dep.rollback(json.loads(self.dep.active.read_text())['release'])
        self.assertEqual(self.dep.snapshot(), before)

    def test_root_units_are_only_retired_inside_activation(self):
        self.dep.profile['system_legacy'] = 'fastread'
        self.controller.fail = 'worker.service'
        with patch('remote.subprocess.run') as run:
            with self.assertRaises(RuntimeError):
                self.dep.deploy(self.bundle)
        operations = [call.args[0][-2:] for call in run.call_args_list if call.args[0][0]=='sudo']
        self.assertEqual(operations, [['quiesce','fastread'], ['restore','fastread']])

    def test_each_profile_has_matching_repository_and_safe_packaging(self):
        profiles = json.loads((ROOT / 'config/apps.json').read_text())
        self.assertEqual(len(profiles), 9)
        self.assertNotIn('fastui', profiles)
        for app, profile in profiles.items():
            self.assertEqual(profile['repository'].split('/')[1].lower(), app)
            self.assertTrue(all(not path.startswith('/') and '..' not in Path(path).parts for path in profile['required_files']))
            if app != 'fastresearch':
                self.assertTrue(profile['services'])
                for required in profile['required_files']:
                    self.assertIn(Path(required).parts[0], profile['artifact_roots'])


class PublicFilesTests(unittest.TestCase):
    def test_both_nginx_paths_restore_and_old_assets_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'public'; root.mkdir()
            (root / 'Read').symlink_to('.')
            (root / 'index.html').write_text('old index')
            (root / 'vendor/pdfjs').mkdir(parents=True)
            (root / 'vendor/pdfjs/worker.js').write_text('old worker')
            (root / 'assets').mkdir()
            (root / 'assets/old-hash.js').write_text('old client')
            release = Path(temporary) / 'release'
            frontend = release / 'frontend'; (frontend / 'vendor/pdfjs').mkdir(parents=True)
            (frontend / 'index.html').write_text('new index')
            (frontend / 'vendor/pdfjs/worker.js').write_text('new worker')
            (frontend / 'assets').mkdir(); (frontend / 'assets/new-hash.js').write_text('new client')
            config = {'root': str(root), 'source': 'frontend'}
            old = snapshot_public(config)
            write_json(release / '.fastdeploy/previous.json', {'public': old})
            publish(config, release, 'release-one')
            self.assertEqual((root / 'Read/index.html').read_text(), 'new index')
            self.assertEqual((root / 'index.html').read_text(), 'new index')
            self.assertEqual((root / 'Read/assets/old-hash.js').read_text(), 'old client')
            self.assertEqual((root / 'vendor/pdfjs/worker.js').read_text(), 'new worker')
            current = snapshot_public(config)
            restore_public(config, old)
            self.assertEqual((root / 'Read/index.html').read_text(), 'old index')
            self.assertFalse((root / 'vendor').is_symlink())
            self.assertEqual((root / 'vendor/pdfjs/worker.js').read_text(), 'old worker')
            # A failed rollback can return to the active publication too.
            restore_public(config, current)
            self.assertEqual((root / 'vendor/pdfjs/worker.js').read_text(), 'new worker')
            restore_public(config, old)
            self.assertEqual((root / 'index.html').read_text(), 'old index')

    def test_profile_allowlist_cannot_admit_live_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            archive = path / 'bad.tgz'
            with tarfile.open(archive, 'w:gz') as tar:
                info=tarfile.TarInfo('data/business.sqlite');info.size=1;tar.addfile(info,io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                extract_archive(archive,path/'out',['backend','vendor'])
            self.assertFalse((path/'out').exists())


class PackageTests(unittest.TestCase):
    def test_pnpm_dependency_cycle_is_flattened_and_links_are_absent(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            (source / 'server-dist').mkdir(); (source / 'server-dist/server.mjs').write_text('// server')
            (source / 'dist').mkdir(); (source / 'dist/index.html').write_text('UI')
            write_json(source / 'package.json', {'dependencies': {'a': '1'}})
            modules = source / 'node_modules'; modules.mkdir()
            for name, dependency in [('a', 'b'), ('b', 'a')]:
                location = modules / '.pnpm' / name / 'node_modules' / name
                location.mkdir(parents=True)
                write_json(location / 'package.json', {'name': name, 'dependencies': {dependency: '1'}})
                (location / 'index.js').write_text(name)
                (modules / name).symlink_to(location)
            for name, dependency in [('a', 'b'), ('b', 'a')]:
                (modules / '.pnpm' / name / 'node_modules' / dependency).symlink_to(modules / dependency)
            environment = dict(os.environ, GITHUB_REPOSITORY='FastR-D/FastFigure', GITHUB_SHA='a'*40)
            subprocess.run([sys.executable, str(ROOT / 'scripts/package.py'), 'fastfigure', '--source', str(source),
                '--output', str(root / 'out')], env=environment, check=True, capture_output=True)
            with tarfile.open(root / 'out/release.tar.gz') as archive:
                names = archive.getnames()
                self.assertIn('node_modules/a/index.js', names)
                self.assertIn('node_modules/b/index.js', names)
                self.assertFalse(any(member.issym() or member.islnk() or '.pnpm' in member.name for member in archive.getmembers()))


class StartupTests(unittest.TestCase):
    def test_pre_start_receives_exact_environment_before_application_exec(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); settings = root / '.fastdeploy'; settings.mkdir()
            (settings / 'launch.py').write_bytes((ROOT / 'scripts/launch.py').read_bytes())
            marker = root / 'migration.txt'
            environment = {'PASSWORD': 'literal $ \nvalue'}
            write_json(settings / 'runtime.json', {'env': environment, 'command': [],
                'services': {'test.service': {'cwd': str(root), 'pre_start': [[sys.executable, '-c',
                    'import os,pathlib;pathlib.Path("migration.txt").write_text(os.environ["PASSWORD"])']],
                    'command': [sys.executable, '-c', 'import pathlib;assert pathlib.Path("migration.txt").exists()']}}})
            result = subprocess.run([sys.executable, str(settings / 'launch.py'), 'test.service'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(), environment['PASSWORD'])
            self.assertNotIn(environment['PASSWORD'], result.stdout + result.stderr)

    def test_failed_migration_never_starts_application(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); settings = root / '.fastdeploy'; settings.mkdir()
            (settings / 'launch.py').write_bytes((ROOT / 'scripts/launch.py').read_bytes())
            write_json(settings / 'runtime.json', {'env': {}, 'pre_start': [[sys.executable, '-c', 'raise SystemExit(1)']],
                'command': [sys.executable, '-c', 'import pathlib;pathlib.Path("started").touch()']})
            result = subprocess.run([sys.executable, str(settings / 'launch.py')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'started').exists())
