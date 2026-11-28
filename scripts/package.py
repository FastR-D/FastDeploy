#!/usr/bin/env python3
"""Package tested, explicit runtime inputs. Never include live data or dotenv files."""
import argparse
import json
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from remote import sha256_file

INPUTS = {
    'fastnews': ['bin/fastnews', 'web/dist'],
    'fastcas': ['bin/fastcas', 'web/dist'],
    'fasttask': ['bin/fasttask', 'web/dist', 'web/node_modules', 'sidecar/dist'],
    'fastwrite': ['apps/server/dist', 'apps/web/dist', 'package.json'],
    'fastread': ['bin/fastread', 'bin/pdftotext', 'bin/pdfinfo', 'lib', 'fastread-frontend/dist'],
    'fastinsight': ['fastinsight', 'feishu_bot.py', 'scripts', 'assets', 'vendor'],
    'fastlabs': ['server.py', 'agent_adapter.py', 'fastcas_pairing.py', 'feishu_gateway.py',
                 'feishu_help.py', 'web', 'skills', 'schemas', 'vendor'],
    'fastfigure': ['bin/fastfigure', 'dist'],
    'fastppt': ['bin/fastppt', 'apps/web/dist', 'apps/gateway', 'apps/render-engine', 'packages', 'themes', 'node_modules', 'package.json', 'pnpm-workspace.yaml', 'tsconfig.base.json'],
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


def package_ppt(source, archive):
    # Stream large font/resource trees; a second expanded staging copy is not
    # needed. Link targets retain their exact pnpm version directories.
    entries, links = {}, []
    def visit(origin, name):
        if origin.is_symlink():
            if origin.exists():
                target = str(origin.resolve().relative_to(source))
                links.append({'path': name, 'target': target})
            return
        if name in entries:
            return
        entries[name] = origin
        if origin.is_dir():
            children = list(origin.iterdir())
            skip = set(ignored(str(origin), [p.name for p in children])) | {'coverage', '.turbo', '.vite', 'tests', 'test', '__tests__', '.fastppt', '.fastppt-data'}
            for child in children:
                if child.name not in skip:
                    visit(child, name + '/' + child.name)
    for name in INPUTS['fastppt']:
        visit(source / name, 'dist' if name == 'apps/web/dist' else name)
    # Workspace test tools are not shipped; omit only links to omitted trees.
    links = [link for link in links if link['target'] in entries]
    payload = json.dumps(links).encode()
    with tarfile.open(archive, 'w:gz', dereference=True, compresslevel=1) as tar:
        for name, origin in sorted(entries.items()):
            tar.add(origin, arcname=name, recursive=False)
        info = tarfile.TarInfo('runtime-links.json')
        info.size = len(payload)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(payload))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', choices=list(INPUTS))
    parser.add_argument('--source', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.app == 'fastppt':
        archive = args.output / 'release.tar.gz'
        package_ppt(source, archive)
        manifest = {'repository': os.environ['GITHUB_REPOSITORY'], 'sha': os.environ['GITHUB_SHA'], 'archive_sha256': sha256_file(archive)}
        (args.output / 'manifest.json').write_text(json.dumps(manifest) + '\n')
        print(f'Packaged fastppt: {archive.stat().st_size // 1024**2} MiB')
        return
    with tempfile.TemporaryDirectory(prefix='fastdeploy-package-') as temporary:
        stage = Path(temporary)
        for name in INPUTS[args.app]:
            copy(source / name, stage / name)
        if args.app == 'fastwrite':
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
        with tarfile.open(archive, 'w:gz', dereference=True, compresslevel=1) as tar:
            for item in sorted(stage.iterdir()):
                tar.add(item, arcname=item.name)
        digest = sha256_file(archive)
        manifest = {'repository': os.environ['GITHUB_REPOSITORY'], 'sha': os.environ['GITHUB_SHA'],
                    'archive_sha256': digest}
        (args.output / 'manifest.json').write_text(json.dumps(manifest) + '\n')
        print(f'Packaged {args.app}: {archive.stat().st_size // 1024**2} MiB')


if __name__ == '__main__':
    main()
