import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote import restore_runtime_links


class RuntimeLinksTests(unittest.TestCase):
    def fixture(self, entries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'packages/a').mkdir(parents=True)
        (root / 'packages/a/package.json').write_text('{}')
        (root / 'runtime-links.json').write_text(json.dumps(entries))
        return root

    def test_relative_dependency_link_preserves_real_package(self):
        root = self.fixture([{'path': 'node_modules/@app/a', 'target': 'packages/a'}])
        restore_runtime_links(root, 'runtime-links.json')
        self.assertEqual((root / 'node_modules/@app/a').resolve(), root / 'packages/a')

    def test_escape_missing_target_private_path_and_chains_fail_before_writing(self):
        cases = [
            [{'path': 'node_modules/a', 'target': '../outside'}],
            [{'path': 'node_modules/a', 'target': '/etc/passwd'}],
            [{'path': 'node_modules/a', 'target': '.env'}],
            [{'path': 'node_modules/a', 'target': 'missing'}],
            [{'path': 'node_modules/a', 'target': 'packages/a'}, {'path': 'node_modules/a/nested', 'target': 'packages/a'}],
            [{'path': 'node_modules/a', 'target': 'packages/a'}, {'path': 'node_modules/b', 'target': 'node_modules/a'}],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                root = self.fixture(entries)
                with self.assertRaises(ValueError):
                    restore_runtime_links(root, 'runtime-links.json')
                self.assertFalse((root / 'node_modules').exists())
