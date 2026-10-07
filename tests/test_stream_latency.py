"""Use a fake ASR engine to verify audio dispatch latency and decode count."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from realtime_subtitle.asr import StreamingWhisper
from realtime_subtitle.audio import AudioBlock
from realtime_subtitle.config import load_config
from realtime_subtitle.stability import Word, StableTranscript, join_words


class StreamLatencyTest(unittest.TestCase):
    def setUp(self):
        config = load_config(Path(__file__).resolve().parents[1] / 'config.toml')
        self.finals, self.partials = [], []
        self.asr = StreamingWhisper(config.asr, config.stream, None,
                                   self.partials.append, lambda *args: self.finals.append(args))
        self.asr.vad = SimpleNamespace(chunk_voiced=lambda _: True)
        self.asr._transcribe = Mock(return_value=('Text from the latest audio chunk', 'en'))
        self.block = AudioBlock(np.full((1600, 2), 0.1, dtype=np.float32), 16000, stream_id=1)

    def feed(self, count):
        for _ in range(count):
            self.asr._consume(self.block)

    def test_translation_keeps_audio_until_prefix_is_stable(self):
        self.asr.configure_translation(True, 2)
        with patch('realtime_subtitle.asr.time.monotonic', return_value=10):
            self.feed(19)
            self.assertEqual(self.finals, [])
            self.assertTrue(self.partials, 'Partial source must already be available')
            before = self.asr._transcribe.call_count
            self.feed(1)
            self.assertEqual(len(self.finals), 0)
            self.assertEqual(self.asr._transcribe.call_count, before)
            self.feed(20)
            self.assertEqual(len(self.finals), 0)

    def test_language_switch_discards_audio_from_previous_hint(self):
        self.asr._speech_started = True
        self.asr._buffer = np.ones(16000, dtype=np.float32)
        self.asr.configure_language('zh')
        self.assertEqual(self.asr.asr.language, 'zh')
        self.assertEqual(self.asr._buffer.size, 0)
        self.assertFalse(self.asr._speech_started)

    def test_music_hallucination_filter_preserves_confident_short_speech(self):
        for text in ('You.', 'Thank you!', 'Thanks.', 'Bye.'):
            self.assertFalse(self.asr._accept_segment(SimpleNamespace(
                text=text, no_speech_prob=.5, avg_logprob=-.8, compression_ratio=1.0)))
            self.assertTrue(self.asr._accept_segment(SimpleNamespace(
                text=text, no_speech_prob=.02, avg_logprob=-.2, compression_ratio=1.0)))
        self.assertTrue(self.asr._accept_segment(SimpleNamespace(
            text='You should keep this complete sentence.', no_speech_prob=.02,
            avg_logprob=-.2, compression_ratio=1.0)))
        self.assertTrue(self.asr._accept_segment(SimpleNamespace(text='Thank you.')))

    def test_short_music_hallucination_with_zero_no_speech_probability(self):
        for text in ('you', 'thank you'):
            self.assertFalse(self.asr._accept_segment(SimpleNamespace(
                text=text, no_speech_prob=0.0, avg_logprob=-1.2, compression_ratio=.3)))
            self.assertTrue(self.asr._accept_segment(SimpleNamespace(
                text=text, no_speech_prob=0.0, avg_logprob=-.2, compression_ratio=.3)))

    def test_filtered_commit_retracts_earlier_partial_without_final(self):
        a = self.asr
        a._transcribe = Mock(return_value=('you', 'en'))
        a._emit_partial(False)
        self.assertEqual(self.partials, ['you'])
        def rejected():
            a._decode_rejected_all = True
            a._decoded_words = []
            return '', 'en'
        a._transcribe.side_effect = rejected
        a._commit_stable()
        self.assertEqual(self.partials, ['you', ''])
        self.assertEqual(self.finals, [])

    def test_filtered_partial_preserves_committed_sentence_prefix(self):
        a = self.asr
        a._sentence_words = [Word(0, 1, ' We need help')]
        a._last_partial = 'We need help you'
        a._decode_rejected_all = True
        a._transcribe = Mock(return_value=('', 'en'))
        a._emit_partial(False)
        self.assertEqual(self.partials, ['We need help'])
        self.assertEqual(self.finals, [])

    def test_high_confidence_short_partial_is_still_immediate(self):
        a = self.asr
        a._transcribe = Mock(return_value=('Thank you.', 'en'))
        a._emit_partial(False)
        self.assertEqual(self.partials, ['Thank you.'])

    def test_confident_music_like_output_is_not_blacklisted(self):
        # This music sample is indistinguishable from a real short phrase
        # using only available decoder evidence; keep it as requested.
        self.assertTrue(self.asr._accept_segment(SimpleNamespace(
            text='Thank you.', no_speech_prob=0.0,
            avg_logprob=-.488219559, compression_ratio=.55555555)))

    def test_confidence_filter_removes_text_and_words_in_one_decode(self):
        a = self.asr
        a._buffer = np.zeros(16000, dtype=np.float32)
        a._request_word_timestamps = True
        false_word = SimpleNamespace(start=0, end=.4, word=' You.')
        true_word = SimpleNamespace(start=.5, end=.9, word=' Hello.')
        bad = SimpleNamespace(text='You.', no_speech_prob=.7, avg_logprob=-1.1,
                              compression_ratio=1.0, words=[false_word])
        good = SimpleNamespace(text='Hello.', no_speech_prob=.02, avg_logprob=-.2,
                               compression_ratio=1.0, words=[true_word])
        a._model = SimpleNamespace(transcribe=Mock(return_value=(iter([bad, good]),
                                                                SimpleNamespace(language='en'))))
        # Restore the actual implementation instead of the fixture's fake decode.
        a._transcribe = StreamingWhisper._transcribe.__get__(a)
        self.assertEqual(a._transcribe(), ('Hello.', 'en'))
        self.assertEqual(join_words(a._decoded_words), 'Hello.')
        self.assertEqual(a._model.transcribe.call_count, 1)
        self.assertEqual(a._metrics['hallucination_segments_rejected'], 1)

    def test_general_filter_requires_weak_acoustic_confidence(self):
        self.assertFalse(self.asr._accept_segment(SimpleNamespace(
            text='Some invented subtitle.', no_speech_prob=.8,
            avg_logprob=-1.2, compression_ratio=1.0)))
        self.assertFalse(self.asr._accept_segment(SimpleNamespace(
            text='Repeating text over and over.', no_speech_prob=.1,
            avg_logprob=-1.2, compression_ratio=3.0)))
        self.assertTrue(self.asr._accept_segment(SimpleNamespace(
            text='A softly spoken real sentence.', no_speech_prob=.7,
            avg_logprob=-.2, compression_ratio=1.0)))

    def test_audio_window_limit_does_not_complete_sentence(self):
        self.asr.configure_translation(False)
        with patch('realtime_subtitle.asr.time.monotonic', return_value=10):
            blocks = int(self.asr.stream.max_segment_seconds * 10)
            self.feed(blocks - 1)
            self.assertEqual(self.finals, [])
            self.feed(1)
            self.assertEqual(self.finals, [])

    def test_enabling_translation_does_not_hard_cut_existing_long_buffer(self):
        with patch('realtime_subtitle.asr.time.monotonic', return_value=10):
            self.feed(30)
            self.assertFalse(self.finals)
            self.asr.configure_translation(True)
            self.feed(1)
            self.assertEqual(len(self.finals), 0)
            self.asr.configure_translation(False)
            self.feed(30)
            self.assertEqual(self.finals, [])

    def test_silence_commits_short_phrases_early(self):
        self.asr.configure_translation(True)
        with patch('realtime_subtitle.asr.time.monotonic', return_value=10):
            self.feed(7)
        self.asr.vad.chunk_voiced = lambda _: False
        with patch('realtime_subtitle.asr.time.monotonic', return_value=10.4):
            self.feed(3)
            self.assertEqual(self.finals, [])
            self.feed(4)
        self.assertEqual(len(self.finals), 1)

    def test_real_word_metadata_commits_stable_prefix_and_keeps_overlap(self):
        a = self.asr
        a._stream_id = 1
        a.configure_translation(True, 2)
        a._buffer = np.zeros(40000, dtype=np.float32)
        words = [Word(0.1, 0.4, ' We'), Word(0.4, 0.7, ' need'), Word(0.7, 1.1, ' help,'),
                 Word(1.2, 1.7, ' because'), Word(1.8, 2.35, ' now')]
        def decode():
            a._decoded_words = words
            return join_words(words), 'en'
        a._transcribe.side_effect = decode
        a._emit_partial(False)
        a._commit_stable()
        self.assertFalse(self.finals)
        a._emit_partial(False)
        a._commit_stable()
        self.assertEqual(self.finals, [])
        self.assertEqual(join_words(a._sentence_words), 'We need help,')
        self.assertAlmostEqual(a._buffer_start, 0.6)
        self.assertAlmostEqual(len(a._buffer) / 16000, 1.9)
        a._emit_partial(False)
        self.assertEqual(self.partials[-1], 'We need help, because now')
        a._finalize()
        self.assertEqual(self.finals[-1], ('We need help, because now', 'en'))

    def test_hard_window_limit_keeps_incomplete_tail_and_decodes_once(self):
        a = self.asr
        a.configure_translation(True)
        a._buffer = np.zeros(96000, dtype=np.float32)
        words = [Word(0.1, 1.0, ' Complete'), Word(1.0, 3.0, ' words'),
                 Word(3.0, 5.2, ' before'), Word(5.2, 6.0, ' incomplete')]
        def decode():
            a._decoded_words = words
            return join_words(words), 'en'
        a._transcribe.side_effect = decode
        a._finalize(forced=True)
        self.assertEqual(a._transcribe.call_count, 1)
        self.assertEqual(self.finals, [])
        self.assertEqual(join_words(a._sentence_words), 'Complete words')
        self.assertGreater(len(a._buffer), 0)
        self.assertEqual(join_words(a._stability.pending), 'before incomplete')

    def test_sentence_updates_across_audio_windows_and_translates_whole_prefix(self):
        a = self.asr
        a.configure_translation(True)
        previews = []
        a.on_preview = lambda *value: previews.append(value)
        a._buffer = np.zeros(96000, dtype=np.float32)
        first = [Word(.1, 1, ' We'), Word(1, 2, ' need'), Word(2, 3, ' to'),
                 Word(3, 4, ' review'), Word(4, 5, ' the'), Word(5, 6, ' proposal')]
        def decode_first():
            a._decoded_words = first
            return join_words(first), 'en'
        a._transcribe.side_effect = decode_first
        a._finalize(forced=True)
        self.assertEqual(self.finals, [])
        self.assertEqual(previews[-1][0], 'We need to review')
        a._buffer = np.zeros(96000, dtype=np.float32)
        last = [Word(4, 5, ' the'), Word(5, 6, ' proposal'),
                Word(6, 7, ' carefully.')]
        def decode_last():
            a._decoded_words = last
            return join_words(last), 'en'
        a._transcribe.side_effect = decode_last
        a._finalize()
        self.assertEqual(self.finals, [('We need to review the proposal carefully.', 'en')])

    def test_decimal_and_abbreviation_are_not_sentence_endings(self):
        for text in (' Dr.', ' Mr.', ' 3.14.', ' U.S.', ' ...'):
            self.assertFalse(Word(0, 1, text).sentence_end, text)
        self.assertTrue(Word(0, 1, ' finished.').sentence_end)

    def test_unstable_period_requires_another_observation(self):
        transcript = StableTranscript()
        transcript.observe([Word(0, 1, ' review')])
        transcript.observe([Word(0, 1, ' review.')])
        self.assertEqual(transcript.stable_count, 0)
        transcript.observe([Word(0, 1, ' review.')])
        self.assertEqual(transcript.stable_count, 1)


class StableAgreementTest(unittest.TestCase):
    def test_revision_does_not_commit_unstable_word(self):
        s = StableTranscript()
        first = [Word(0, 0.3, ' I'), Word(0.3, 0.7, ' want'), Word(0.7, 1.2, ' help'), Word(1.2, 1.5, ' now')]
        revised = [Word(0, 0.3, ' I'), Word(0.3, 0.7, ' wanted'), Word(0.7, 1.2, ' help'), Word(1.2, 1.5, ' now')]
        s.observe(first)
        s.observe(revised)
        self.assertEqual(s.stable_count, 1)
        self.assertFalse(s.choose(2.5, 2))
        s.observe(revised)
        committed = s.commit(s.choose(2.5, 2))
        self.assertEqual(committed, 'I wanted help')
        self.assertEqual(join_words(s.pending), 'now')

    def test_english_function_word_tail_is_not_committed(self):
        s = StableTranscript()
        words = [Word(0, .3, ' Before'), Word(.3, .7, ' making'), Word(.7, 1, ' the'),
                 Word(1, 1.3, ' final')]
        s.observe(words)
        s.observe(words)
        self.assertFalse(s.choose(2.2, 2))

    def test_unpunctuated_phrase_respects_minimum_commit_duration(self):
        s = StableTranscript()
        words = [Word(0, .35, ' We'), Word(.35, .7, ' need'),
                 Word(.7, 1.05, ' to'), Word(1.05, 1.4, ' review'),
                 Word(1.4, 1.75, ' this')]
        s.observe(words)
        s.observe(words)
        self.assertFalse(s.choose(1.5, 1.2, 1.6))
        self.assertTrue(s.choose(2.3, 1.2, 1.6))

    def test_unpunctuated_english_phrase_respects_minimum_word_count(self):
        s = StableTranscript()
        words = [Word(0, .5, ' We'), Word(.5, 1.0, ' need'),
                 Word(1.0, 1.5, ' help')]
        s.observe(words)
        s.observe(words)
        self.assertFalse(s.choose(2.0, 1.2, 1.6, min_words=4))

    def test_timestamp_frontier_deduplicates_overlap_with_punctuation_changes(self):
        s = StableTranscript()
        s.commit([Word(0, 0.4, 'hello'), Word(0.4, 0.9, ' world.')])
        self.assertEqual(s.observe([Word(0, 0.4, 'Hello'), Word(0.4, 0.9, ' world'),
                                    Word(1.0, 1.3, ' Next')]), 'Next')

    def test_chinese_spacing_and_punctuation_preserved(self):
        s = StableTranscript()
        words = [Word(0, 0.3, '我们'), Word(0.3, 0.7, '需要'), Word(0.7, 1.1, '帮助，'),
                 Word(1.2, 1.8, '现在')]
        s.observe(words)
        s.observe(words)
        self.assertEqual(s.commit(s.choose(2, 2)), '我们需要帮助，')


if __name__ == '__main__':
    unittest.main()
