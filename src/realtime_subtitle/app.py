from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path


class SingleInstance:
    """Prevent two model copies from consuming the GPU at the same time."""

    def __init__(self, name: str = "Local\\RealtimeSubtitleTranslator"):
        self.name = name
        self.handle = None

    def __enter__(self):
        if sys.platform != "win32":
            return self
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        ctypes.set_last_error(0)
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise RuntimeError("无法创建单实例锁")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError("字幕程序已经在运行，请先关闭现有窗口")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle and sys.platform == "win32":
            import ctypes
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def configure_desktop_friendly_runtime() -> None:
    """Keep local inference responsive without competing with the desktop."""
    import os
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        import psutil
        process = psutil.Process()
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        logging.getLogger(__name__).debug("Could not lower process priority", exc_info=True)

from .asr import StreamingWhisper
from .audio import AudioQueue, WasapiLoopbackCapture
from .config import (languages_from_mode, load_config, load_saved_settings,
                      mode_from_languages, normalize_source_language,
                      normalize_target_language, save_settings)
from .overlay import SubtitleOverlay
from .translator import QwenTranslator


class Application:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self._settings_path = self.config.paths.cache_dir / 'subtitle_settings.json'
        # Explicit diagnostic/fallback TOML files are opt-in configurations;
        # only the normal default profile participates in UI memory.
        self._persist_settings_enabled = Path(config_path).name.lower() == 'config.toml'
        if self._persist_settings_enabled:
            self._restore_saved_settings()
        # The source-language choice is also Whisper's language hint.  Keep
        # the TOML/default path consistent with runtime changes.
        self.config.asr.language = self.config.translation.source_language
        self._stopping = False
        self._caption_id = 0
        self._preview_revision = 0
        self._latest_final = None
        self._subtitle_lock = threading.RLock()
        log_path = self.config.paths.logs_dir / "runtime.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")],
        )
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        self.logger = logging.getLogger(__name__)
        self.audio_queue = AudioQueue()
        self.overlay = SubtitleOverlay(self.config.ui, self.stop, self._on_settings, self.config.translation)
        self.capture = WasapiLoopbackCapture(
            self.config.audio.sample_rate,
            self.config.audio.channels,
            self.config.audio.block_ms,
            self.audio_queue.put,
            on_error=self.overlay.show_status,
            on_status=self.overlay.show_status,
        )
        self.translator: QwenTranslator | None = None
        if self.config.translation.enabled and self.config.translation.target_language != "none":
            self.translator = QwenTranslator(
                self.config.translation,
                self.config.paths.cache_dir / "huggingface",
                self.overlay.show_translation,
                on_error=self.overlay.show_status,
                project_root=self.config.root,
            )
        self.asr = StreamingWhisper(
            self.config.asr,
            self.config.stream,
            self.audio_queue,
            self.overlay.show_partial,
            self._on_final,
            self._on_preview,
        )
        self.asr.configure_translation(
            self.config.translation.enabled and self.config.translation.target_language != 'none',
            self.config.translation.segment_seconds,
        )

    def _restore_saved_settings(self) -> None:
        saved = load_saved_settings(self._settings_path)
        if not saved:
            return
        translation = self.config.translation
        source = normalize_source_language(saved.get('source_language', translation.source_language))
        target = normalize_target_language(saved.get('target_language', translation.target_language))
        # A settings file from an older build only has the combined mode.
        if 'source_language' not in saved and 'target_language' not in saved and 'mode' in saved:
            source, target = languages_from_mode(saved.get('mode'))
        translation.source_language = source
        translation.target_language = target
        translation.mode = mode_from_languages(source, target)
        translation.bilingual = bool(saved.get('bilingual', translation.bilingual))
        translation.enabled = translation.enabled or target != 'none'
        self.config.asr.language = source
        for key in ('source_font_size', 'translation_font_size', 'background_opacity',
                    'text_opacity', 'background_color', 'source_color', 'translation_color',
                    'history_enabled', 'history_display_seconds', 'history_count', 'width'):
            if key in saved:
                setattr(self.config.ui, key, saved[key])

    def _settings_payload(self) -> dict:
        translation = self.config.translation
        return {
            # Keep a combined field for older builds; the two explicit fields
            # are authoritative for the current UI.
            'mode': translation.mode,
            'source_language': translation.source_language,
            'target_language': translation.target_language,
            'bilingual': translation.bilingual,
            'source_font_size': self.config.ui.source_font_size,
            'translation_font_size': self.config.ui.translation_font_size,
            'background_opacity': self.config.ui.background_opacity,
            'text_opacity': self.config.ui.text_opacity,
            'background_color': self.config.ui.background_color,
            'source_color': self.config.ui.source_color,
            'translation_color': self.config.ui.translation_color,
            'history_enabled': self.config.ui.history_enabled,
            'history_display_seconds': self.config.ui.history_display_seconds,
            'history_count': self.config.ui.history_count,
            'width': self.config.ui.width,
        }

    def _persist_settings(self) -> None:
        if not getattr(self, '_persist_settings_enabled', False):
            return
        try:
            save_settings(self._settings_path, self._settings_payload())
        except OSError:
            getattr(self, 'logger', logging.getLogger(__name__)).warning(
                'Could not save subtitle settings', exc_info=True)

    def start(self) -> None:
        self.logger.info("Starting real-time subtitle translator")
        try:
            self.asr.start()
            if self.translator:
                self.translator.start()
            self.capture.start()
            self.overlay.run()
        finally:
            self.stop()

    def _on_final(self, text: str, language: str) -> None:
        with self._subtitle_lock:
            self._preview_revision = 0
            if self.translator:
                self.translator.invalidate_previews()
            self._caption_id += 1
            self._latest_final = (text, language, self._caption_id)
            self.overlay.show_final(text, self._caption_id)
            if self.translator and self.config.translation.target_language != 'none':
                self.translator.submit(text, language, self._caption_id)

    def _on_preview(self, text: str, language: str, revision: int) -> None:
        with self._subtitle_lock:
            if self.translator and self.config.translation.target_language != 'none':
                self._preview_revision = max(self._preview_revision, revision)
                self.overlay.expect_preview(self._caption_id + 1,
                                            self.translator._generation, revision)
                self.translator.submit_preview(
                    text, language, self._caption_id + 1, revision)

    def _on_settings(self, settings: dict) -> None:
        with self._subtitle_lock:
            self._apply_translation_settings(settings)

    def _apply_translation_settings(self, settings: dict) -> None:
        old_mode = self.config.translation.mode
        translation = self.config.translation
        source = normalize_source_language(settings.get('source_language', translation.source_language))
        target = normalize_target_language(settings.get('target_language', translation.target_language))
        if 'source_language' not in settings and 'target_language' not in settings and 'mode' in settings:
            source, target = languages_from_mode(settings['mode'])
        translation.source_language = source
        translation.target_language = target
        translation.mode = mode_from_languages(source, target)
        translation.bilingual = bool(settings['bilingual'])
        self.config.asr.language = source
        configure_language = getattr(self.asr, 'configure_language', None)
        if configure_language:
            configure_language(source)
        self.asr.configure_translation(target != 'none',
                                       self.config.translation.segment_seconds)
        self.logger.info('Subtitle settings applied: source=%s target=%s bilingual=%s',
                         source, target, translation.bilingual)
        if self.translator:
            self.translator.invalidate_previews()
            generation = self.translator.configure(
                translation.mode, translation.bilingual,
                source_language=source, target_language=target)
            self.overlay.show_generation(generation)
        elif target != 'none':
            # A config can start in no-translation mode. Load the already
            # downloaded model only when the user enables translation at runtime.
            self.config.translation.enabled = True
            self.translator = QwenTranslator(
                self.config.translation,
                self.config.paths.cache_dir / "huggingface",
                self.overlay.show_translation,
                on_error=self.overlay.show_status,
                project_root=self.config.root,
            )
            self.translator.start()
            self.overlay.show_generation(0)
        self._persist_settings()
        if old_mode != translation.mode and self._latest_final and self.translator:
            self.translator.submit(*self._latest_final)

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.logger.info("Stopping")
        self._persist_settings()
        self.capture.stop()
        self.asr.stop()
        if self.translator:
            self.translator.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local real-time bilingual system-audio subtitle translator")
    parser.add_argument("--config", default="config.toml", help="Path to a TOML configuration file")
    args = parser.parse_args()
    configure_desktop_friendly_runtime()
    try:
        with SingleInstance():
            Application(args.config).start()
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        logging.exception("Application failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
