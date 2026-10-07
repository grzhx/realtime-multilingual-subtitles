"""Regression checks using real Tk/Win32 windows and a fake translation engine."""
import sys
import ctypes
from ctypes import wintypes
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from realtime_subtitle.config import load_config
from realtime_subtitle.overlay import HistoryCaption, SubtitleOverlay, OverlayMessage
from realtime_subtitle.translator import QwenTranslator, TranslationResult
from realtime_subtitle.windows import TRANSPARENT, NOACTIVATE
from realtime_subtitle.app import Application

ROOT = Path(__file__).resolve().parents[1]


class TranslatorRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.translator = QwenTranslator(load_config(ROOT / 'config.toml').translation,
                                         ROOT / 'cache', self.results.append)

    def tearDown(self):
        self.translator.stop()

    def test_off_then_on_and_bilingual_does_not_invalidate_translation(self):
        t = self.translator
        self.assertEqual(t.configure('off'), 1)
        t.submit('ignored', 'en', 1)
        self.assertTrue(t._queue.empty())
        self.assertEqual(t.configure('en_to_zh', False), 2)
        t.submit('hello', 'en', 2)
        self.assertEqual(t.configure('en_to_zh', True), 2)
        self.assertEqual(t._queue.get_nowait(), ('hello', 'en', 2, 'en_to_zh', 2))

    def test_direction_change_discards_inflight_and_accepts_new_result(self):
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        t = self.translator
        def translate(text, language, mode=None):
            if text == 'old':
                entered.set()
                release.wait(3)
            return mode + ':' + text
        def publish(result):
            self.results.append(result)
            published.set()
        t.on_translation = publish
        with patch.object(t, '_load_model'), patch.object(t, 'translate', translate):
            t.start()
            t.submit('old', 'zh', 1)
            self.assertTrue(entered.wait(2))
            generation = t.configure('en_to_zh')
            t.submit('new', 'en', 2)
            release.set()
            self.assertTrue(published.wait(2))
        self.assertEqual(self.results, [TranslationResult('en_to_zh:new', 2, generation, 'new')])

    def test_loading_does_not_block_calling_thread(self):
        loading, release = threading.Event(), threading.Event()
        def load():
            loading.set()
            release.wait(3)
        with patch.object(self.translator, '_load_model', load):
            self.translator.start()
            self.assertTrue(loading.wait(1))
            self.assertFalse(self.translator._ready.is_set())
            self.translator.configure('off')
            release.set()

    def test_prompt_requires_target_language_even_for_wrong_source(self):
        system, user = self.translator._translation_prompt('We need help.', 'en_to_zh')
        self.assertIn('Simplified Chinese', system)
        self.assertEqual(user, 'We need help.')
        self.assertIn('already in the target language', system)
        system, user = self.translator._translation_prompt('需要帮助。', 'zh_to_en')
        self.assertIn('English', system)
        self.assertEqual(user, '需要帮助。')

    def test_labels_removed_and_old_context_never_in_prompt(self):
        t = self.translator
        for prefix in ('当前标题：', '当前字幕:', '译文：', 'Current subtitle:', 'Translation:'):
            self.assertEqual(t._clean_output(prefix + '我们需要帮助。'), '我们需要帮助。')
        self.assertEqual(t._clean_output('Translation: 当前标题：需要帮助。'), '需要帮助。')
        self.assertEqual(t._clean_output('报告里写着当前标题：你好。'), '报告里写着当前标题：你好。')
        _, user = t._translation_prompt('New line.', 'en_to_zh', 'OLD ENGLISH TRANSLATION')
        self.assertEqual(user, 'New line.')

    def test_backlog_keeps_latest_pending_task_after_loading(self):
        t = self.translator
        t.configure('off')
        t.configure('en_to_zh')
        for i in range(3):
            t.submit(f'Line {i}', 'en', i)
        queued = [t._queue.get_nowait()[0] for _ in range(t._queue.qsize())]
        self.assertEqual(queued, [f'Line {i}' for i in range(3)])

    def test_preview_queue_keeps_only_newest_provisional_request(self):
        t = self.translator
        t.configure('en_to_zh')
        t.submit_preview('We need', 'en', 1, 1)
        t.submit_preview('We need to review', 'en', 1, 2)
        self.assertEqual(t._preview_queue.qsize(), 1)
        self.assertEqual(t._preview_queue.get_nowait()[0], 'We need to review')

    def test_new_preview_invalidates_inflight_old_revision(self):
        t = self.translator
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        def translate(text, language, mode=None):
            if text == 'old':
                entered.set()
                release.wait(3)
            return text
        def publish(result):
            self.results.append(result)
            published.set()
        t.on_translation = publish
        with patch.object(t, '_load_model'), patch.object(t, 'translate', translate):
            t.start()
            t.submit_preview('old', 'en', 1, 1)
            self.assertTrue(entered.wait(2))
            t.submit_preview('new complete prefix', 'en', 1, 2)
            release.set()
            self.assertTrue(published.wait(2))
        self.assertEqual([result.text for result in self.results], ['new complete prefix'])

    def test_final_submission_invalidates_inflight_preview(self):
        t = self.translator
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        def translate(text, language, mode=None):
            if text == 'preview':
                entered.set()
                release.wait(3)
            return text
        def publish(result):
            self.results.append(result)
            published.set()
        t.on_translation = publish
        with patch.object(t, '_load_model'), patch.object(t, 'translate', translate):
            t.start()
            t.submit_preview('preview', 'en', 1, 1)
            self.assertTrue(entered.wait(2))
            t.invalidate_previews()
            t.submit('complete sentence', 'en', 1)
            release.set()
            self.assertTrue(published.wait(2))
        self.assertEqual([result.text for result in self.results], ['complete sentence'])


