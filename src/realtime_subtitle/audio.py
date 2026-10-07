from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np


logger = logging.getLogger(__name__)


@dataclass
class AudioBlock:
    samples: np.ndarray
    sample_rate: int
    stream_id: int = 0
    captured_at: float = 0.0


class WasapiLoopbackCapture:
    """Capture the current Windows default speaker through WASAPI loopback."""

    def __init__(self, sample_rate: int, channels: int, block_ms: int, on_block: Callable[[AudioBlock], None],
                 on_error: Callable[[str], None] | None = None,
                 on_status: Callable[[str], None] | None = None,
                 device_check_seconds: float = 0.75, reconnect_seconds: float = 0.5):
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_ms = block_ms
        self.on_block = on_block
        self.on_error = on_error
        self.on_status = on_status
        self.device_check_seconds = device_check_seconds
        self.reconnect_seconds = reconnect_seconds
        self._stream_id = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="wasapi-loopback", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise RuntimeError("系统音频采集启动超时，请检查默认播放设备")
        if self._error is not None:
            raise RuntimeError(f"系统音频采集启动失败：{self._error}") from self._error

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        try:
            import soundcard as sc
        except Exception as exc:
            self._error = exc
            self._ready.set()
            logger.exception("Could not load the audio backend")
            if self.on_error:
                self.on_error(str(exc))
            return

        frames = max(1, int(self.sample_rate * self.block_ms / 1000))
        last_error = None
        while not self._stop.is_set():
            try:
                speaker = sc.default_speaker()
                if speaker is None:
                    raise RuntimeError("未找到默认音频输出设备")
                loopback = sc.get_microphone(id=speaker.id, include_loopback=True)
                if not loopback.isloopback:
                    raise RuntimeError("默认输出设备未提供 WASAPI loopback 录音接口")
                # Use the device's full channel layout (including surround center).
                # SoundCard on Windows has known issues with one-channel capture;
                # shared-mode WASAPI can remix mono output to stereo instead.
                native_channels = int(loopback.channels)
                channels = max(2, native_channels)
                signature = (speaker.id, native_channels)
                with loopback.recorder(samplerate=self.sample_rate, channels=channels,
                                       blocksize=frames, exclusive_mode=False) as recorder:
                    self._stream_id += 1
                    last_error = None
                    logger.info("Capturing WASAPI loopback from %s at %s Hz (%s channels)",
                                speaker.name, self.sample_rate, channels)
                    if self.on_status:
                        self.on_status(f"音频已连接：{speaker.name}")
                    self._ready.set()
                    next_check = time.monotonic() + self.device_check_seconds
                    while not self._stop.is_set():
                        if time.monotonic() >= next_check:
                            current = sc.default_speaker()
                            if current is None or (current.id, int(current.channels)) != signature:
                                logger.info("Default audio output changed; reopening loopback")
                                break
                            next_check = time.monotonic() + self.device_check_seconds
                        data = recorder.record(numframes=frames)
                        if data is None or len(data) == 0:
                            continue
                        samples = np.asarray(data, dtype=np.float32)
                        if samples.ndim == 1:
                            samples = samples[:, None]
                        self.on_block(AudioBlock(samples=samples, sample_rate=self.sample_rate,
                                                 stream_id=self._stream_id,
                                                 captured_at=time.monotonic()))
            except Exception as exc:
                if self._stop.is_set():
                    break
                message = f"音频暂不可用，正在重新连接：{exc}"
                if message != last_error:
                    logger.warning("%s", message, exc_info=True)
                    if self.on_error:
                        self.on_error(message)
                    if self.on_status:
                        self.on_status("等待音频输出设备")
                    last_error = message
                # An unplugged device is recoverable. Keep the UI and models alive.
                self._ready.set()
                if self._stop.wait(self.reconnect_seconds):
                    break


class AudioQueue:
    def __init__(self, max_blocks: int = 30):
        self.queue: queue.Queue[AudioBlock] = queue.Queue(maxsize=max_blocks)
        self.dropped_blocks = 0

    def put(self, block: AudioBlock) -> None:
        try:
            self.queue.put_nowait(block)
        except queue.Full:
            # Drop the oldest block to keep latency bounded instead of building an unbounded backlog.
            try:
                self.queue.get_nowait()
                self.dropped_blocks += 1
                self.queue.put_nowait(block)
            except queue.Empty:
                pass

    def get(self, timeout: float = 0.2) -> AudioBlock | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
