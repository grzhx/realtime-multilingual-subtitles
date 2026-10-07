from __future__ import annotations

import numpy as np


class WebRtcVad:
    """Small frame VAD wrapper. It falls back to energy gating if WebRTC is unavailable."""

    def __init__(self, sample_rate: int = 16000, aggressiveness: int = 3, frame_ms: int = 30,
                 min_rms: float = 0.003):
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        # WebRTC VAD can classify wireless-headset hiss and codec comfort noise
        # as speech. The RMS gate runs first and prevents those frames from ever
        # triggering Whisper. 0.003 is well above the measured idle HyperX
        # loopback noise (~0.0005 RMS), while remaining below normal speech.
        self.min_rms = min_rms
        self._vad = None
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(aggressiveness)
        except ImportError:
            pass

    def is_voiced(self, audio: np.ndarray) -> bool:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size < self.frame_samples:
            padded = np.zeros(self.frame_samples, dtype=np.float32)
            padded[:audio.size] = audio
            audio = padded
        else:
            audio = audio[:self.frame_samples]
        audio = np.clip(audio, -1.0, 1.0)
        rms = float(np.sqrt(np.mean(audio * audio)))
        if rms < self.min_rms:
            return False
        if self._vad is None:
            return True
        pcm = (audio * 32767).astype(np.int16).tobytes()
        return bool(self._vad.is_speech(pcm, self.sample_rate))

    def chunk_voiced(self, audio: np.ndarray) -> bool:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return False
        values = []
        for start in range(0, len(audio), self.frame_samples):
            values.append(self.is_voiced(audio[start : start + self.frame_samples]))
        # Require half of the frames to pass both energy and WebRTC checks.
        return sum(values) >= max(1, (len(values) + 1) // 2)
