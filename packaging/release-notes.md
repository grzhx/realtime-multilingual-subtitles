# v0.2.0

Initial public experimental Windows release of Real-time Multilingual Subtitles.

- Local WASAPI system-audio capture, multilingual Whisper recognition, Chinese
  and English Qwen translation.
- Continuously growing sentences, provisional translation and final captions.
- Borderless overlay, independent opacity, resize, click-through lock, remembered
  settings, and timed history with protected recent captions.

The installer and portable package contain Python and application source. First
launch downloads inference dependencies, model weights and llama.cpp. They are
not offline bundles. NVIDIA CUDA is the supported default. Windows binaries are
unsigned; recognition may hallucinate on music or noise.

Source: MIT. Third-party model and runtime licenses remain applicable.
