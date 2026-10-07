import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from realtime_subtitle.config import load_config
from realtime_subtitle.gguf import LlamaServer, SCHEMA
from realtime_subtitle.translator import QwenTranslator


class StructuredTranslationTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / 'config.toml').translation
        self.config.backend = 'llama_cpp'
        self.server = LlamaServer(self.config, ROOT)

    def test_schema_is_sent_and_only_translation_field_returned(self):
        self.server.request = Mock(return_value={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'translation': '请关门。'}, ensure_ascii=False)}}]})
        output = self.server.translate('Please close the door.', 'en_to_zh', 128)
        self.assertEqual(output, '请关门。')
        _, payload = self.server.request.call_args.args
        self.assertEqual(payload['response_format']['json_schema']['schema'], SCHEMA)
        self.assertTrue(payload['response_format']['json_schema']['strict'])
        self.assertFalse(payload['chat_template_kwargs']['enable_thinking'])

    def test_malformed_extra_fields_and_truncated_output_rejected(self):
        for content, reason in [('not JSON', 'stop'), ('{"label":"hello"}', 'stop'),
                                ('{"translation":"ok","label":"bad"}', 'stop'),
                                ('{"translation":5}', 'stop'), ('{"translation":"ok"}', 'length')]:
            self.server.request = Mock(return_value={'choices': [{'finish_reason': reason,
                                                                'message': {'content': content}}]})
            with self.assertRaises(ValueError):
                self.server.translate('hello', 'en_to_zh', 128)

    def test_wrong_language_repaired_once_without_leaking_labels(self):
        t = QwenTranslator(self.config, ROOT / 'cache/huggingface', lambda _: None)
        t._server = SimpleNamespace(translate=Mock(side_effect=['We should close the door.', '译文：请关门。']))
        self.assertEqual(t.translate('Please close the door.', 'en', mode='en_to_zh'), '请关门。')
        self.assertEqual(t._server.translate.call_count, 2)
        self.assertTrue(t._server.translate.call_args.kwargs['strict'])

    def test_length_retry_gets_a_larger_budget(self):
        t = QwenTranslator(self.config, ROOT / 'cache/huggingface', lambda _: None)
        t.config.max_new_tokens = 128
        t._server = SimpleNamespace(translate=Mock(
            side_effect=[ValueError('Translation exceeded token budget'), '请关门。']))
        self.assertEqual(t.translate('Please close the door.', 'en', mode='en_to_zh'), '请关门。')
        calls = t._server.translate.call_args_list
        self.assertEqual(calls[0].args[2], 128)
        self.assertEqual(calls[1].args[2], 256)
        self.assertTrue(calls[1].kwargs['strict'])

    def test_adaptive_source_uses_selected_target_language(self):
        t = QwenTranslator(self.config, ROOT / 'cache/huggingface', lambda _: None)
        t._server = SimpleNamespace(translate=Mock(return_value='这是一个测试。'))
        self.assertEqual(t.translate('This is a test.', 'es', mode='auto_to_zh'), '这是一个测试。')
        self.assertEqual(t._server.translate.call_args.args[:3], ('This is a test.', 'auto_to_zh', self.config.max_new_tokens))

    def test_repair_is_bounded_and_invalid_result_not_returned(self):
        t = QwenTranslator(self.config, ROOT / 'cache/huggingface', lambda _: None)
        t._server = SimpleNamespace(translate=Mock(return_value='<translation>bad</translation>'))
        with self.assertRaises(ValueError):
            t.translate('Please close the door.', 'en', mode='en_to_zh')
        self.assertEqual(t._server.translate.call_count, 2)

    def test_worker_continues_after_failed_translation(self):
        result = threading.Event()
        errors, outputs = [], []
        def publish(value):
            outputs.append(value)
            result.set()
        t = QwenTranslator(self.config, ROOT / 'cache/huggingface', publish, errors.append)
        t.translate = Mock(side_effect=[ValueError('bad output'), 'A good translation'])
        t._thread = threading.Thread(target=t._run)
        t.submit('first', 'zh', 1)
        t.submit('second', 'zh', 2)
        t._thread.start()
        try:
            self.assertTrue(result.wait(3))
        finally:
            t.stop()
        self.assertEqual(outputs[0].caption_id, 2)
        self.assertEqual(len(errors), 1)


if __name__ == '__main__':
    unittest.main()
