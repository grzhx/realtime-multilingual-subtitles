from __future__ import annotations

import queue
import logging
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import colorchooser, ttk

from .config import (TranslationConfig, UiConfig, languages_from_mode,
                      mode_from_languages, normalize_source_language,
                      normalize_target_language)
from .windows import WindowStyles

COLOR_KEY = '#010203'
BUTTON_SIZE = 32
TEXT_RIGHT_PADDING = 3 * BUTTON_SIZE + 20
MODE_NAMES = {'zh_to_en': '中译英', 'en_to_zh': '英译中', 'off': '无翻译'}
SOURCE_NAMES = {'auto': '自适应', 'zh': '中文', 'en': 'English'}
TARGET_NAMES = {'none': '无翻译', 'zh': '中文', 'en': 'English'}
LEGACY_MODE_ALIASES = {'中译英': ('zh', 'en'), '英译中': ('en', 'zh')}
logger = logging.getLogger(__name__)


@dataclass
class OverlayMessage:
    kind: str
    text: str = ''
    caption_id: int = 0
    generation: int = 0
    source_text: str = ''
    preview: bool = False
    revision: int = 0


@dataclass
class HistoryCaption:
    caption_id: int
    source: str
    translation: str = ''
    expires_at: float | None = None
    archived_at: float | None = None


