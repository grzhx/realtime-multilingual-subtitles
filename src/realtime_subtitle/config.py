from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import json
import tempfile
import tomllib


SOURCE_LANGUAGES = {'auto', 'zh', 'en'}
TARGET_LANGUAGES = {'none', 'zh', 'en'}


def normalize_source_language(value: str | None) -> str:
    value = str(value or 'auto').lower()
    return value if value in SOURCE_LANGUAGES else 'auto'


def normalize_target_language(value: str | None) -> str:
    value = str(value or 'none').lower()
    return value if value in TARGET_LANGUAGES else 'none'


def mode_from_languages(source_language: str | None, target_language: str | None) -> str:
    source = normalize_source_language(source_language)
    target = normalize_target_language(target_language)
    if target == 'none':
        return 'off'
    return f'{source}_to_{target}'


def languages_from_mode(mode: str | None) -> tuple[str, str]:
    mode = str(mode or 'off').lower()
    if mode == 'zh_to_en':
        return 'zh', 'en'
    if mode == 'en_to_zh':
        return 'en', 'zh'
    if mode in {'auto_to_zh', 'auto_to_en', 'zh_to_zh', 'en_to_en'}:
        source, target = mode.split('_to_', 1)
        return source, target
    return 'auto', 'none'


def load_saved_settings(path: str | Path) -> dict:
    """Read the last applied UI/language settings, ignoring corrupt files."""
    try:
        with Path(path).open('r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_settings(path: str | Path, value: dict) -> None:
    """Atomically save user settings inside the project cache directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.subtitle-settings-', suffix='.tmp',
                                     dir=destination.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass
class PathsConfig:
    models_dir: Path
    cache_dir: Path
    logs_dir: Path


@dataclass
class AudioConfig:
    sample_rate: int
    channels: int
    block_ms: int


@dataclass
class AsrConfig:
    model_size_or_path: str
    device: str
    compute_type: str
    language: str
    beam_size: int
    vad_filter: bool
    download_root: Path
    cpu_threads: int
    num_workers: int
    hallucination_no_speech_prob: float = 0.62
    hallucination_avg_logprob: float = -0.75
    hallucination_compression_ratio: float = 2.4


@dataclass
class StreamConfig:
    update_interval_ms: int
    window_seconds: float
    overlap_seconds: float
    min_speech_ms: int
    max_segment_seconds: float
    silence_end_ms: int
    commit_interval_ms: int = 650
    min_commit_seconds: float = 1.6
    preview_translation_seconds: float = 0.9
    sentence_end_ms: int = 700


@dataclass
class TranslationConfig:
    enabled: bool
    model_id: str
    mode: str
    bilingual: bool
    device: str
    dtype: str
    max_new_tokens: int
    context_segments: int
    segment_seconds: float = 2.0
    backend: str = 'transformers'
    server_executable: str = 'runtime/llama.cpp/llama-server.exe'
    server_context: int = 2048
    server_timeout: float = 30.0
    source_language: str = 'auto'
    target_language: str = 'none'


@dataclass
class UiConfig:
    font_family: str
    source_font_size: int
    translation_font_size: int
    width: int
    height: int
    bottom_margin: int
    opacity: float
    background_color: str
    source_color: str
    translation_color: str
    text_opacity: float
    background_opacity: float
    max_height: int
    history_enabled: bool = False
    history_display_seconds: float = 5.0
    history_count: int = 2


@dataclass
class AppConfig:
    root: Path
    paths: PathsConfig
    audio: AudioConfig
    asr: AsrConfig
    stream: StreamConfig
    translation: TranslationConfig
    ui: UiConfig


def _path(root: Path, value: str) -> Path:
    value_path = Path(value)
    return value_path if value_path.is_absolute() else root / value_path


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).resolve()
    root = path.parent
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    paths = raw["paths"]
    audio = raw["audio"]
    asr = raw["asr"]
    stream = raw["stream"]
    translation = raw["translation"]
    ui = raw["ui"]
    ui = {
        "background_color": "#000000",
        "source_color": "#EEEEEE",
        "translation_color": "#FFD75A",
        "text_opacity": 1.0,
        "background_opacity": ui.get("opacity", 0.92),
        "max_height": 360,
        "history_enabled": False,
        "history_display_seconds": 5.0,
        "history_count": 2,
        **ui,
    }

    asr_model = asr["model_size_or_path"]
    asr_model_path = root / asr_model
    if asr_model_path.exists():
        asr_model = str(asr_model_path)
    translation_model = translation["model_id"]
    translation_model_path = root / translation_model
    if translation_model_path.exists():
        translation_model = str(translation_model_path)

    legacy_source, legacy_target = languages_from_mode(translation.get('mode', 'zh_to_en'))
    source_language = normalize_source_language(translation.get('source_language', legacy_source))
    target_language = normalize_target_language(translation.get('target_language', legacy_target))
    result = AppConfig(
        root=root,
        paths=PathsConfig(
            models_dir=_path(root, paths["models_dir"]),
            cache_dir=_path(root, paths["cache_dir"]),
            logs_dir=_path(root, paths["logs_dir"]),
        ),
        audio=AudioConfig(**audio),
        asr=AsrConfig(
            model_size_or_path=asr_model,
            device=asr["device"],
            compute_type=asr["compute_type"],
            language=asr["language"],
            beam_size=asr["beam_size"],
            vad_filter=asr["vad_filter"],
            download_root=_path(root, asr["download_root"]),
            cpu_threads=asr.get("cpu_threads", 2),
            num_workers=asr.get("num_workers", 1),
            hallucination_no_speech_prob=asr.get('hallucination_no_speech_prob', 0.62),
            hallucination_avg_logprob=asr.get('hallucination_avg_logprob', -0.75),
            hallucination_compression_ratio=asr.get('hallucination_compression_ratio', 2.4),
        ),
        stream=StreamConfig(**stream),
        translation=TranslationConfig(
            enabled=translation["enabled"],
            model_id=translation_model,
            mode=mode_from_languages(source_language, target_language),
            bilingual=translation.get("bilingual", True),
            device=translation["device"],
            dtype=translation["dtype"],
            max_new_tokens=translation["max_new_tokens"],
            context_segments=translation["context_segments"],
            segment_seconds=translation.get('segment_seconds', 2.0),
            backend=translation.get('backend', 'transformers'),
            server_executable=str(_path(root, translation.get('server_executable', 'runtime/llama.cpp/llama-server.exe'))),
            server_context=translation.get('server_context', 2048),
            server_timeout=translation.get('server_timeout', 30.0),
            source_language=source_language,
            target_language=target_language,
        ),
        ui=UiConfig(**ui),
    )

    for directory in (result.paths.models_dir, result.paths.cache_dir, result.paths.logs_dir, result.asr.download_root):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(result.paths.cache_dir / "huggingface"))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(result.paths.cache_dir / "huggingface" / "hub"))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(result.paths.cache_dir / "huggingface" / "transformers"))
    os.environ.setdefault("TORCH_HOME", str(result.paths.cache_dir / "torch"))
    cuda_cache = result.paths.cache_dir / 'cuda'
    cuda_cache.mkdir(parents=True, exist_ok=True)
    os.environ['CUDA_CACHE_PATH'] = str(cuda_cache)
    return result




