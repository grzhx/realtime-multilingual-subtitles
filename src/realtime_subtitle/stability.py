"""Local agreement over Whisper word timestamps, with a committed time frontier."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str

    @property
    def key(self):
        return re.sub(r'\W+', '', self.text, flags=re.UNICODE).casefold() or self.text.strip()

    @property
    def sentence_end(self):
        value = self.text.strip().rstrip('\"\'\u201d\u2019\u300d\u300f)')
        if re.search(r'[。！？!?]$', value):
            return True
        if not value.endswith('.'):
            return False
        # Common titles/initials and decimal numbers are not sentence ends.
        if re.search(r'(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)|\b[A-Za-z]|\d+\.\d+)\.$', value, re.I):
            return False
        return not value.endswith('...')


def join_words(words):
    # Whisper's word strings already contain language-appropriate spacing.
    return ''.join(word.text for word in words).strip()


class StableTranscript:
    def __init__(self):
        self.committed_end = 0.0
        self.previous = []
        self.pending = []
        self.stable_count = 0

    def observe(self, words):
        self.pending = [w for w in words if (w.start + w.end) / 2 > self.committed_end + 0.02]
        self.stable_count = 0
        for current, previous in zip(self.pending, self.previous):
            if current.key != previous.key:
                break
            # A newly hallucinated period must survive another decode before
            # it can irreversibly close a sentence.
            if current.sentence_end != previous.sentence_end:
                break
            self.stable_count += 1
        self.previous = list(self.pending)
        return join_words(self.pending)

    def choose(self, audio_end, target_seconds, min_commit_seconds=None, min_words=3):
        stable = self.pending[:self.stable_count]
        # The very latest word may be incomplete even when two passes agree.
        safe = [w for w in stable if w.end <= audio_end - 0.35]
        if not safe:
            return []
        boundaries = [i + 1 for i, w in enumerate(safe)
                      if re.search(r'[.!?。！？,，;；:：]$', w.text.strip())]
        if boundaries:
            return safe[:boundaries[-1]]
        target_seconds = max(target_seconds, min_commit_seconds or 0.0)
        if audio_end - self.committed_end >= target_seconds:
            # Retain the last stable token for the next window unless there is
            # a natural boundary; this reduces clipped phrases at the edge.
            candidate = safe[:-1] if len(safe) > 1 else []
            if any(re.search(r'[A-Za-z]', w.text) for w in candidate):
                trailing = {'a', 'an', 'the', 'of', 'to', 'for', 'with', 'and', 'or',
                            'but', 'in', 'on', 'at', 'by', 'from', 'because', 'before', 'after'}
                while candidate and candidate[-1].key in trailing:
                    candidate.pop()
                if len(candidate) < min_words:
                    return []
            return candidate
        return []

    def commit(self, words):
        if not words:
            return ''
        text = join_words(words)
        self.committed_end = max(self.committed_end, words[-1].end)
        self.pending = self.pending[len(words):]
        self.previous = list(self.pending)
        self.stable_count = 0
        return text
