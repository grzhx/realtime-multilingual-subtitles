import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from realtime_subtitle.config import (languages_from_mode, load_saved_settings,
                                      mode_from_languages, save_settings)


class SettingsPersistenceTest(unittest.TestCase):
    def test_language_mode_mapping_and_legacy_compatibility(self):
        self.assertEqual(mode_from_languages('auto', 'zh'), 'auto_to_zh')
        self.assertEqual(mode_from_languages('en', 'none'), 'off')
        self.assertEqual(languages_from_mode('zh_to_en'), ('zh', 'en'))
        self.assertEqual(languages_from_mode('off'), ('auto', 'none'))

    def test_settings_are_saved_atomically_and_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache' / 'subtitle_settings.json'
            value = {'source_language': 'auto', 'target_language': 'en', 'bilingual': False}
            save_settings(path, value)
            self.assertEqual(load_saved_settings(path), value)
            self.assertFalse(list(path.parent.glob('.subtitle-settings-*.tmp')))

    def test_corrupt_settings_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(load_saved_settings(path), {})


if __name__ == '__main__':
    unittest.main()
