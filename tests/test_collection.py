"""Collection checks reject broken metadata, resources and portability regressions."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from validate_collection import validate_repository


class CollectionValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill = self.root / 'skills' / 'example'
        self.skill.mkdir(parents=True)
        (self.root / 'README.md').write_text('[Example](skills/example/README.md)\n')
        (self.skill / 'SKILL.md').write_text('---\nname: example\ndescription: Exercise a portable example workflow.\nlicense: MIT\n---\n\n# Example\n\n[Instructions](README.md)\n')
        (self.skill / 'README.md').write_text('# Example\n\nUse this example.\n')
        (self.skill / 'LICENSE').write_text('MIT License\n')

    def test_valid_collection(self):
        self.assertEqual(validate_repository(self.root), [])

    def test_invalid_standard_metadata(self):
        p = self.skill / 'SKILL.md'
        p.write_text(p.read_text().replace('name: example', 'name: Wrong_Name'))
        self.assertTrue(validate_repository(self.root))

    def test_missing_skill_license(self):
        (self.skill / 'LICENSE').unlink()
        self.assertTrue(any('LICENSE' in e for e in validate_repository(self.root)))

    def test_broken_relative_link(self):
        (self.skill / 'README.md').write_text('[Missing](references/missing.md)\n')
        self.assertTrue(any('missing' in e for e in validate_repository(self.root)))

    def test_missing_anchor(self):
        (self.skill / 'README.md').write_text('# Example\n\n[Section](#absent)\n')
        self.assertTrue(any('anchor' in e for e in validate_repository(self.root)))

    def test_installed_skill_cannot_depend_on_collection_files(self):
        (self.skill / 'README.md').write_text('[Outside](../../README.md)\n')
        self.assertTrue(any('outside skill' in e for e in validate_repository(self.root)))

    def test_symlinked_resource_is_rejected(self):
        (self.skill / 'linked.md').symlink_to(self.root / 'README.md')
        self.assertTrue(any('symlink' in e for e in validate_repository(self.root)))

    def test_machine_specific_home_path_is_rejected(self):
        path = '/' + 'home' + '/someone/private/scene.py'
        (self.skill / 'README.md').write_text('Run ' + path + '\n')
        self.assertTrue(any('machine-specific' in e for e in validate_repository(self.root)))

    def test_credential_pattern_is_rejected(self):
        token = 'gh' + 'p_' + 'a' * 36
        (self.skill / 'README.md').write_text(token + '\n')
        self.assertTrue(any('credential' in e for e in validate_repository(self.root)))

    def test_validator_source_does_not_trigger_its_own_patterns(self):
        import shutil
        target = self.root / 'tools' / 'validate_collection.py'
        target.parent.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'tools' / 'validate_collection.py', target)
        self.assertEqual(validate_repository(self.root), [])

    def test_catalog_must_link_each_skill(self):
        (self.root / 'README.md').write_text('# Empty catalog\n')
        self.assertTrue(any('catalog' in e for e in validate_repository(self.root)))

    def test_empty_collection_cannot_pass(self):
        import shutil
        shutil.rmtree(self.skill)
        self.assertTrue(any('No skills' in e for e in validate_repository(self.root)))

    def test_ignored_private_file_cannot_hide_from_source_tree_review(self):
        (self.skill / '.env').write_text('PRIVATE_SETTING=value\n')
        self.assertTrue(any('private local file' in e for e in validate_repository(self.root)))


if __name__ == '__main__':
    unittest.main()
