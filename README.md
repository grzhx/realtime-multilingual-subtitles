# Real-time Multilingual Subtitles

[English](README.md) | [简体中文](README.zh-CN.md)

A Windows desktop tool for real-time multilingual subtitles. It captures the default speaker, headset, or HDMI output through WASAPI Loopback, recognizes speech locally with Whisper, and translates it locally with Qwen into Chinese or English.

> “Multilingual” describes source-language recognition. Target languages are currently Chinese, English, or no translation. This is an experimental release; accuracy and latency depend on audio, drivers, and hardware.

## Features

- WASAPI system-audio capture with default-device switching and reconnect.
- Adaptive, Chinese, or English source language; Chinese, English, or no-translation target.
- Fast partial subtitles, provisional translation, and complete-sentence confirmation. Audio-window rollover does not create half-sentence history entries.
- Bilingual or translation-only display with live settings.
- Borderless topmost overlay, drag movement, edge resize, and click-through lock.
- Independent font, color, text-opacity, and background-opacity controls, including a fully transparent background.
- Timed sentence history with a protected number of newest entries.
- Stable current-line and control-button anchoring while the window resizes.
- Settings remembered in the project-local cache.

## Requirements

- Windows 10/11 x64 and a recent NVIDIA driver.
- An NVIDIA GPU is recommended. The development machine is an RTX 5070 Ti 16GB.
- About 20GB of free disk space for dependencies, caches, and model weights.
- Internet access for first initialization: PyPI, PyTorch, Hugging Face, and GitHub. The initialized app runs offline.
- Source setup requires Python 3.11+ with Tkinter; Python 3.14 x64 is recommended.

## Windows Installer

Download the Windows x64 installer from [Releases](https://github.com/grzhx/realtime-multilingual-subtitles/releases). It contains the local Python runtime and application source, but not the large model weights or inference dependencies.

The first launch opens initialization, installs dependencies, and downloads the default ASR model, Q4 translation model, and CUDA llama.cpp runtime. Later launches open the subtitle window directly.

Initialization can be retried by launching the shortcut again. Bootstrap logs are in `logs/bootstrap.log`; runtime logs are in `logs/runtime.log`. The installer does not require administrator privileges. Uninstall removes program files and keeps models, logs, and user settings.

## Source Setup

```powershell
git clone https://github.com/grzhx/realtime-multilingual-subtitles.git
cd realtime-multilingual-subtitles
powershell -NoProfile -ExecutionPolicy Bypass -File .\initialize.ps1
.\run.ps1
```

`initialize.ps1` creates the virtual environment, installs dependencies, downloads the default weights, and installs the CUDA runtime. Downloads stay inside the project directory.

```powershell
.\run.ps1 -Config config.no_translation.toml
```

This profile runs recognition without translation. Enabling translation from the settings panel loads the local model.

`config.fallback_0_6b.toml` is an optional Transformers fallback and requires a separate Qwen3-0.6B download. The default initializer downloads only large-v3-turbo and the 4B Q4 GGUF.

## Models and Runtime

| Purpose | Default component | Source |
|---|---|---|
| ASR | faster-whisper large-v3-turbo | [Hugging Face](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) |
| Translation | Qwen3-4B-Instruct-2507 Q4_K_M | [Hugging Face](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF) |
| Inference | llama.cpp b11461 CUDA 12.4 | [GitHub](https://github.com/ggml-org/llama.cpp) |

The downloader pins model revisions and verifies the main weights and runtime archives with SHA-256. The ASR weights are about 1.5GiB and the GGUF is about 2.3GiB. The translation server listens only on a random localhost port and exits with the app.

## Usage and Settings

When unlocked, drag the body to move the overlay or drag either side edge to resize it. Moving the pointer into the overlay shows settings and close controls; the lock button follows the current subtitle line. When locked, only the unlock button remains clickable and the rest of the overlay is click-through.

Language and bilingual settings apply immediately. Apply font, width, color, opacity, and history changes from the settings panel. Settings are saved to `cache/subtitle_settings.json`.

History count protects the newest entries; it is not a hard display cap. For example, with count 2 and display time 5 seconds, the newest two history entries remain protected, while older entries are removed after their original five-second timer expires. The open current sentence is not history.

## Advanced Configuration

The usual settings belong in the UI. Advanced values are in `config.toml`:

| Parameter | Purpose |
|---|---|
| `asr.compute_type` | `float16` by default; test `int8_float16` if VRAM is tight |
| `stream.update_interval_ms` | Partial subtitle update target |
| `stream.commit_interval_ms` | Stable word-timestamp check interval |
| `stream.max_segment_seconds` | Audio rollover window, not sentence length |
| `stream.sentence_end_ms` | Pause duration used to complete a sentence |
| `stream.preview_translation_seconds` | Stable audio required before provisional translation |
| `translation.max_new_tokens` | Base output budget; long captions receive more room |
| `ui.history_count` | Protected newest history entries |
| `ui.history_display_seconds` | History lifetime starting when an entry is archived |

## Limitations

- Music, noise, multiple speakers, and accents can cause recognition errors. Low-confidence filtering reduces music hallucinations but cannot remove every high-confidence error.
- Provisional translation changes as the open sentence grows; punctuation and pause detection can be wrong.
- Protected audio, exclusive mode, and some drivers may not support loopback.
- The overlay shrinks text when screen space is limited and may move the anchor to avoid clipping.
- NVIDIA CUDA is the supported default; cross-platform and CPU low-latency use are not validated.

## Development and Packaging

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\packaging\build.ps1 -PythonHome C:\Python314 -IsccPath path\to\ISCC.exe
```

Tests stay in the repository. Windows UI tests require an interactive desktop; simulated tests do not require a GPU.

The build creates a source ZIP, portable initialization package, and Inno Setup installer in `dist/`. The portable package includes Python but not dependencies or models, so it is not an offline bundle.

## License

The source is [MIT](LICENSE). Models, Python, llama.cpp, CUDA, and other dependencies retain their own licenses. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
