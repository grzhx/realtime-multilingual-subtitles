from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from threading import RLock
from dataclasses import dataclass
from pathlib import Path

from .config import (TranslationConfig, languages_from_mode,
                      mode_from_languages, normalize_source_language,
                      normalize_target_language)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslationResult:
    text: str
    caption_id: int
    generation: int
    source_text: str = ''
    preview: bool = False
    revision: int = 0


class QwenTranslator:
    """Single-worker local translator with safe runtime direction changes."""

    def __init__(self, config: TranslationConfig, cache_dir, on_translation: Callable[[TranslationResult], None],
                 on_error: Callable[[str], None] | None = None, project_root=None):
        self.config = config
        self.cache_dir = cache_dir
        self.on_translation = on_translation
        self.on_error = on_error
        self.project_root = Path(project_root) if project_root else Path(cache_dir).parent.parent
        self._server = None
        # Q4 inference is fast enough for several short committed segments.
        # Keep a small FIFO so stable chunks are translated in order; dropping
        # the whole queue was the reason long speech skipped captions.
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._preview_queue: queue.Queue = queue.Queue(maxsize=1)
        self._latest_preview = None
        self._work_available = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tokenizer = None
        self._model = None
        self._history: deque[tuple[str, str]] = deque(maxlen=config.context_segments)
        self._source_language = normalize_source_language(config.source_language)
        self._target_language = normalize_target_language(config.target_language)
        self._mode = mode_from_languages(self._source_language, self._target_language) \
            if config.enabled else "off"
        self._generation = 0
        self._state_lock = RLock()
        self._ready = threading.Event()
        self._failure = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._initialize_and_run, name="translation", daemon=True)
        self._thread.start()

    def _initialize_and_run(self):
        try:
            self._load_model()
            self._ready.set()
            self._run()
        except Exception as exc:
            self._failure = exc
            self._ready.set()
            logger.exception('Translation model initialization failed')
            if self.on_error:
                self.on_error(f'翻译模型加载失败：{exc}')
        finally:
            if self._server:
                self._server.stop()

    def stop(self) -> None:
        self._stop.set()
        self._work_available.set()
        if self._server:
            self._server.stop()
        if self._thread:
            self._thread.join(timeout=5)

    def submit(self, text: str, source_language: str, caption_id: int = 0) -> None:
        with self._state_lock:
            if self._mode == 'off':
                return
            task = (text, source_language, self._generation, self._mode, caption_id)
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                # Drop only the oldest pending task; never discard every chunk.
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(task)
            self._work_available.set()

    def submit_preview(self, text: str, source_language: str, caption_id: int,
                       revision: int) -> None:
        """Submit only the newest provisional translation request."""
        with self._state_lock:
            if self._mode == 'off' or not text.strip():
                return
            try:
                self._preview_queue.get_nowait()
            except queue.Empty:
                pass
            self._preview_queue.put_nowait(
                (text, source_language, self._generation, self._mode,
                 caption_id, True, revision, time.perf_counter()))
            self._latest_preview = (self._generation, caption_id, revision)
            self._work_available.set()

    def invalidate_previews(self) -> None:
        with self._state_lock:
            self._latest_preview = None
            while True:
                try:
                    self._preview_queue.get_nowait()
                except queue.Empty:
                    break

    def _drain_pending(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.invalidate_previews()

    def configure(self, mode: str, bilingual: bool | None = None,
                  source_language: str | None = None,
                  target_language: str | None = None) -> int:
        """Apply a new direction immediately and invalidate old results."""
        with self._state_lock:
            if source_language is None or target_language is None:
                source_language, target_language = languages_from_mode(mode)
            source_language = normalize_source_language(source_language)
            target_language = normalize_target_language(target_language)
            requested = mode_from_languages(source_language, target_language)
            if bilingual is not None:
                self.config.bilingual = bilingual
            if (requested == self._mode and source_language == self._source_language
                    and target_language == self._target_language):
                return self._generation
            self._mode = requested
            self._source_language = source_language
            self._target_language = target_language
            self._generation += 1
            self.config.mode = requested
            self.config.source_language = source_language
            self.config.target_language = target_language
            logger.info('Translation languages changed: %s -> %s (generation %s)',
                        source_language, target_language, self._generation)
            self._history.clear()
            self._drain_pending()
            return self._generation

    def _load_model(self) -> None:
        if self.config.backend == 'llama_cpp':
            from .gguf import LlamaServer
            self._server = LlamaServer(self.config, self.project_root)
            self._server.start(self._stop)
            if not self._stop.is_set():
                logger.info('Warming up structured translation before processing subtitles')
                self._server.translate('Please close the door before you leave.', 'en_to_zh', 64)
                if not self._stop.is_set():
                    self._server.translate('我们需要先讨论这个方案的优点和可能出现的问题。', 'zh_to_en', 96)
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(2)
        if torch.cuda.is_available():
            try:
                torch.set_num_interop_threads(1)
            except RuntimeError:
                logger.debug("Torch inter-op pool was already initialized", exc_info=True)
        logger.info("Loading translation model: %s", self.config.model_id)
        dtype = torch.float16 if self.config.dtype.lower() in {"float16", "fp16"} and torch.cuda.is_available() else torch.float32
        device = self.config.device if self.config.device != "cuda" or torch.cuda.is_available() else "cpu"
        self._tokenizer = AutoTokenizer.from_pretrained(self.config.model_id, cache_dir=str(self.cache_dir))
        self._model = AutoModelForCausalLM.from_pretrained(
            self.config.model_id, cache_dir=str(self.cache_dir), dtype=dtype,
            device_map="auto" if device == "cuda" else None,
        )
        if device != "cuda":
            self._model.to(device)
        self._model.eval()
        logger.info("Translation model loaded on %s", device)

    def _run(self) -> None:
        last_was_preview = False
        while not self._stop.is_set():
            self._work_available.clear()
            # Preview work is latest-only and short. Let it run ahead of one
            # pending final item; if finals build up, drain finals first.
            try:
                if self._queue.qsize() > 2 or (last_was_preview and not self._queue.empty()):
                    raise queue.Empty
                task = self._preview_queue.get_nowait()
            except queue.Empty:
                try:
                    task = self._queue.get_nowait()
                except queue.Empty:
                    try:
                        task = self._preview_queue.get_nowait()
                    except queue.Empty:
                        self._work_available.wait(0.2)
                        continue
            if len(task) == 5:
                text, source_language, generation, mode, caption_id = task
                preview, revision, submitted_at = False, 0, 0.0
            else:
                (text, source_language, generation, mode, caption_id,
                 preview, revision, submitted_at) = task
            last_was_preview = preview
            try:
                with self._state_lock:
                    if generation != self._generation or mode != self._mode:
                        continue
                    if preview and self._latest_preview != (generation, caption_id, revision):
                        continue
                started = time.perf_counter()
                translated = self.translate(text, source_language, mode=mode)
                elapsed = time.perf_counter() - started
                if preview:
                    total_elapsed = max(0.0, time.perf_counter() - submitted_at)
                    queue_elapsed = max(0.0, total_elapsed - elapsed)
                    logger.info('Preview translation ready: revision=%s model=%.3fs queue=%.3fs',
                                revision, elapsed, queue_elapsed)
                elif elapsed > 0.75:
                    logger.info("Translation took %.3fs", elapsed)
                with self._state_lock:
                    if (translated and generation == self._generation
                            and self._mode == mode and not self._stop.is_set()):
                        if preview and self._latest_preview != (generation, caption_id, revision):
                            continue
                        translated = self._clean_output(translated)
                        if not translated:
                            continue
                        self.on_translation(TranslationResult(
                            translated, caption_id, generation, text, preview, revision))
                        if not preview:
                            self._history.append((text, translated))
            except Exception:
                if not self._stop.is_set():
                    if preview:
                        logger.warning('Preview translation failed; awaiting a newer revision', exc_info=True)
                        continue
                    logger.exception("Translation failed; keeping source subtitle")
                    if self.on_error:
                        self.on_error('翻译失败，保留原文；下一段继续重试')

    def translate(self, text: str, source_language: str, mode: str | None = None) -> str:
        if mode is None:
            with self._state_lock:
                mode = self._mode
        if mode == 'off':
            return ''
        if self.config.backend == 'llama_cpp':
            if not self._server:
                raise RuntimeError('GGUF translation server not loaded')
            failure = None
            for attempt in range(2):
                try:
                    # A length stop is recoverable: give the repair request a
                    # little more room, while keeping the normal request at
                    # the configured low-latency budget.
                    token_budget = self._token_budget(text)
                    if attempt:
                        token_budget = min(max(token_budget * 2, 256), 512)
                    result = self._server.translate(text, mode, token_budget,
                                                    strict=bool(attempt))
                    result = self._clean_output(result)
                    self._validate_output(result, text, mode)
                    return result
                except (ValueError, KeyError, TypeError) as exc:
                    failure = exc
                    logger.warning('Invalid translation, attempt %s: %s', attempt + 1, exc)
                    if self._stop.is_set():
                        return ''
            raise ValueError(f'Unable to produce a valid translation: {failure}')
        if not self._model or not self._tokenizer:
            return ""
        import torch

        if mode is None:
            with self._state_lock:
                mode = self._mode
        if mode == "off":
            return ""
        # Keep each short subtitle independent. Feeding old outputs back into
        # the 0.6B model can make it repeat stale lines after a direction switch.
        system, user = self._translation_prompt(text, mode)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            prompt = self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
            )
        except TypeError:
            prompt = self._tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self._tokenizer(prompt, return_tensors="pt")
        model_device = next(self._model.parameters()).device
        inputs = {key: value.to(model_device) for key, value in inputs.items()}
        with torch.inference_mode():
            output = self._model.generate(
                **inputs, max_new_tokens=self.config.max_new_tokens,
                do_sample=False, repetition_penalty=1.05,
            )
        generated = output[:, inputs["input_ids"].shape[1]:]
        return self._tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()

    def _token_budget(self, text: str) -> int:
        """Reserve more output room only for unusually long live captions."""
        base = max(64, int(self.config.max_new_tokens))
        # Translation output is usually shorter than the source, but JSON
        # structure and a long subtitle still need headroom. Short captions
        # keep the exact configured budget and therefore the same latency.
        length_based = 64 + len(text.strip()) * 2
        return min(512, max(base, length_based))

    @staticmethod
    def _validate_output(text: str, source: str, mode: str):
        if not text.strip() or re.search(r'<\/?(?:think|translation|label|subtitle)\b', text, re.I):
            raise ValueError('Empty translation or leaked markup')
        if re.match(r'^(?:标签|标题|当前标题|当前字幕|译文|翻译结果|label|title|translation)\s*[:：]', text, re.I):
            raise ValueError('Translation still contains a generated label')
        han = len(re.findall(r'[\u4e00-\u9fff]', text))
        latin = len(re.findall(r'[A-Za-z]', text))
        meaningful = bool(re.search(r'[A-Za-z\u4e00-\u9fff]', source))
        target = mode.rsplit('_to_', 1)[-1]
        if target == 'zh' and meaningful and not han and len(re.findall(r'[A-Za-z]+', source)) >= 3:
            raise ValueError('Expected Chinese but received no Chinese text')
        if target == 'en' and re.search(r'[\u4e00-\u9fff]', source) and han > latin:
            raise ValueError('Expected English but received predominantly Chinese text')

    @staticmethod
    def _clean_output(text: str) -> str:
        """Remove prompt labels that a small instruct model sometimes echoes."""
        value = re.sub(r'\s+', ' ', text.replace('\r', ' ').replace('\n', ' ')).strip()
        prefix = r'^(?:标签|当前标题|当前字幕|译文|翻译结果|current (?:title|subtitle)|translation|English translation|Simplified Chinese translation)\s*[:：]\s*'
        for _ in range(3):
            cleaned = re.sub(prefix, '', value, flags=re.I)
            if cleaned == value:
                break
            value = cleaned
        if len(value) >= 2 and (value[0], value[-1]) in {('"', '"'), ("'", "'"), ('“', '”')}:
            value = value[1:-1].strip()
        return value

    @staticmethod
    def _translation_prompt(text: str, mode: str, context: str = '') -> tuple[str, str]:
        target = 'Simplified Chinese' if mode.rsplit('_to_', 1)[-1] == 'zh' else 'English'
        system = (
            f'Translate the current subtitle into {target}. '
            f'Your entire response must be in {target}. '
            'Output only the translated subtitle, with no explanations or labels. '
            'Translate every ordinary word; retain only proper names, code and numbers. '
            'If the subtitle is already in the target language, return it unchanged. '
        )
        # The user message is only the text. Do not offer a label the model can
        # repeat literally as "当前标题" / "Current subtitle".
        user = text
        return system, user
