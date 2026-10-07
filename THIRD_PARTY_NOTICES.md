# Third-Party Components

The project source is MIT-licensed. This does not relicense model weights,
downloaded binaries, Python, CUDA libraries, or Python packages.

- Python: PSF License and bundled third-party notices. The bootstrap package
  retains Python's LICENSE.txt and Tcl/Tk notices.
- Whisper, faster-whisper, CTranslate2: their upstream MIT licenses.
- Qwen3 and GGUF weights: the pinned model repositories' Apache-2.0/license terms.
- llama.cpp: MIT. Initialization downloads official binaries and their LICENSE.
  The upstream OpenMP notice is retained.
- NVIDIA CUDA, cuDNN, cuBLAS: NVIDIA's applicable software and redistribution
  terms; these are not covered by the project's MIT license.
- NumPy, SciPy, SoundCard, WebRTC VAD wheels, ONNX Runtime, PyTorch, Hugging Face
  Hub and other dependencies: their own licenses and notices.
- Inno Setup: its upstream license; used only to build installers.

Model weights and inference dependencies are downloaded during initialization,
not committed to the source repository. Retain their upstream notices when
redistributing a fully offline package containing these components.