@unittest.skipUnless(sys.platform == 'win32', 'Windows overlay tests')
class OverlayRuntimeTest(unittest.TestCase):
    def setUp(self):
        c = load_config(ROOT / 'config.toml')
        self.overlay = SubtitleOverlay(c.ui, lambda: None, translation=c.translation)
        self.pointer_patch = patch.object(self.overlay.root, 'winfo_pointerxy',
                                          side_effect=lambda: (self.overlay.x + 5, self.overlay.y + 5))
        self.pointer_patch.start()
        self.overlay.root.update()

    def tearDown(self):
        self.pointer_patch.stop()
        self.overlay._close()

    def assert_layer_order(self):
        o = self.overlay
        api = o._styles.api
        api.GetTopWindow.argtypes = [wintypes.HWND]
        api.GetTopWindow.restype = wintypes.HWND
        api.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        api.GetWindow.restype = wintypes.HWND
        api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        api.GetWindowRect.restype = wintypes.BOOL
        for layer in (o.root, o.background):
            rect = wintypes.RECT()
            self.assertTrue(api.GetWindowRect(o._styles.hwnd(layer), ctypes.byref(rect)))
            self.assertEqual((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                             (o.x, o.y, o.width, o.height))
        windows = []
        handle = api.GetTopWindow(None)
        while handle:
            windows.append(handle)
            handle = api.GetWindow(handle, 2)  # GW_HWNDNEXT, top to bottom
        control = windows.index(o._styles.hwnd(o.controls))
        text = windows.index(o._styles.hwnd(o.root))
        background = windows.index(o._styles.hwnd(o.background))
        self.assertLess(control, text, 'Controls must be above subtitle text')
        self.assertLess(text, background, 'Subtitle text must be above background')
        # Hit testing must resolve to the controls, not the background or text.
        api.WindowFromPoint.argtypes = [wintypes.POINT]
        api.WindowFromPoint.restype = wintypes.HWND
        point = wintypes.POINT(o.lock_button.winfo_rootx() + 16,
                               o.lock_button.winfo_rooty() + 16)
        target = api.WindowFromPoint(point)
        self.assertEqual(api.GetAncestor(target, 2), o._styles.hwnd(o.controls))

    def test_native_order_and_button_hit_target_after_layout_changes(self):
        o = self.overlay
        o._source_text = 'Subtitle text must remain above its background.'
        for opacity in (1.0, 0.78, 0.0):
            o.config.background_opacity = opacity
            for locked in (False, True, False):
                if locked != o._locked:
                    o.toggle_lock()
                o._render_text()
                o.root.update()
                self.assert_layer_order()
        o._drag_start(SimpleNamespace(x_root=100, y_root=100))
        o._drag_move(SimpleNamespace(x_root=155, y_root=50))
        o.root.update()
        self.assert_layer_order()

    def test_focus_restack_and_control_commands_remain_accessible(self):
        o = self.overlay
        # Reproduce a background activation moving it ahead of the text.
        o._styles.raise_topmost(o.background)
        o.background.event_generate('<FocusIn>')
        o.root.update()
        self.assert_layer_order()

        o.lock_button.invoke()
        o.root.update()
        self.assertTrue(o._locked)
        self.assert_layer_order()
        o.lock_button.invoke()
        o.root.update()
        self.assertFalse(o._locked)
        o.settings_button.invoke()
        o.root.update()
        self.assertTrue(o._settings_window.winfo_viewable())
        o._settings_window.destroy()
        o._restack_layers()
        o.root.update()
        self.assert_layer_order()

    def test_dropdown_changes_live_translator_without_apply_button(self):
        o = self.overlay
        config = load_config(ROOT / 'config.toml')
        translator = QwenTranslator(config.translation, ROOT / 'cache', o.show_translation)
        app = Application.__new__(Application)
        app.config, app.translator, app.overlay = config, translator, o
        app.asr = SimpleNamespace(configure_translation=lambda *args: None)
        app._subtitle_lock = threading.RLock()
        import logging
        app.logger = logging.getLogger(__name__)
        app._latest_final = ('We need help.', 'en', 7)
        o.on_settings = app._on_settings
        o._translation_text = 'The previous English translation.'
        o.open_settings()
        frame = o._settings_window.winfo_children()[0]
        combo = next(w for w in frame.winfo_children() if isinstance(w, ttk.Combobox))
        combo.set('英译中')
        combo.event_generate('<<ComboboxSelected>>')
        o.root.update()
        self.assertEqual(app.config.translation.mode, 'en_to_zh')
        self.assertEqual(o._mode, 'en_to_zh')
        self.assertEqual(translator._mode, 'en_to_zh')
        self.assertEqual(o._translation_text, '')
        self.assertEqual(translator._queue.get_nowait(), ('We need help.', 'en', 1, 'en_to_zh', 7))
        combo.set('无翻译')
        combo.event_generate('<<ComboboxSelected>>')
        o.root.update()
        self.assertEqual(o._mode, 'off')
        self.assertEqual(translator._mode, 'off')
        self.assertTrue(translator._queue.empty())

    def test_twenty_lock_cycles_restore_input_and_button_rectangle(self):
        o = self.overlay
        o._hovered = True
        o._update_controls()
        o.root.update()
        original = (o.controls.winfo_rootx() + o.lock_button.winfo_x(),
                    o.controls.winfo_rooty() + o.lock_button.winfo_y(),
                    o.lock_button.winfo_width(), o.lock_button.winfo_height())
        for _ in range(20):
            o._lock()
            o.root.update()
            self.assertTrue(o._styles.get(o.root) & TRANSPARENT)
            self.assertTrue(o._styles.get(o.background) & TRANSPARENT)
            self.assertFalse(o.settings_button.winfo_ismapped())
            self.assertFalse(o.close_button.winfo_ismapped())
            rectangle = (o.controls.winfo_rootx() + o.lock_button.winfo_x(),
                         o.controls.winfo_rooty() + o.lock_button.winfo_y(),
                         o.lock_button.winfo_width(), o.lock_button.winfo_height())
            self.assertEqual(original, rectangle)
            self.assertFalse(o._styles.get(o.controls) & TRANSPARENT)
            o._unlock()
            o.root.update()
            self.assertFalse(o._styles.get(o.root) & (TRANSPARENT | NOACTIVATE))
            self.assertFalse(o._styles.get(o.background) & (TRANSPARENT | NOACTIVATE))
            o._hovered = True
            o._update_controls()
            o.root.update()
            self.assertTrue(o.settings_button.winfo_ismapped())

    def test_transparent_background_keeps_visible_text_and_hover_controls(self):
        o = self.overlay
        o.config.background_opacity = 0
        o.config.text_opacity = 1
        o._render_text()
        o.root.update()
        self.assertAlmostEqual(o.background.attributes('-alpha'), 1 / 255, places=2)
        self.assertEqual(o.root.attributes('-alpha'), 1)
        self.assertFalse(o._styles.get(o.background) & TRANSPARENT)
        o._hovered = True
        o._update_controls()
        o.root.update()
        self.assertTrue(o.settings_button.winfo_ismapped())
        o._lock()
        o.root.update()
        self.assertFalse(o.background.winfo_ismapped())
        self.assertTrue(o.controls.winfo_ismapped())
        o._unlock()
        o.root.update()
        self.assertTrue(o.background.winfo_ismapped())

    def test_drag_position_survives_new_subtitle_and_lock(self):
        o = self.overlay
        o._source_text = 'Initial subtitle.'
        o._render_text()
        o.root.update()
        original_x, original_y = o.x, o.y
        o._drag_start(SimpleNamespace(x_root=100, y_root=100))
        o._drag_move(SimpleNamespace(x_root=155, y_root=50))
        o._source_text = 'A new subtitle after dragging.'
        o._render_text()
        self.assertEqual(o.x, original_x + 55)
        self.assertEqual(o.y, original_y - 50)
        o._lock()
        o._drag_move(SimpleNamespace(x_root=0, y_root=0))
        self.assertEqual(o.x, original_x + 55)

    def test_window_edge_resize_increases_width_and_reflows_text(self):
        o = self.overlay
        original_width, original_x = o.width, o.x
        o._drag_start(SimpleNamespace(x_root=original_x + original_width - 2,
                                      y_root=o.y + 80))
        o._drag_move(SimpleNamespace(x_root=original_x + original_width + 180,
                                     y_root=o.y + 80))
        o._drag_end(None)
        self.assertGreater(o.width, original_width)
        self.assertEqual(o.x, original_x)
        o._drag_start(SimpleNamespace(x_root=o.x + 2, y_root=o.y + 80))
        o._drag_move(SimpleNamespace(x_root=o.x - 80, y_root=o.y + 80))
        o._drag_end(None)
        self.assertGreater(o.width, original_width + 180)
        self.assertLessEqual(o.x, original_x)

    def test_current_line_anchor_and_controls_follow_height_changes(self):
        o = self.overlay
        o._source_text = 'Current line stays anchored.'
        o._render_text()
        o.root.update()
        # Leave space below the anchor for translated history; screen-edge
        # clamping is covered independently by the long-text fitting test.
        o._drag_start(SimpleNamespace(x_root=o.x + 100, y_root=o.y + 50))
        o._drag_move(SimpleNamespace(x_root=o.x + 100, y_root=o.y - 150))
        o._drag_end(None)
        o.root.update()
        first_bottom = o.source.winfo_rooty() + o.source.winfo_height()
        first_controls_y = o.controls.winfo_rooty()
        first_top = o.y
        o._history_enabled = True
        o._history_display_seconds = 30
        o._history.append(HistoryCaption(1, 'Older line one.',
                                         'Older translation one.', 9999999999))
        o._history.append(HistoryCaption(2, 'Older line two.',
                                         'Older translation two.', 9999999999))
        o._render_text()
        o.root.update()
        second_bottom = o.source.winfo_rooty() + o.source.winfo_height()
        self.assertAlmostEqual(first_bottom, second_bottom, delta=3)
        self.assertLess(o.y, first_top)
        self.assertGreaterEqual(o.controls.winfo_rooty(), o.y)
        self.assertEqual(first_controls_y, o.controls.winfo_rooty())
        self.assert_layer_order()
        o._hovered = True
        o._update_controls()
        o.root.update()
        self.assertLessEqual(o.source.winfo_rootx() + o.source.winfo_width(),
                             o.controls.winfo_rootx())
        o._translation_text = 'A translated current line.'
        o._render_text()
        o.root.update()
        self.assertAlmostEqual(first_bottom, o.source.winfo_rooty() + o.source.winfo_height(), delta=3)
        self.assertEqual(first_controls_y, o.controls.winfo_rooty())
        for locked in (True, False):
            o.toggle_lock()
            o.root.update()
            self.assertEqual(o._locked, locked)
            self.assertEqual(first_controls_y, o.controls.winfo_rooty())
            self.assert_layer_order()
        o._history.clear()
        o._render_text()
        o.root.update()
        self.assertAlmostEqual(first_bottom, o.source.winfo_rooty() + o.source.winfo_height(), delta=3)
        self.assertEqual(first_controls_y, o.controls.winfo_rooty())

    def test_long_bilingual_text_fits_and_base_font_is_unchanged(self):
        o = self.overlay
        o.config.source_font_size = o.config.translation_font_size = 96
        o._source_text = '中文字幕需要完整显示，不能被截断。' * 30
        o._translation_text = 'Every word must remain visible after automatic wrapping. ' * 30
        o._render_text()
        o.root.update()
        self.assertLessEqual(o.height, o.root.winfo_screenheight() - 60)
        self.assertGreaterEqual(o.source.winfo_height(), o.source.winfo_reqheight())
        self.assertGreaterEqual(o.translation_label.winfo_height(), o.translation_label.winfo_reqheight())
        self.assertEqual(o.config.source_font_size, 96)

    def test_old_generation_and_old_caption_never_overwrite(self):
        o = self.overlay
        o._caption_id, o._generation = 2, 3
        o.messages.put(OverlayMessage('translation', 'wrong mode', 2, 2))
        o.messages.put(OverlayMessage('translation', 'old subtitle', 1, 3))
        o.messages.put(OverlayMessage('translation', 'correct', 2, 3))
        o._poll()
        self.assertEqual(o._translation_text, 'correct')

    def test_slow_translation_advances_without_freezing_live_source(self):
        o = self.overlay
        o._generation = 1
        o.show_final('Latest recognized line', 10)
        o.show_translation(TranslationResult('第一句译文', 8, 1, 'Source eight'))
        o._poll()
        self.assertEqual(o._translation_text, '第一句译文')
        self.assertEqual(o.source.cget('text'), 'Latest recognized line')
        o.show_final('An even newer line', 11)
        o._poll()
        self.assertEqual(o._translation_text, '')
        o.show_translation(TranslationResult('第二句译文', 9, 1, 'Source nine'))
        o._poll()
        self.assertEqual(o._translation_text, '第二句译文')
        self.assertEqual(o.source.cget('text'), 'An even newer line')
        o.show_translation(TranslationResult('旧句倒序', 8, 1, 'Source eight'))
        o.show_translation(TranslationResult('旧方向结果', 11, 0, 'Source eleven'))
        o._poll()
        self.assertEqual(o._translation_text, '第二句译文')

    def test_bilingual_partial_is_visible_before_translation_and_keeps_updating(self):
        o = self.overlay
        o.show_partial('A long sentence is still being spoken')
        o._poll()
        self.assertEqual(o.source.cget('text'), 'A long sentence is still being spoken')
        self.assertEqual(o.translation_label.cget('text'), '')
        o.show_final('A confirmed portion', 1)
        o.show_translation(TranslationResult('已翻译的小段', 1, 0, 'A confirmed portion'))
        o.show_partial('The speaker is now continuing the sentence')
        o._poll()
        self.assertEqual(o.source.cget('text'), 'The speaker is now continuing the sentence')
        self.assertEqual(o.translation_label.cget('text'), '已翻译的小段')

    def test_preview_translation_is_replaced_by_final_translation(self):
        o = self.overlay
        o._generation = 0
        o.show_partial('We need to review')
        o.show_translation(TranslationResult('我们需要审查', 1, 0,
                                             'We need to review', True, 1))
        o._poll()
        self.assertEqual(o.translation_label.cget('text'), '我们需要审查')
        o.show_final('We need to review the proposal.', 1)
        o._poll()
        self.assertEqual(o.translation_label.cget('text'), '我们需要审查')
        o.show_translation(TranslationResult('我们需要审查这份提案。', 1, 0,
                                             'We need to review the proposal.'))
        o._poll()
        self.assertEqual(o.translation_label.cget('text'), '我们需要审查这份提案。')

        o.show_translation(TranslationResult('旧预翻译', 1, 0,
                                             'We need to review', True, 2))
        o._poll()
        self.assertEqual(o.translation_label.cget('text'), '我们需要审查这份提案。')

    def test_open_sentence_remains_current_until_completed(self):
        o = self.overlay
        o.apply_settings(dict(source_language='en', target_language='zh', bilingual=True,
                               history_enabled=True, history_display_seconds=30))
        first = 'We need to review'
        full = 'We need to review the proposal carefully.'
        o.show_partial(first)
        o.expect_preview(1, 0, 1)
        o.show_translation(TranslationResult('我们需要审查', 1, 0, first, True, 1))
        o._poll()
        self.assertEqual(o._history, [])
        o.show_partial(full)
        o.expect_preview(1, 0, 2)
        o.show_translation(TranslationResult('过时译文', 1, 0, first, True, 1))
        o.show_translation(TranslationResult('我们需要仔细审查提案。', 1, 0, full, True, 2))
        o._poll()
        self.assertEqual(o._history, [])
        self.assertEqual(o.source.cget('text'), full)
        self.assertTrue(o.translation_label.cget('text').endswith('我们需要仔细审查提案。'))
        o.show_final(full, 1)
        o._poll()
        self.assertEqual(o._history, [])
        o.show_partial('The next sentence')
        o._poll()
        self.assertEqual([item.source for item in o._history], [full])
        self.assertEqual(o._history[0].translation, '')
        o.show_translation(TranslationResult('第一句最终译文。', 1, 0, full))
        o.expect_preview(2, 0, 3)
        o.show_translation(TranslationResult('下一句预译文', 2, 0, 'The next sentence', True, 3))
        o._poll()
        self.assertEqual(o.source.cget('text'), full + '\nThe next sentence')
        self.assertEqual(o.translation_label.cget('text'), '第一句最终译文。\n下一句预译文')

    def test_history_keeps_confirmed_source_and_matching_translation(self):
        o = self.overlay
        o.apply_settings(dict(source_language='en', target_language='zh',
                               bilingual=True, history_enabled=True,
                               history_display_seconds=5))
        for caption, source, translation in (
                (1, 'First sentence.', '第一句。'),
                (2, 'Second sentence.', '第二句。'),
                (3, 'Third sentence.', '第三句。')):
            o.show_final(source, caption)
            o.show_translation(TranslationResult(translation, caption, 0, source))
            o._poll()
        self.assertIn('\n', o.source.cget('text'))
        self.assertIn('\n', o.translation_label.cget('text'))
        self.assertIn('First sentence.', o.source.cget('text'))
        self.assertIn('第一句。', o.translation_label.cget('text'))
        self.assertTrue(o.source.cget('text').endswith('Third sentence.'))
        self.assertTrue(o.translation_label.cget('text').endswith('第三句。'))
        self.assertEqual([item.caption_id for item in o._history], [1, 2])
        o.root.update()
        self.assert_layer_order()
        o.apply_settings(dict(source_language='en', target_language='zh',
                               bilingual=True, history_enabled=False,
                               history_display_seconds=5))
        self.assertEqual(o._history, [])

    def test_history_expires_by_display_time(self):
        o = self.overlay
        o.apply_settings(dict(source_language='en', target_language='zh',
                               bilingual=True, history_enabled=True,
                               history_count=0, history_display_seconds=1))
        o.show_final('First sentence.', 1)
        o._poll()
        o.show_final('Second sentence.', 2)
        o._poll()
        self.assertEqual([item.caption_id for item in o._history], [1])
        o._history[0].archived_at = 0
        o._poll()
        self.assertEqual(o._history, [])

    def test_history_count_protects_newest_but_timer_starts_at_archive(self):
        o = self.overlay
        o._history_enabled = True
        o._history_count = 2
        o._history_display_seconds = 5
        def archive(caption, timestamp):
            o._caption_id = caption
            o._current_final_text = f'Sentence {caption}.'
            o._current_final_translation = f'Translation {caption}.'
            with patch('realtime_subtitle.overlay.time.monotonic', return_value=timestamp):
                o._archive_current_caption()
        archive(1, 100)
        archive(2, 101)
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=120):
            self.assertFalse(o._expire_history())
        self.assertEqual([entry.caption_id for entry in o._history], [1, 2])
        self.assertEqual(o._history[0].expires_at, 105)
        archive(3, 120)
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=120):
            self.assertTrue(o._expire_history())
        self.assertEqual([entry.caption_id for entry in o._history], [2, 3])
        archive(4, 121)
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=121):
            o._expire_history()
        self.assertEqual([entry.caption_id for entry in o._history], [3, 4])
        archive(5, 122)
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=122):
            self.assertFalse(o._expire_history())
        self.assertEqual([entry.caption_id for entry in o._history], [3, 4, 5])
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=125):
            self.assertTrue(o._expire_history())
        self.assertEqual([entry.caption_id for entry in o._history], [4, 5])

    def test_history_settings_recalculate_using_original_archive_time(self):
        o = self.overlay
        options = dict(source_language='en', target_language='zh', bilingual=True,
                       history_enabled=True, history_count=2, history_display_seconds=5)
        o.apply_settings(options)
        o._history = [HistoryCaption(1, 'One.', 'First.', 105, 100),
                      HistoryCaption(2, 'Two.', 'Second.', 114, 109)]
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=110):
            o.apply_settings({**options, 'history_count': 1})
        self.assertEqual([entry.caption_id for entry in o._history], [2])
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=112):
            o.apply_settings({**options, 'history_count': 0, 'history_display_seconds': 10})
        self.assertEqual(o._history[0].expires_at, 119)
        self.assertEqual(o.config.history_count, 0)
        self.assertEqual(o.config.history_display_seconds, 10)
        with patch('realtime_subtitle.overlay.time.monotonic', return_value=119):
            self.assertTrue(o._expire_history())
        self.assertEqual(o._history, [])

    def test_translation_only_mode_still_hides_partial_source(self):
        o = self.overlay
        o.set_display_options('en_to_zh', False)
        o.show_partial('Do not expose source in translation-only mode')
        o._poll()
        self.assertEqual(o.source.cget('text'), '')
        o.set_display_options('off', False)
        self.assertEqual(o.source.cget('text'), 'Do not expose source in translation-only mode')

    def test_bilingual_toggle_hides_source_history_and_keeps_translations(self):
        o = self.overlay
        settings = dict(source_language='en', target_language='zh',
                        bilingual=True, history_enabled=True,
                        history_display_seconds=30)
        o.apply_settings(settings)
        for caption, source, translation in (
                (1, 'First sentence.', '第一句。'),
                (2, 'Second sentence.', '第二句。')):
            o.show_final(source, caption)
            o.show_translation(TranslationResult(translation, caption, 0, source))
            o._poll()
        translated = o.translation_label.cget('text')
        o.apply_settings({**settings, 'bilingual': False})
        self.assertEqual(o.source.cget('text'), '')
        self.assertEqual(o.translation_label.cget('text'), translated)
        o.show_partial('New words being spoken')
        o.show_translation(TranslationResult('新的预译文', 3, 0,
                                             'New words being spoken', True, 1))
        o._poll()
        self.assertEqual(o.source.cget('text'), '')
        self.assertEqual(o.translation_label.cget('text'), '第一句。\n第二句。\n新的预译文')
        o.apply_settings(settings)
        self.assertEqual(o.source.cget('text'), 'First sentence.\nSecond sentence.\nNew words being spoken')
        o.apply_settings({**settings, 'target_language': 'none', 'bilingual': False})
        self.assertEqual(o.source.cget('text'), 'New words being spoken')
        self.assertEqual(o.translation_label.cget('text'), '')

    def test_translation_only_layout_removes_source_space(self):
        o = self.overlay
        o.set_display_options('en_to_zh', True)
        o._source_text = 'Recognized source text.'
        o._translation_text = 'Translated text.'
        o._render_text()
        o.root.update()
        bilingual_height = o.height
        anchored_bottom = o._line_anchor_y
        o.set_display_options('en_to_zh', False)
        o.root.update()
        self.assertFalse(o.source.winfo_ismapped())
        self.assertEqual(o.source.winfo_manager(), '')
        self.assertTrue(o.translation_label.winfo_ismapped())
        self.assertEqual(o.translation_label.winfo_y(), 12)
        self.assertEqual(o.height, o.translation_label.winfo_reqheight() + 24)
        self.assertLess(o.height, bilingual_height)
        self.assertEqual(o._line_anchor_y, anchored_bottom)
        self.assertAlmostEqual(o.translation_label.winfo_rooty() + o.translation_label.winfo_height(),
                               anchored_bottom, delta=2)
        compact_height = o.height
        o.config.source_font_size = 96
        o._render_text()
        o.root.update()
        self.assertEqual(o.height, compact_height)
        self.assert_layer_order()
        o.set_display_options('en_to_zh', True)
        o.root.update()
        self.assertTrue(o.source.winfo_ismapped())
        o.set_display_options('off', False)
        o.root.update()
        self.assertTrue(o.source.winfo_ismapped())
        self.assertFalse(o.translation_label.winfo_ismapped())

    def test_destroyed_background_is_recreated_and_polling_continues(self):
        o = self.overlay
        old = str(o.background)
        o.background.destroy()
        o.show_final('After external background destruction', 1)
        o._poll()
        o.root.update()
        self.assertTrue(o.background.winfo_exists())
        self.assertNotEqual(str(o.background), old)
        self.assertIsNotNone(o._poll_after)
        o.show_final('Next line still works', 2)
        o._poll()
        self.assertEqual(o.source.cget('text'), 'Next line still works')

    def test_render_exception_does_not_kill_refresh_timer(self):
        o = self.overlay
        o.show_final('One', 1)
        with patch.object(o, '_render_text', side_effect=RuntimeError('temporary render error')):
            with self.assertLogs('realtime_subtitle.overlay', level='ERROR'):
                o._poll()
        self.assertIsNotNone(o._poll_after)
        o.show_final('Two', 2)
        o._poll()
        self.assertEqual(o.source.cget('text'), 'Two')

    def test_close_from_idle_during_fit_is_safe(self):
        o = self.overlay
        o.root.after_idle(o._close)
        o.show_final('Closing during render', 1)
        o._poll()
        self.assertTrue(o._closed)
        self.assertIsNone(o._poll_after)

    def test_native_close_on_background_closes_application_safely(self):
        o = self.overlay
        close_command = o.background.protocol('WM_DELETE_WINDOW')
        o.root.tk.call(close_command)
        self.assertTrue(o._closed)
        o._poll()
        self.assertIsNone(o._poll_after)

    def test_posted_mode_dropdown_survives_continuous_caption_updates(self):
        o = self.overlay
        o.open_settings()
        o.root.update()
        combo = o._mode_box
        tk = o.root.tk
        tk.call('ttk::combobox::Post', str(combo))
        o.root.update()
        self.assertTrue(o._popup_open())
        grab = tk.call('grab', 'current')
        with patch.object(o._styles, 'raise_topmost') as raise_window:
            for i in range(20):
                o.show_partial(f'Continuous partial {i}')
                o.show_final(f'Final {i}', i + 1)
                o._poll()
                o._restack_layers()
                o.root.update()
                self.assertTrue(o._popup_open())
                self.assertEqual(tk.call('grab', 'current'), grab)
            raise_window.assert_not_called()
        tk.call('ttk::combobox::Unpost', str(combo))
        o._poll()
        self.assertEqual(o._source_text, 'Final 19')

    def test_settings_escape_does_not_close_application(self):
        o = self.overlay
        o.open_settings()
        o.root.update()
        win = o._settings_window
        win.focus_force()
        win.event_generate('<Escape>')
        o.root.update()
        self.assertFalse(o._closed)
        self.assertIsNone(o._settings_window)


if __name__ == '__main__':
    unittest.main()