class SubtitleOverlay:
    """Separate text, background and controls so alpha and input stay independent."""

    def __init__(self, config: UiConfig, on_close, on_settings=None,
                 translation: TranslationConfig | None = None):
        self.config, self.on_close, self.on_settings = config, on_close, on_settings
        self.messages = queue.Queue()
        self._source_language = (normalize_source_language(translation.source_language)
                                 if translation else 'auto')
        self._target_language = (normalize_target_language(translation.target_language)
                                 if translation and translation.enabled else 'none')
        self._mode = mode_from_languages(self._source_language, self._target_language)
        self._bilingual = translation.bilingual if translation else True
        self._source_text = self._translation_text = self._status = ''
        self._current_final_text = ''
        self._current_final_translation = ''
        self._expected_preview = None
        self._history_enabled = bool(getattr(config, 'history_enabled', False))
        self._history_display_seconds = max(0.5, float(
            getattr(config, 'history_display_seconds', 5.0)))
        self._history_count = max(0, min(10, int(config.history_count)))
        self._history: list[HistoryCaption] = []
        self._caption_id = self._generation = 0
        self._translated_caption_id = -1
        self._preview_active = False
        self._preview_revision = 0
        self._translated_source = ''
        self._locked = self._closed = self._hovered = False
        self._drag_origin = None
        self._resize_origin = None
        self._settings_window = None
        self._mode_box = None  # Compatibility alias for the target-language box.
        self._source_box = None
        self._moved = False
        self._line_anchor_y = None
        self._poll_after = None
        self._restack_after = None
        self._layer_repair_after = None
        self._styles = WindowStyles()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title('Realtime Subtitle Translator')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.configure(bg=COLOR_KEY)
        self.root.attributes('-transparentcolor', COLOR_KEY)
        self.root.protocol('WM_DELETE_WINDOW', self._close)

        self.background = self._layer(config.background_color)
        self.background.protocol('WM_DELETE_WINDOW', self._close)
        self.controls = self._layer('#202020')
        self.toolbar = tk.Frame(self.controls, bg='#202020')
        self.toolbar.pack(fill='both', expand=True)
        self.lock_button = self._button('🔒', self.toggle_lock, 2)
        self.settings_button = self._button('⚙', self.open_settings, 1)
        self.close_button = self._button('×', self._close, 0)
        self.source = tk.Label(self.root, bg=COLOR_KEY, fg=config.source_color, bd=0,
                               anchor='center', justify='center')
        self.translation_label = tk.Label(self.root, bg=COLOR_KEY, fg=config.translation_color,
                                          bd=0, anchor='center', justify='center')
        self.source.pack(fill='x', padx=(20, TEXT_RIGHT_PADDING), pady=(40, 0))
        self.translation_label.pack(fill='x', padx=(20, TEXT_RIGHT_PADDING), pady=(4, 12))
        for widget in (self.root, self.source, self.translation_label, self.background):
            widget.bind('<ButtonPress-1>', self._drag_start)
            widget.bind('<B1-Motion>', self._drag_move)
            widget.bind('<ButtonRelease-1>', self._drag_end)
            widget.bind('<Motion>', self._update_resize_cursor)
        self.root.bind('<FocusIn>', self._schedule_restack, add='+')
        self.background.bind('<FocusIn>', self._schedule_restack, add='+')
        self.root.bind('<Escape>', self._escape)
        self.width = min(max(260, config.width), self.root.winfo_screenwidth() - 40)
        self.height = max(80, config.height)
        self.x = max(0, (self.root.winfo_screenwidth() - self.width) // 2)
        self.y = max(0, self.root.winfo_screenheight() - self.height - config.bottom_margin)
        self.root.deiconify()
        # Tk ignores geometry changes made while the root is withdrawn. Flush
        # the deiconify before the first layout so the text HWND starts at the
        # same coordinates as the background HWND.
        # A full update is required here: with sibling top-level windows,
        # update_idletasks() can leave the withdrawn root's HWND at (0, 0).
        self.root.update()
        self._update_layers()
        self._render_text()
        self._poll_after = self.root.after(60, self._poll)

    def _layer(self, color):
        win = tk.Toplevel(self.root)
        # Alt+F4 can target a borderless background/control layer. Closing any
        # application layer must close the app, never leave a partial overlay.
        win.protocol('WM_DELETE_WINDOW', self._close)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=color)
        return win

    def _button(self, text, command, column):
        button = tk.Button(self.toolbar, text=text, command=command, relief='flat', bd=0,
                           bg='#202020', fg='#FFFFFF', activebackground='#404040',
                           activeforeground='#FFFFFF', font=('Segoe UI Symbol', 14), cursor='hand2')
        button.place(x=column * BUTTON_SIZE, y=0, width=BUTTON_SIZE, height=BUTTON_SIZE)
        return button

    def _update_layers(self):
        """Never call update() here: it reenters geometry/lock callbacks."""
        if not self._alive():
            return
        self._ensure_background()
        geometry = f'{self.width}x{self.height}+{self.x}+{self.y}'
        self.background.geometry(geometry)
        self.root.geometry(geometry)
        # Tk can retain (0, 0) for a color-keyed overrideredirect root. The
        # Win32 move is the authoritative position for both layers.
        self._styles.move_resize(self.background, self.x, self.y, self.width, self.height)
        self._styles.move_resize(self.root, self.x, self.y, self.width, self.height)
        self.background.configure(bg=self.config.background_color)
        opacity = max(0.0, min(1.0, self.config.background_opacity))
        # Windows ignores hit testing on alpha-zero pixels. One alpha unit on
        # the input/background layer preserves dragging without fading text.
        if self._locked and opacity == 0:
            self.background.withdraw()
        else:
            if not self.background.winfo_ismapped():
                self.background.deiconify()
            self.background.attributes('-alpha', max(1 / 255, opacity))
        self.root.attributes('-alpha', max(0.01, min(1.0, self.config.text_opacity)))
        self._styles.click_through(self.root, self._locked)
        self._styles.click_through(self.background, self._locked)
        # Controls are always the interactive top layer, including after a
        # previous locked state or a transparent background transition.
        self._styles.click_through(self.controls, False)
        controls_y = self._controls_y()
        self.controls.geometry(f'{3 * BUTTON_SIZE}x{BUTTON_SIZE}+{self.x + self.width - 3 * BUTTON_SIZE - 4}+{controls_y}')
        self._styles.move_resize(self.controls, self.x + self.width - 3 * BUTTON_SIZE - 4,
                                 controls_y, 3 * BUTTON_SIZE, BUTTON_SIZE)
        self._update_controls()
        self._restack_layers()
        self._schedule_layer_repair()

    def _controls_y(self):
        """Align controls with the bottom/current line of visible subtitles."""
        desired = (self._line_anchor_y - BUTTON_SIZE
                   if self._line_anchor_y is not None else self.y + 4)
        return max(self.y + 4, min(desired, self.y + self.height - BUTTON_SIZE - 4))

    def _schedule_restack(self, _event=None):
        if not self._closed and self._restack_after is None:
            self._restack_after = self.root.after_idle(self._restack_layers)

    def _schedule_layer_repair(self):
        """Repair native Z order after Tk finishes an asynchronous layout."""
        if self._closed or self._layer_repair_after is not None:
            return
        self._layer_repair_after = self.root.after(12, self._repair_layers)

    def _repair_layers(self):
        self._layer_repair_after = None
        if not self._alive():
            return
        if self._settings_window and self._settings_window.winfo_exists():
            return
        self._restack_layers()

    def _restack_layers(self):
        if self._restack_after is not None:
            self.root.after_cancel(self._restack_after)
            self._restack_after = None
        if not self._alive():
            return
        # A ttk popup is a native window with its own grab. Do not reorder any
        # of the overlay/dialog windows while the settings dialog is visible.
        if self._settings_window and self._settings_window.winfo_exists():
            return
        self._ensure_background()
        self._styles.detach_owner(self.background)
        self._styles.detach_owner(self.controls)
        self._styles.raise_topmost(self.background)
        self._styles.raise_topmost(self.root)
        self._styles.raise_topmost(self.controls)

    def _update_controls(self):
        if not self._alive() or not self.controls.winfo_exists():
            return
        self.lock_button.configure(text='🔓' if self._locked else '🔒')
        # The same button keeps exactly the same 32x32 rectangle in both states.
        for button, column in ((self.close_button, 0), (self.settings_button, 1)):
            if self._hovered and not self._locked:
                button.place(x=column * BUTTON_SIZE, y=0, width=BUTTON_SIZE, height=BUTTON_SIZE)
            else:
                button.place_forget()
        # Unmapped control slots must not intercept input when locked/hidden.
        controls_y = self._controls_y()
        if self._locked or not self._hovered:
            self.controls.geometry(f'{BUTTON_SIZE}x{BUTTON_SIZE}+{self.x + self.width - BUTTON_SIZE - 4}+{controls_y}')
            self._styles.move_resize(self.controls, self.x + self.width - BUTTON_SIZE - 4,
                                     controls_y, BUTTON_SIZE, BUTTON_SIZE)
            self.lock_button.place(x=0, y=0, width=BUTTON_SIZE, height=BUTTON_SIZE)
        else:
            self.controls.geometry(f'{3 * BUTTON_SIZE}x{BUTTON_SIZE}+{self.x + self.width - 3 * BUTTON_SIZE - 4}+{controls_y}')
            self._styles.move_resize(self.controls, self.x + self.width - 3 * BUTTON_SIZE - 4,
                                     controls_y, 3 * BUTTON_SIZE, BUTTON_SIZE)
            self.lock_button.place(x=2 * BUTTON_SIZE, y=0, width=BUTTON_SIZE, height=BUTTON_SIZE)

    def toggle_lock(self):
        self._locked = not self._locked
        self._drag_origin = None
        self._resize_origin = None
        if self._locked and self._settings_window and self._settings_window.winfo_exists():
            self._close_settings()
        self._update_layers()

    def _lock(self):
        if not self._locked:
            self.toggle_lock()

    def _unlock(self):
        if self._locked:
            self.toggle_lock()

    def _drag_start(self, event):
        if self._locked:
            return
        edge = self._resize_edge(event.x_root)
        if edge:
            self._resize_origin = edge, event.x_root, self.width, self.x
            self._drag_origin = None
        else:
            self._drag_origin = event.x_root, event.y_root, self.x, self.y
            self._resize_origin = None
        self._schedule_restack()

    def _drag_end(self, _event):
        self._drag_origin = None
        self._resize_origin = None

    def _resize_edge(self, x_root):
        if self._locked:
            return None
        edge_size = 12
        if self.x <= x_root <= self.x + edge_size:
            return 'left'
        if self.x + self.width - edge_size <= x_root <= self.x + self.width:
            return 'right'
        return None

    def _update_resize_cursor(self, event):
        if self._locked:
            cursor = 'arrow'
        else:
            cursor = 'size_we' if self._resize_edge(event.x_root) else 'arrow'
        try:
            event.widget.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _drag_move(self, event):
        if self._locked:
            return
        if self._resize_origin is not None:
            edge, pointer_x, original_width, original_x = self._resize_origin
            delta = event.x_root - pointer_x
            minimum = 260
            maximum = self.root.winfo_screenwidth() - 40
            if edge == 'right':
                self.width = max(minimum, min(maximum, original_width + delta))
            else:
                new_width = max(minimum, min(maximum, original_width - delta))
                self.width = new_width
                self.x = original_x + original_width - new_width
                self.x = max(0, min(self.x, self.root.winfo_screenwidth() - self.width))
            self._fit_window()
            return
        if self._drag_origin is None:
            return
        px, py, x, y = self._drag_origin
        old_y = self.y
        self.x, self.y = x + event.x_root - px, y + event.y_root - py
        if self._line_anchor_y is not None:
            self._line_anchor_y += self.y - old_y
        self._moved = True
        self._update_layers()

    def set_display_options(self, source_language, target_language=None, bilingual=None):
        # Keep the old two-argument API usable for scripts and older plugins.
        if source_language in MODE_NAMES:
            old_bilingual = target_language if bilingual is None else bilingual
            source_language, target_language = languages_from_mode(source_language)
            bilingual = bool(old_bilingual)
        else:
            bilingual = bool(bilingual)
        source_language = normalize_source_language(source_language)
        target_language = normalize_target_language(target_language)
        mode = mode_from_languages(source_language, target_language)
        if mode != self._mode:
            self._translation_text = ''
            self._translated_source = ''
            self._translated_caption_id = -1
            self._preview_active = False
            self._preview_revision = 0
            self._history.clear()
            self._expected_preview = None
            self._current_final_translation = ''
        self._source_language = source_language
        self._target_language = target_language
        self._mode, self._bilingual = mode, bool(bilingual)
        self._render_text()

    def show_partial(self, text):
        self.messages.put(OverlayMessage('partial', text))

    def expect_preview(self, caption_id, generation, revision):
        self.messages.put(OverlayMessage('preview_request', caption_id=caption_id,
                                         generation=generation, revision=revision))

    def show_final(self, text, caption_id=0):
        self.messages.put(OverlayMessage('final', text, caption_id))

    def show_translation(self, result):
        if isinstance(result, str):
            self.messages.put(OverlayMessage('status', result))
        else:
            self.messages.put(OverlayMessage('translation', result.text, result.caption_id,
                                             result.generation, result.source_text,
                                             result.preview, result.revision))

    def show_status(self, text):
        self.messages.put(OverlayMessage('status', text))

    def show_generation(self, generation):
        # Called on the UI thread when settings are applied.
        self._generation = generation

    def _poll(self):
        try:
            self._poll_messages()
        except Exception:
            if self._alive():
                logger.exception('Subtitle refresh failed; will retry')
        finally:
            # An isolated render error must not kill all future subtitle updates.
            if self._alive() and self._poll_after is None:
                self._poll_after = self.root.after(60, self._poll)

    def _poll_messages(self):
        if self._closed:
            return
        if self._poll_after is not None:
            self.root.after_cancel(self._poll_after)
            self._poll_after = None
        if self._popup_open():
            # Do not resize, restack or change focus/grab during mode selection.
            # The short selection pause is drained on the next tick after unpost.
            return
        changed = False
        for _ in range(100):
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if message.kind in ('partial', 'final'):
                if (message.kind == 'partial' and message.text.strip()
                        and self._history_enabled and self._current_final_text
                        and message.text != self._current_final_text):
                    self._archive_current_caption()
                    self._current_final_text = ''
                    self._current_final_translation = ''
                    self._translation_text = ''
                    self._translated_source = ''
                    self._preview_active = False
                    self._preview_revision = 0
                    self._translated_caption_id = -1
                self._source_text = message.text
                self._status = ''
                if message.kind == 'final':
                    self._archive_current_caption()
                    # A preview uses the next caption id. If it arrived before
                    # the final ASR event, keep it visible through this final
                    # source update until the formal translation replaces it.
                    keep_preview = (self._preview_active and
                                    self._translated_caption_id == message.caption_id)
                    self._caption_id = message.caption_id
                    self._current_final_text = message.text
                    self._current_final_translation = ''
                    self._expected_preview = None
                    if not keep_preview:
                        self._translation_text = ''
                        self._translated_source = ''
                        self._translated_caption_id = -1
                        self._preview_active = False
                        self._preview_revision = 0
                changed = True
            elif message.kind == 'preview_request':
                if (message.generation == self._generation and
                        message.caption_id == self._caption_id + 1):
                    self._expected_preview = (message.caption_id, message.generation, message.revision)
            elif message.kind == 'translation':
                if (self._mode != 'off' and message.generation == self._generation
                        and (message.caption_id <= self._caption_id or
                             (message.preview and message.caption_id == self._caption_id + 1))):
                    if message.preview:
                        if (self._expected_preview is not None and
                                self._expected_preview != (message.caption_id, message.generation, message.revision)):
                            continue
                        if (self._preview_active and message.revision < self._preview_revision):
                            continue
                        if message.caption_id != self._caption_id + 1:
                            continue
                        self._translation_text = message.text
                        self._translated_source = message.source_text or self._source_text
                        self._translated_caption_id = message.caption_id
                        self._preview_active = True
                        self._preview_revision = message.revision
                    elif message.caption_id == self._caption_id and self._current_final_text:
                        self._translation_text = message.text
                        self._translated_source = message.source_text or self._source_text
                        self._translated_caption_id = message.caption_id
                        self._preview_active = False
                        self._preview_revision = 0
                        self._current_final_translation = message.text
                    else:
                        history = next((item for item in self._history
                                        if item.caption_id == message.caption_id), None)
                        if history is None:
                            # Preserve the legacy latest-only behavior when
                            # history display is disabled.
                            if (not message.preview and not self._history_enabled and
                                    self._translated_caption_id < message.caption_id):
                                self._translation_text = message.text
                                self._translated_source = message.source_text or self._source_text
                                self._translated_caption_id = message.caption_id
                            else:
                                continue
                        else:
                            history.translation = message.text
                    self._status = ''
                    changed = True
            elif message.kind == 'status':
                self._status = message.text
                changed = True
        if changed and self._alive():
            self._render_text()
        if self._expire_history():
            self._render_text()
        if not self._alive():
            return
        # Cursor polling works over color-keyed areas which generate no Enter.
        px, py = self.root.winfo_pointerxy()
        hovered = self.x <= px < self.x + self.width and self.y <= py < self.y + self.height
        if hovered != self._hovered:
            self._hovered = hovered
            self._update_controls()

    def _render_text(self):
        if not self._alive():
            return
        source_history, translation_history = self._history_text()
        source = (self._join_history(source_history, self._source_text)
                  if self._mode == 'off' or self._bilingual else '')
        translated = (self._join_history(translation_history, self._translation_text)
                      if self._mode != 'off' else '')
        # Source is the live ASR result. Never freeze it on an older translated
        # segment while the speaker is already continuing the next sentence.
        if self._status:
            source, translated = self._status, ''
        self.source.configure(text=source)
        self.translation_label.configure(text=translated)
        self._fit_window()

    @staticmethod
    def _join_history(history: str, current: str) -> str:
        if history and current:
            # Keep the live/current caption below the historical sentences.
            return f'{history}\n{current}'
        return history or current

    def _history_text(self) -> tuple[str, str]:
        if not self._history_enabled:
            return '', ''
        entries = self._history
        return ('\n'.join(item.source for item in entries),
                '\n'.join(item.translation for item in entries))

    def _archive_current_caption(self):
        if not self._history_enabled:
            return
        if not self._current_final_text or self._caption_id <= 0:
            return
        # Only final translations enter history; previews may cover a longer
        # still-open sentence and must never be archived as a finished result.
        translation = self._current_final_translation
        now = time.monotonic()
        self._history.append(HistoryCaption(
            self._caption_id, self._current_final_text, translation,
            expires_at=now + self._history_display_seconds,
            archived_at=now))
        self._update_history_deadlines(now)

    def _update_history_deadlines(self, now):
        for item in self._history:
            # The display timer starts when the sentence enters history. The
            # count only protects the newest entries from being removed while
            # they are still within the protected window.
            if item.archived_at is not None:
                item.expires_at = item.archived_at + self._history_display_seconds
            elif item.expires_at is None:
                item.archived_at = now
                item.expires_at = now + self._history_display_seconds

    def _expire_history(self) -> bool:
        if not self._history:
            return False
        now = time.monotonic()
        self._update_history_deadlines(now)
        overflow_count = max(0, len(self._history) - self._history_count)
        remaining = [item for index, item in enumerate(self._history)
                     if index >= overflow_count or item.expires_at is None or item.expires_at > now]
        if len(remaining) == len(self._history):
            return False
        self._history = remaining
        return True

    def _popup_open(self):
        if not self._mode_box:
            return False
        try:
            popup = self.root.tk.call('ttk::combobox::PopdownWindow', str(self._mode_box))
            return bool(int(self.root.tk.call('winfo', 'viewable', popup)))
        except tk.TclError:
            return False

    def _fit_window(self):
        if not self._alive():
            return
        # Measure the wrapped labels, not the current allocated window height.
        available = max(100, self.root.winfo_screenheight() - 60)
        source_visible = bool(self.source.cget('text'))
        translation_visible = bool(self.translation_label.cget('text'))
        self.source.pack_forget()
        self.translation_label.pack_forget()
        source_size, translated_size = self.config.source_font_size, self.config.translation_font_size
        for _ in range(100):
            self.source.configure(wraplength=self.width - 20 - TEXT_RIGHT_PADDING, fg=self.config.source_color,
                                  font=(self.config.font_family, source_size))
            self.translation_label.configure(wraplength=self.width - 20 - TEXT_RIGHT_PADDING, fg=self.config.translation_color,
                                             font=(self.config.font_family, translated_size, 'bold'))
            source_height = self.source.winfo_reqheight() if source_visible else 0
            translation_height = self.translation_label.winfo_reqheight() if translation_visible else 0
            # Only visible labels take space. Leave enough room for the 32px
            # controls even when the visible text uses a very small font.
            first_height = source_height if source_visible else translation_height
            top_padding = max(12, BUTTON_SIZE + 4 - first_height)
            gap = 4 if source_visible and translation_visible else 0
            if source_visible:
                self.source.pack(fill='x', padx=(20, TEXT_RIGHT_PADDING),
                                 pady=(top_padding, 0 if translation_visible else 12))
            if translation_visible:
                self.translation_label.pack(fill='x', padx=(20, TEXT_RIGHT_PADDING),
                                            pady=(gap if source_visible else top_padding, 12))
            self.root.update_idletasks()
            # update_idletasks can run an idle close callback. Never continue
            # rendering a window destroyed by that callback.
            if not self._alive():
                return
            required = source_height + translation_height + top_padding + gap + 12
            if required <= available or max(source_size, translated_size) <= 1:
                break
            source_size = max(1, int(source_size * 0.9))
            translated_size = max(1, int(translated_size * 0.9))
        self.height = max(BUTTON_SIZE + 16, required)
        # The last line of the first visible label anchors both text and tools.
        anchor_offset = top_padding + first_height
        if self._line_anchor_y is None:
            self.y = max(0, self.root.winfo_screenheight() - self.height - self.config.bottom_margin)
            self._line_anchor_y = self.y + anchor_offset
        desired_y = self._line_anchor_y - anchor_offset
        self.y = max(0, min(desired_y, self.root.winfo_screenheight() - self.height))
        # Screen edges take precedence over anchoring to prevent clipped text.
        self._line_anchor_y = self.y + anchor_offset
        self._update_layers()

    def apply_settings(self, settings):
        source_language = settings.get('source_language', self._source_language)
        target_language = settings.get('target_language', self._target_language)
        if 'source_language' not in settings and 'target_language' not in settings:
            source_language, target_language = languages_from_mode(settings.get('mode', self._mode))
        source_language = normalize_source_language(source_language)
        target_language = normalize_target_language(target_language)
        mode = mode_from_languages(source_language, target_language)
        bilingual = bool(settings['bilingual'])
        history_enabled = bool(settings.get('history_enabled', self._history_enabled))
        try:
            history_count = max(0, min(10, int(settings.get('history_count', self._history_count))))
        except (TypeError, ValueError):
            history_count = self._history_count
        try:
            history_display_seconds = max(0.5, min(60.0, float(
                settings.get('history_display_seconds', self._history_display_seconds))))
        except (TypeError, ValueError):
            history_display_seconds = self._history_display_seconds
        for key in ('width', 'source_font_size', 'translation_font_size', 'background_opacity',
                    'text_opacity', 'background_color', 'source_color', 'translation_color',
                    'history_enabled'):
            if key in settings:
                setattr(self.config, key, settings[key])
        if 'width' in settings:
            self.width = max(260, min(self.root.winfo_screenwidth() - 40,
                                      int(settings['width'])))
            if not self._moved:
                self.x = max(0, (self.root.winfo_screenwidth() - self.width) // 2)
        self._history_enabled = history_enabled
        self._history_display_seconds = history_display_seconds
        self._history_count = history_count
        self.config.history_display_seconds = history_display_seconds
        self.config.history_count = history_count
        if not history_enabled:
            self._history.clear()
        else:
            self._expire_history()
        if self.on_settings:
            self.on_settings({'mode': mode, 'source_language': source_language,
                              'target_language': target_language, 'bilingual': bilingual})
        self.set_display_options(source_language, target_language, bilingual)
        self._schedule_layer_repair()

    def open_settings(self):
        if self._locked:
            return
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.lift()
            return
        win = tk.Toplevel(self.root)
        self._settings_window = win
        win.protocol('WM_DELETE_WINDOW', self._close_settings)
        win.bind('<Escape>', lambda _: (self._close_settings(), 'break')[1])
        win.title('字幕设置')
        win.attributes('-topmost', True)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=14)
        frame.pack()
        source = tk.StringVar(value=SOURCE_NAMES[self._source_language])
        target = tk.StringVar(value=TARGET_NAMES[self._target_language])
        bilingual = tk.BooleanVar(value=self._bilingual)
        history_enabled = tk.BooleanVar(value=self._history_enabled)
        history_display_seconds = tk.DoubleVar(value=self._history_display_seconds)
        history_count = tk.IntVar(value=self._history_count)
        ttk.Label(frame, text='目标语言').grid(row=0, column=0, sticky='w')
        target_box = ttk.Combobox(frame, values=list(TARGET_NAMES.values()), textvariable=target,
                                  state='readonly', width=20)
        self._mode_box = target_box
        target_box.grid(row=0, column=1, pady=4)
        ttk.Label(frame, text='源语言').grid(row=1, column=0, sticky='w')
        source_box = ttk.Combobox(frame, values=list(SOURCE_NAMES.values()), textvariable=source,
                                  state='readonly', width=20)
        self._source_box = source_box
        source_box.grid(row=1, column=1, pady=4)

        def apply_translation_options(_event=None):
            alias = LEGACY_MODE_ALIASES.get(target.get())
            if alias:
                source.set(SOURCE_NAMES[alias[0]])
                target.set(TARGET_NAMES[alias[1]])
            source_value = {v: k for k, v in SOURCE_NAMES.items()}.get(source.get(), self._source_language)
            target_value = {v: k for k, v in TARGET_NAMES.items()}.get(target.get(), self._target_language)
            self.apply_settings(dict(source_language=source_value,
                                     target_language=target_value,
                                     bilingual=bilingual.get(),
                                     history_enabled=history_enabled.get(),
                                     history_count=history_count.get(),
                                     history_display_seconds=history_display_seconds.get()))
        target_box.bind('<<ComboboxSelected>>', apply_translation_options)
        source_box.bind('<<ComboboxSelected>>', apply_translation_options)
        ttk.Checkbutton(frame, text='双语字幕', variable=bilingual,
                        command=apply_translation_options).grid(row=2, column=0, columnspan=2, sticky='w')
        ttk.Checkbutton(frame, text='保留历史字幕', variable=history_enabled,
                        command=apply_translation_options).grid(row=3, column=0, columnspan=2, sticky='w')
        ttk.Label(frame, text='字幕显示时间(秒)').grid(row=4, column=0, sticky='w', pady=4)
        ttk.Spinbox(frame, from_=0.5, to=60, increment=0.5,
                    textvariable=history_display_seconds,
                    width=8).grid(row=4, column=1, sticky='w')
        ttk.Label(frame, text='历史条数').grid(row=5, column=0, sticky='w', pady=4)
        ttk.Spinbox(frame, from_=0, to=10, textvariable=history_count,
                    width=8).grid(row=5, column=1, sticky='w')
        variables = {}
        labels = [('width', '窗口宽度'), ('source_font_size', '原文字号'), ('translation_font_size', '译文字号'),
                  ('background_opacity', '背景不透明度'), ('text_opacity', '文字不透明度')]
        for row, (key, label) in enumerate(labels, 6):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=4)
            if key == 'width':
                var = tk.IntVar(value=self.width)
                ttk.Spinbox(frame, from_=260, to=max(260, self.root.winfo_screenwidth() - 40),
                            textvariable=var, width=8).grid(row=row, column=1, sticky='w')
            elif 'font_size' in key:
                var = tk.IntVar(value=getattr(self.config, key))
                ttk.Spinbox(frame, from_=8, to=96, textvariable=var, width=8).grid(row=row, column=1, sticky='w')
            else:
                var = tk.DoubleVar(value=getattr(self.config, key))
                tk.Scale(frame, from_=0, to=1, resolution=0.05, orient='horizontal',
                         length=180, variable=var).grid(row=row, column=1)
            variables[key] = var
        for row, (key, label) in enumerate((('background_color', '背景颜色'),
                                           ('source_color', '原文颜色'), ('translation_color', '译文颜色')), 11):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=4)
            var = tk.StringVar(value=getattr(self.config, key))
            variables[key] = var
            button = tk.Button(frame, bg=var.get(), width=8, relief='groove')
            def choose(v=var, b=button):
                color = colorchooser.askcolor(v.get(), parent=win)[1]
                if color:
                    v.set(color)
                    b.configure(bg=color)
            button.configure(command=choose)
            button.grid(row=row, column=1, sticky='w')
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, foreground='#b00020').grid(row=14, column=0, columnspan=2)
        def apply():
            try:
                values = {key: var.get() for key, var in variables.items()}
                values['width'] = max(260, min(self.root.winfo_screenwidth() - 40, int(values['width'])))
                for key in ('source_font_size', 'translation_font_size'):
                    values[key] = max(8, min(96, int(values[key])))
                self.apply_settings(dict(source_language={v: k for k, v in SOURCE_NAMES.items()}[source.get()],
                                         target_language={v: k for k, v in TARGET_NAMES.items()}[target.get()],
                                         bilingual=bilingual.get(),
                                         history_enabled=history_enabled.get(),
                                         history_count=history_count.get(),
                                         history_display_seconds=history_display_seconds.get(), **values))
                error.set('')
            except (ValueError, tk.TclError) as exc:
                error.set(str(exc))
        ttk.Button(frame, text='应用', command=apply).grid(row=15, column=0, pady=8)
        ttk.Button(frame, text='关闭', command=self._close_settings).grid(row=15, column=1, pady=8)
        win.update_idletasks()
        win.geometry(f'+{max(0, self.x + self.width - win.winfo_reqwidth())}+{max(0, self.y - win.winfo_reqheight() - 10)}')

    def run(self):
        self.root.mainloop()

    def _alive(self):
        if self._closed:
            return False
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _ensure_background(self):
        if self.background.winfo_exists():
            return
        logger.warning('Background layer was destroyed; recreating it')
        self.background = self._layer(self.config.background_color)
        self.background.protocol('WM_DELETE_WINDOW', self._close)
        self.background.bind('<ButtonPress-1>', self._drag_start)
        self.background.bind('<B1-Motion>', self._drag_move)
        self.background.bind('<ButtonRelease-1>', self._drag_end)
        self.background.bind('<Motion>', self._update_resize_cursor)
        self.background.bind('<FocusIn>', self._schedule_restack, add='+')

    def _close_settings(self):
        if self._mode_box and self._mode_box.winfo_exists():
            self.root.tk.call('ttk::combobox::Unpost', str(self._mode_box))
        self._mode_box = None
        win, self._settings_window = self._settings_window, None
        if win and win.winfo_exists():
            win.destroy()
        self._schedule_restack()
        self._schedule_layer_repair()

    def _escape(self, event):
        if not self._alive():
            return
        if event.widget.winfo_toplevel() == self.root:
            self._close()
        else:
            self._close_settings()
        return 'break'

    def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._restack_after is not None:
                self.root.after_cancel(self._restack_after)
                self._restack_after = None
            if self._poll_after is not None:
                try:
                    self.root.after_cancel(self._poll_after)
                except tk.TclError:
                    pass
                self._poll_after = None
            if self._layer_repair_after is not None:
                try:
                    self.root.after_cancel(self._layer_repair_after)
                except tk.TclError:
                    pass
                self._layer_repair_after = None
            self.on_close()
        finally:
            try:
                if self.root.winfo_exists():
                    self.root.destroy()
            except tk.TclError:
                pass
