#!/usr/bin/env python3
"""Package tested, explicit runtime inputs. Never include live data or dotenv files."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from remote import sha256_file

INPUTS = {
    'fastcas': ['bin/fastcas', 'web/dist'],
    'fasttask': ['bin/fasttask', 'web/dist', 'web/node_modules', 'sidecar/dist'],
    'fastwrite': ['apps/server/dist', 'apps/web/dist', 'package.json'],
    'fastread': ['backend/main.py', 'backend/app', 'fastread-frontend/dist', 'vendor'],
    'fastinsight': ['fastinsight', 'feishu_bot.py', 'scripts', 'assets', 'vendor'],
    'fastlabs': ['server.py', 'agent_adapter.py', 'fastcas_pairing.py', 'feishu_gateway.py',
                 'feishu_help.py', 'web', 'skills', 'schemas', 'vendor'],
    'fastfigure': ['server-dist', 'dist', 'package.json'],
}


def ignored(directory, names):
    return [name for name in names if name in {'.git', '__pycache__', '.fastdeploy', '.env'}
            or name.startswith('.env.') or name.endswith(('.pyc', '.test.ts', '.test.mjs'))]


def copy(source, destination):
    if source.is_dir():
        shutil.copytree(source, destination, ignore=ignored, symlinks=False)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', choices=[*INPUTS, 'fastnews'])
    parser.add_argument('--source', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fastdeploy-package-') as temporary:
        stage = Path(temporary)
        if args.app == 'fastnews':
            tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode().split('\0')
            for name in tracked:
                parts = Path(name).parts
                # Generated reports, article databases and personal data stay on the server.
                if not parts or parts[0].startswith('.') or 'data' in parts or name.startswith('test'):
                    continue
                if Path(name).suffix.lower() in {'.py', '.html', '.css', '.js', '.svg', '.png', '.ico', '.txt'} or parts[0] == 'prompt':
                    copy(source / name, stage / 'server' / name)
            copy(source / 'vendor', stage / 'vendor')
        else:
            for name in INPUTS[args.app]:
                copy(source / name, stage / name)
        if args.app in {'fastwrite', 'fastfigure'}:
            # Copy the reachable package graph once, without pnpm's cyclic store links.
            modules = source / ('apps/server/node_modules' if args.app == 'fastwrite' else 'node_modules')
            copied = set()
            def package(name, parent, optional=False):
                if name in copied:
                    return
                bases = [parent / 'node_modules', *[path / 'node_modules' for path in parent.parents], modules]
                found = next((base / name for base in bases if (base / name / 'package.json').exists()), None)
                if found is None:
                    if optional:
                        return
                    raise ValueError('missing production Node dependency: ' + name)
                found = found.resolve()
                copied.add(name)
                copy(found, stage / 'node_modules' / name)
                metadata = json.loads((found / 'package.json').read_text())
                for dependency in metadata.get('dependencies', {}):
                    package(dependency, found)
                for dependency in metadata.get('optionalDependencies', {}):
                    package(dependency, found, optional=True)
            names = ['pdfjs-dist'] if args.app == 'fastwrite' else json.loads((source / 'package.json').read_text())['dependencies']
            for name in names:
                package(name, modules.parent)
        archive = args.output / 'release.tar.gz'
        with tarfile.open(archive, 'w:gz', dereference=True) as tar:
            for item in sorted(stage.iterdir()):
                tar.add(item, arcname=item.name)
        digest = sha256_file(archive)
        manifest = {'repository': os.environ['GITHUB_REPOSITORY'], 'sha': os.environ['GITHUB_SHA'],
                    'archive_sha256': digest}
        (args.output / 'manifest.json').write_text(json.dumps(manifest) + '\n')
        print(f'Packaged {args.app}: {archive.stat().st_size // 1024**2} MiB')


if __name__ == '__main__':
    main()
