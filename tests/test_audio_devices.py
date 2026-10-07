"""Exercise output switching without changing Windows audio settings."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from realtime_subtitle.audio import WasapiLoopbackCapture
from realtime_subtitle.asr import StreamingWhisper, _to_mono_16k
from realtime_subtitle.config import load_config


class FakeBackend:
    def __init__(self, stages):
        self.stages = stages
        self.stage = 0
        self.opened = []
        self.closed = []

    def default_speaker(self):
        stage = self.stages[self.stage]
        if stage is None:
            self.stage += 1
            return None
        return SimpleNamespace(id=stage[0], name='Output', channels=stage[1])

    def get_microphone(self, id, include_loopback):
        assert include_loopback
        current = self.stages[self.stage]
        assert id == current[0]
        backend = self
        stage_index = self.stage

        class Recorder:
            def __enter__(self):
                backend.opened.append((current[0], self.channels))
                return self

            def __exit__(self, *_):
                backend.closed.append(stage_index)

            def record(self, numframes):
                if current[2] == 'disconnect':
                    backend.stage += 1
                    raise OSError('device unplugged')
                data = np.full((numframes, self.channels), stage_index + 1, dtype=np.float32)
                if backend.stage < len(backend.stages) - 1:
                    backend.stage += 1
                return data

        def recorder(**kwargs):
            assert kwargs['exclusive_mode'] is False
            result = Recorder()
            result.channels = kwargs['channels']
            return result

        return SimpleNamespace(isloopback=True, channels=current[1], recorder=recorder)


class AudioDevicesTest(unittest.TestCase):
    def exercise(self, stages):
        backend = FakeBackend(stages)
        received = []
        statuses = []
        done = threading.Event()

        def receive(block):
            received.append(block)
            if backend.opened and backend.stage == len(stages) - 1:
                done.set()

        capture = WasapiLoopbackCapture(48000, 2, 100, receive, on_status=statuses.append,
                                       device_check_seconds=0, reconnect_seconds=0.01)
        with patch.dict(sys.modules, {'soundcard': backend}):
            try:
                capture.start()
                self.assertTrue(done.wait(3), 'Did not reach final output device')
            finally:
                capture.stop()
        self.assertFalse(capture._thread.is_alive())
        self.assertEqual(len(backend.opened), len(backend.closed))
        return backend, received, statuses

    def test_switch_same_name_different_ids_and_surround(self):
        backend, blocks, _ = self.exercise([('headset', 2, 'ok'), ('hdmi', 8, 'ok')])
        self.assertEqual(backend.opened, [('headset', 2), ('hdmi', 8)])
        self.assertEqual(blocks[0].samples.shape, (4800, 2))
        self.assertEqual(blocks[-1].samples.shape, (4800, 8))
        self.assertNotEqual(blocks[0].stream_id, blocks[-1].stream_id)

    def test_unplug_missing_device_then_mono_reconnect(self):
        backend, blocks, statuses = self.exercise([
            ('usb', 2, 'disconnect'), None, ('bluetooth', 1, 'ok'),
        ])
        self.assertEqual(backend.opened, [('usb', 2), ('bluetooth', 2)])
        self.assertEqual(blocks[-1].samples.shape, (4800, 2))
        self.assertIn('等待音频输出设备', statuses)

    def test_channel_layout_changes_on_same_endpoint(self):
        backend, _, _ = self.exercise([('hdmi', 2, 'ok'), ('hdmi', 6, 'ok')])
        self.assertEqual(backend.opened, [('hdmi', 2), ('hdmi', 6)])

    def test_no_device_at_start_can_recover(self):
        backend, blocks, _ = self.exercise([None, ('speaker', 2, 'ok')])
        self.assertEqual(backend.opened, [('speaker', 2)])
        self.assertTrue(blocks)

    def test_stop_while_no_device(self):
        backend = SimpleNamespace(default_speaker=lambda: None)
        capture = WasapiLoopbackCapture(48000, 2, 100, lambda _: None,
                                       reconnect_seconds=0.01)
        with patch.dict(sys.modules, {'soundcard': backend}):
            capture.start()
            capture.stop()
        self.assertFalse(capture._thread.is_alive())

    def test_center_channel_audio_reaches_asr_and_resets_on_switch(self):
        surround = np.zeros((4800, 6), dtype=np.float32)
        surround[:, 2] = 0.6
        mono = _to_mono_16k(surround, 48000)
        self.assertEqual(mono.shape, (1600,))
        self.assertGreater(float(mono.mean()), 0.09)
        config = load_config(Path(__file__).resolve().parents[1] / 'config.toml')
        worker = StreamingWhisper(config.asr, config.stream, None, lambda _: None, lambda *_: None)
        worker._stream_id = 1
        worker._buffer = np.ones(16000, dtype=np.float32)
        worker._speech_started = True
        worker.vad = SimpleNamespace(chunk_voiced=lambda _: False)
        from realtime_subtitle.audio import AudioBlock
        worker._consume(AudioBlock(np.zeros((4800, 2), dtype=np.float32), 48000, stream_id=2))
        self.assertEqual(worker._buffer.size, 0)
        self.assertFalse(worker._speech_started)

    def test_vad_rejects_wireless_headset_noise(self):
        from realtime_subtitle.vad import WebRtcVad
        vad = WebRtcVad()
        noise = np.full(480, 0.001, dtype=np.float32)
        self.assertFalse(vad.is_voiced(noise))
        self.assertFalse(vad.chunk_voiced(noise))

    def test_vad_accepts_normal_speech_level_audio(self):
        from realtime_subtitle.vad import WebRtcVad
        vad = WebRtcVad()
        rng = np.random.default_rng(7)
        speech = (0.03 * rng.normal(size=480)).clip(-1, 1).astype(np.float32)
        self.assertTrue(vad.is_voiced(speech))


if __name__ == '__main__':
    unittest.main()
