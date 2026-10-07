from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable

import numpy as np

from .audio import AudioBlock, AudioQueue
from .config import AsrConfig, StreamConfig
from .vad import WebRtcVad
from .stability import StableTranscript, Word, join_words

logger = logging.getLogger(__name__)


def _to_mono_16k(samples: np.ndarray, source_rate: int) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    samples = np.clip(samples, -1.0, 1.0)
    if source_rate == 16000:
        return samples
    target_len = max(1, int(round(len(samples) * 16000 / source_rate)))
    try:
        from scipy.signal import resample_poly
        import math
        gcd = math.gcd(source_rate, 16000)
        return resample_poly(samples, 16000 // gcd, source_rate // gcd).astype(np.float32)
    except ImportError:
        old_x = np.linspace(0.0, 1.0, len(samples), endpoint=False)
        new_x = np.linspace(0.0, 1.0, target_len, endpoint=False)
        return np.interp(new_x, old_x, samples).astype(np.float32)


class StreamingWhisper:
    def __init__(self, asr: AsrConfig, stream: StreamConfig, audio_queue: AudioQueue,
                 on_partial: Callable[[str], None], on_final: Callable[[str, str], None],
                 on_preview: Callable[[str, str, int], None] | None = None):
        self.asr = asr
        self.stream = stream
        self.audio_queue = audio_queue
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_preview = on_preview
        self.vad = WebRtcVad()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._model = None
        self._buffer = np.empty(0, dtype=np.float32)
        self._last_update = 0.0
        self._last_voice = 0.0
        self._speech_started = False
        self._last_partial = ""
        self._last_language = "en"
        self._speech_started_at = 0.0
        self._stream_id: int | None = None
        self._last_transcribe_seconds = 0.0
        self._translation_enabled = False
        self._translation_segment_seconds = 2.0
        self._commit_interval_seconds = max(0.4, self.stream.commit_interval_ms / 1000.0)
        self._last_commit_decode = 0.0
        self._request_word_timestamps = False
        self._stability = StableTranscript()
        self._buffer_start = 0.0
        self._decoded_words = []
        self._last_committed_text = ''
        self._decode_rejected_all = False
        self._sentence_words: list[Word] = []
        self._silence_samples = 0
        self._preview_revision = 0
        self._last_preview_text = ''
        self._metrics = {
            'partial_decodes': 0, 'commit_decodes': 0,
            'partial_seconds': 0.0, 'commit_seconds': 0.0,
            'max_audio_queue': 0, 'max_queue_age_seconds': 0.0,
            'committed_segments': 0, 'commit_latency_seconds': 0.0,
            'last_commit_latency_seconds': 0.0,
            'hallucination_segments_rejected': 0,
        }
        self._last_metrics_log = 0.0

    def configure_translation(self, enabled: bool, segment_seconds: float = 2.0) -> None:
        """Bound audio wait time for translation without changing ASR-only mode."""
        self._translation_segment_seconds = max(1.0, min(float(segment_seconds),
                                                          self.stream.max_segment_seconds))
        self._translation_enabled = bool(enabled)
        self._last_commit_decode = 0.0
        self._last_preview_text = ''

    def configure_language(self, language: str) -> None:
        """Change Whisper's language hint for subsequent audio windows."""
        language = str(language or 'auto').lower()
        language = language if language in {'auto', 'zh', 'en'} else 'auto'
        if language != self.asr.language and self._speech_started:
            # Do not decode audio collected under the previous language hint.
            self._buffer = np.empty(0, dtype=np.float32)
            self._speech_started = False
            self._speech_started_at = 0.0
            self._stability = StableTranscript()
            self._decoded_words = []
            self._buffer_start = 0.0
            self._last_commit_decode = 0.0
            self._last_committed_text = ''
            self._sentence_words = []
            self._silence_samples = 0
            self._last_preview_text = ''
        self.asr.language = language
        self._last_language = self.asr.language if self.asr.language != 'auto' else 'en'
        self._last_partial = ''

    def start(self) -> None:
        self._load_model()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="streaming-asr", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _load_model(self) -> None:
        from faster_whisper import WhisperModel
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        logger.info("Loading ASR model: %s", self.asr.model_size_or_path)
        self._model = WhisperModel(
            self.asr.model_size_or_path,
            device=self.asr.device,
            compute_type=self.asr.compute_type,
            download_root=str(self.asr.download_root),
            cpu_threads=self.asr.cpu_threads,
            num_workers=self.asr.num_workers,
        )
        logger.info("ASR model loaded")

    def _run(self) -> None:
        while not self._stop.is_set():
            block = self.audio_queue.get(timeout=0.2)
            if block is None:
                continue
            self._consume(block)
        if self._buffer.size and self._speech_started:
            self._finalize()

    def metrics_snapshot(self) -> dict:
        """Return a copy of ASR timing counters for diagnostics."""
        result = dict(self._metrics)
        result['audio_queue_depth'] = self.audio_queue.queue.qsize() if self.audio_queue else 0
        result['audio_blocks_dropped'] = getattr(self.audio_queue, 'dropped_blocks', 0) if self.audio_queue else 0
        result['partial_average_seconds'] = (
            result['partial_seconds'] / result['partial_decodes']
            if result['partial_decodes'] else 0.0)
        result['commit_average_seconds'] = (
            result['commit_seconds'] / result['commit_decodes']
            if result['commit_decodes'] else 0.0)
        result['commit_latency_average_seconds'] = (
            result['commit_latency_seconds'] / result['committed_segments']
            if result['committed_segments'] else 0.0)
        return result

    def _maybe_log_metrics(self, now: float) -> None:
        if now - self._last_metrics_log < 30.0:
            return
        self._last_metrics_log = now
        logger.info('ASR metrics: %s', self.metrics_snapshot())

    def _consume(self, block: AudioBlock) -> None:
        if block.stream_id != self._stream_id:
            # Never join speech from disconnected/different output devices.
            self._stream_id = block.stream_id
            self._buffer = np.empty(0, dtype=np.float32)
            self._speech_started = False
            self._speech_started_at = 0.0
            self._last_partial = ""
            self._last_update = 0.0
            self._last_voice = 0.0
            self._stability = StableTranscript()
            self._buffer_start = 0.0
            self._decoded_words = []
            self._sentence_words = []
            self._silence_samples = 0
            self._last_committed_text = ''
            self._last_preview_text = ''
            self.on_partial("")
        audio = _to_mono_16k(block.samples, block.sample_rate)
        if audio.size == 0:
            return
        queue_depth = self.audio_queue.queue.qsize() if self.audio_queue else 0
        self._metrics['max_audio_queue'] = max(self._metrics['max_audio_queue'], queue_depth)
        if getattr(block, 'captured_at', 0.0):
            age = max(0.0, time.monotonic() - block.captured_at)
            self._metrics['max_queue_age_seconds'] = max(
                self._metrics['max_queue_age_seconds'], age)
        voiced = self.vad.chunk_voiced(audio)
        now = time.monotonic()
        if voiced:
            self._silence_samples = 0
            self._last_voice = now
            if not self._speech_started:
                self._speech_started_at = now
            self._speech_started = True
        elif self._speech_started:
            self._silence_samples += len(audio)
        if self._speech_started:
            self._buffer = np.concatenate((self._buffer, audio))
            max_samples = int(self.stream.max_segment_seconds * 16000)
            if len(self._buffer) >= max_samples:
                # Finalization also publishes the text, so do not run Whisper
                # twice over the same buffer at a forced segment boundary.
                self._finalize(forced=True)
                return
            if (now - self._last_update) * 1000 >= self.stream.update_interval_ms and len(self._buffer) >= 16000:
                self._emit_partial(force=False)
                if now - self._last_commit_decode >= self._commit_interval_seconds:
                    self._commit_stable()
        if self._speech_started and not voiced:
            silence_ms = self._silence_samples / 16
            if silence_ms >= self.stream.sentence_end_ms and len(self._buffer) >= int(self.stream.min_speech_ms * 16):
                self._finalize()
        self._maybe_log_metrics(now)

    def _transcribe(self) -> tuple[str, str]:
        self._decode_rejected_all = False
        if self._model is None or self._buffer.size == 0:
            return "", self._last_language
        language = None if self.asr.language.lower() == "auto" else self.asr.language
        started = time.perf_counter()
        segments, info = self._model.transcribe(
            self._buffer,
            language=language,
            beam_size=self.asr.beam_size,
            vad_filter=self.asr.vad_filter,
            condition_on_previous_text=False,
            temperature=0.0,
            without_timestamps=not self._request_word_timestamps,
            word_timestamps=self._request_word_timestamps,
        )
        segments = list(segments)
        had_text = any(segment.text.strip() for segment in segments)
        # Confidence values are produced by the existing Whisper decode. This
        # filter adds no second pass, VAD model or stability waiting period.
        segments = [segment for segment in segments if self._accept_segment(segment)]
        self._decode_rejected_all = had_text and not segments
        if self._request_word_timestamps:
            self._decoded_words = [Word(self._buffer_start + word.start,
                                        self._buffer_start + word.end, word.word)
                                   for segment in segments for word in (segment.words or [])]
        else:
            self._decoded_words = []
        text = " ".join(segment.text.strip() for segment in segments).strip()
        detected = getattr(info, "language", None) or self._last_language
        elapsed = time.perf_counter() - started
        key = 'commit' if self._request_word_timestamps else 'partial'
        self._metrics[f'{key}_decodes'] += 1
        self._metrics[f'{key}_seconds'] += elapsed
        return text, detected

    def _accept_segment(self, segment) -> bool:
        text = getattr(segment, 'text', '').strip()
        if not text:
            return False
        no_speech = getattr(segment, 'no_speech_prob', 0.0)
        logprob = getattr(segment, 'avg_logprob', 0.0)
        compression = getattr(segment, 'compression_ratio', 0.0)
        # These are ordinary words too: reject them only when acoustic
        # confidence supports a non-speech/hallucination interpretation.
        key = re.sub(r'[^a-z]+', ' ', text.lower()).strip()
        common_short_hallucination = key in {'you', 'thank you', 'thanks', 'bye', 'bye bye'}
        weak = logprob < self.asr.hallucination_avg_logprob
        reject = (
            (no_speech >= self.asr.hallucination_no_speech_prob and weak)
            or (compression > self.asr.hallucination_compression_ratio and weak)
            # In the captured turbo music sample no_speech_prob stayed zero
            # even for low-confidence "you" hallucinations. Its log probability
            # still distinguishes these from confident, real short speech.
            or (common_short_hallucination and weak)
            or (common_short_hallucination and no_speech >= 0.40 and logprob < -0.55)
        )
        if reject:
            self._metrics['hallucination_segments_rejected'] += 1
            logger.info('ASR hallucination filtered: no_speech=%.3f logprob=%.3f compression=%.3f text=%r',
                        no_speech, logprob, compression, text)
            return False
        if common_short_hallucination:
            logger.info('ASR short candidate retained: path=%s no_speech=%.3f logprob=%.3f text=%r',
                        'commit' if self._request_word_timestamps else 'partial',
                        no_speech, logprob, text)
        return True

    def _emit_partial(self, force: bool) -> None:
        # Fast path: partial text does not need word timestamps. Timestamp
        # decoding is reserved for the lower-frequency commit path below.
        self._request_word_timestamps = False
        started = time.perf_counter()
        text, language = self._transcribe()
        if self._decoded_words and self._stability.committed_end:
            display_words = [word for word in self._decoded_words
                             if word.end > self._stability.committed_end + 0.02]
            text = join_words(display_words)
        elif self._last_committed_text:
            text = self._remove_committed_overlap(text)
        text = self._sentence_text(text)
        self._last_transcribe_seconds = time.perf_counter() - started
        if self._last_transcribe_seconds > 0.5:
            logger.debug("ASR transcription took %.3fs for %.2fs of audio",
                         self._last_transcribe_seconds, len(self._buffer) / 16000)
        self._last_update = time.monotonic()
        if text and (force or text != self._last_partial):
            self._last_partial = text
            self._last_language = language
            self.on_partial(text)
        elif self._decode_rejected_all:
            self._retract_rejected_partial()

    def _retract_rejected_partial(self):
        # Only withdraw provisional text; keep already committed sentence
        # context and never create a final/history entry from a rejected decode.
        text = self._sentence_text()
        if text != self._last_partial:
            self._last_partial = text
            self.on_partial(text)

    def _commit_stable(self):
        self._request_word_timestamps = True
        self._last_commit_decode = time.monotonic()
        text, language = self._transcribe()
        if not self._decoded_words:
            if self._decode_rejected_all:
                self._stability.observe([])
                self._retract_rejected_partial()
            return
        self._last_language = language
        self._stability.observe(self._decoded_words)
        audio_end = self._buffer_start + len(self._buffer) / 16000
        safe_stable = [word for word in self._stability.pending[:self._stability.stable_count]
                       if word.end <= audio_end - 0.25]
        words = self._stability.choose(
            audio_end, self._translation_segment_seconds,
            self.stream.min_commit_seconds,
            min_words=4 if language == 'en' else 3)
        self._commit_words(words, language)
        remaining = [word for word in safe_stable
                     if (word.start + word.end) / 2 > self._stability.committed_end + 0.02]
        preview_text = self._sentence_text(join_words(remaining))
        preview_ready = (bool(self._sentence_words) or
                         (audio_end - self._stability.committed_end >= self.stream.preview_translation_seconds
                          and len(remaining) >= 3))
        self._emit_preview(preview_text, language, preview_ready)

    def _commit_words(self, words, language, allow_sentence_end=True):
        text = self._stability.commit(words)
        if not text:
            return
        self._last_committed_text = text
        # Committing audio only makes its words immutable. It does not close
        # the displayed sentence; only a confirmed terminal mark does that.
        for word in words:
            self._sentence_words.append(word)
            if allow_sentence_end and word.sentence_end:
                self._finish_sentence(language, 'punctuation')
        if self._sentence_words or self._stability.pending:
            self._publish_sentence(join_words(self._stability.pending), language)
        else:
            self._last_partial = ''
        # Keep real audio before the boundary so the next recognition includes
        # the end of the previous word; timestamps prevent translating it twice.
        overlap = max(0.4, self.stream.overlap_seconds)
        drop_seconds = max(0.0, self._stability.committed_end - overlap - self._buffer_start)
        drop_samples = min(len(self._buffer), int(drop_seconds * 16000))
        self._buffer = self._buffer[drop_samples:]
        self._buffer_start += drop_samples / 16000

    def _sentence_text(self, tail=''):
        prefix = join_words(self._sentence_words)
        if not prefix:
            return tail.strip()
        if not tail.strip():
            return prefix
        separator = '' if re.search(r'[\u3040-\u30ff\u3400-\u9fff]$', prefix) else ' '
        return prefix + separator + tail.strip()

    def _publish_sentence(self, tail, language):
        text = self._sentence_text(tail)
        if text and text != self._last_partial:
            self._last_partial = text
            self.on_partial(text)

    def _emit_preview(self, text, language, ready):
        if (self._translation_enabled and self.on_preview and ready and
                text and text != self._last_preview_text):
            self._preview_revision += 1
            self._last_preview_text = text
            self.on_preview(text, language, self._preview_revision)

    def _finish_sentence(self, language, reason):
        text = self._sentence_text()
        if not text:
            return
        self.on_final(text, language)
        logger.info('Sentence completed: reason=%s language=%s words=%s characters=%s',
                    reason, language, len(self._sentence_words), len(text))
        self._sentence_words = []
        self._last_preview_text = ''
        self._last_partial = ''
        if self._speech_started_at:
            latency = max(0.0, time.monotonic() - self._speech_started_at)
            self._metrics['committed_segments'] += 1
            self._metrics['commit_latency_seconds'] += latency
            self._metrics['last_commit_latency_seconds'] = latency
            # Measure the next committed phrase independently instead of
            # charging all consecutive speech to the first segment.
            self._speech_started_at = time.monotonic()

    def _finalize(self, forced=False) -> None:
        self._request_word_timestamps = True
        text, language = self._transcribe()
        if self._decode_rejected_all:
            self._retract_rejected_partial()
        if self._decoded_words:
            self._stability.observe(self._decoded_words)
            if forced:
                audio_end = self._buffer_start + len(self._buffer) / 16000
                safe = [w for w in self._stability.pending if w.end <= audio_end - 0.35]
                if len(safe) > 1:
                    # Roll the audio window forward without creating a new
                    # caption. The accumulated sentence remains on screen.
                    self._commit_words(safe[:-1], language, allow_sentence_end=False)
                    self._emit_preview(self._sentence_text(), language, bool(self._sentence_words))
                    self._last_update = time.monotonic()
                    return
                self._last_update = time.monotonic()
                return
            self._commit_words(list(self._stability.pending), language)
            text = ''
        elif forced:
            # Whisper can occasionally return text without word metadata.
            # Keep that text in the open sentence and roll audio anyway so a
            # missing timestamp result cannot grow the live buffer unbounded.
            tail = self._remove_committed_overlap(text)
            if tail:
                self._sentence_words.append(Word(self._buffer_start,
                    self._buffer_start + len(self._buffer) / 16000, ' ' + tail))
                self._last_committed_text = tail
                self._publish_sentence('', language)
                self._emit_preview(self._sentence_text(), language, True)
            retain = int(max(0.4, self.stream.overlap_seconds) * 16000)
            dropped = max(0, len(self._buffer) - retain)
            self._buffer = self._buffer[dropped:]
            self._buffer_start += dropped / 16000
            self._last_update = time.monotonic()
            return
        if text:
            text = self._remove_committed_overlap(text)
            self._sentence_words.append(Word(self._buffer_start,
                self._buffer_start + len(self._buffer) / 16000, ' ' + text))
        self._finish_sentence(language, 'pause')
        self._buffer = np.empty(0, dtype=np.float32)
        self._speech_started = False
        self._speech_started_at = 0.0
        self._last_partial = ""
        self._last_update = 0.0
        self._last_commit_decode = 0.0
        self._stability = StableTranscript()
        self._buffer_start = 0.0
        self._decoded_words = []
        self._last_committed_text = ''
        self._silence_samples = 0
        self._last_preview_text = ''

    def _remove_committed_overlap(self, text: str) -> str:
        """Remove the short overlap retained for Whisper context from partials."""
        if not text or not self._last_committed_text:
            return text
        current = text.strip()
        committed = self._last_committed_text.strip()
        current_words = current.split()
        committed_words = committed.split()
        for count in range(min(len(current_words), len(committed_words)), 0, -1):
            left = re.sub(r'\W+', '', ' '.join(current_words[:count]), flags=re.UNICODE).casefold()
            right = re.sub(r'\W+', '', ' '.join(committed_words[-count:]), flags=re.UNICODE).casefold()
            if left and left == right:
                return ' '.join(current_words[count:]).strip()
        return current
