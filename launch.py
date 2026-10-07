"""Windows shortcut entry point with a project-local runtime and bootstrap."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def run():
    os.chdir(ROOT)
    cache = ROOT / 'cache'
    locations = {
        'TEMP': cache / 'tmp', 'TMP': cache / 'tmp',
        'PIP_CACHE_DIR': cache / 'pip', 'HF_HOME': cache / 'huggingface',
        'HF_HUB_CACHE': cache / 'huggingface/hub',
        'HF_XET_CACHE': cache / 'huggingface/xet', 'TORCH_HOME': cache / 'torch',
        'CUDA_CACHE_PATH': cache / 'cuda', 'XDG_CACHE_HOME': cache / 'xdg',
    }
    for name, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    python = ROOT / '.venv/Scripts/pythonw.exe'
    required = [python, ROOT / 'cache/initialized.json',
                ROOT / 'models/faster-whisper-large-v3-turbo/model.bin',
                ROOT / 'models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf',
                ROOT / 'runtime/llama.cpp/llama-server.exe']
    if not all(path.is_file() for path in required):
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-File', str(ROOT / 'initialize.ps1')]
        result = subprocess.run(command, cwd=ROOT,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
        if result.returncode:
            raise RuntimeError('Initialization failed. See logs/bootstrap.log and run again to retry.')
    os.environ['PYTHONPATH'] = str(ROOT / 'src')
    log_dir = ROOT / 'logs'
    log_dir.mkdir(exist_ok=True)
    with (log_dir / 'launcher.log').open('a', encoding='utf-8') as output:
        subprocess.Popen([str(python), '-m', 'realtime_subtitle.app',
                          '--config', str(ROOT / 'config.toml')], cwd=ROOT,
                         stdout=output, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Real-time Multilingual Subtitles', 0x10)
        sys.exit(1)
