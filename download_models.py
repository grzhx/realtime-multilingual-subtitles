"""Download the default ASR and GGUF weights at pinned revisions."""
from pathlib import Path
import hashlib
import os

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'cache/huggingface'
os.environ['HF_HOME'] = str(CACHE)
os.environ['HF_HUB_CACHE'] = str(CACHE / 'hub')
os.environ['HF_XET_CACHE'] = str(CACHE / 'xet')
os.environ['TEMP'] = str(ROOT / 'cache/tmp')
os.environ['TMP'] = os.environ['TEMP']
Path(os.environ['TEMP']).mkdir(parents=True, exist_ok=True)


def verify(path, expected):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError(f'SHA-256 mismatch: {path.name}')
    print(f'SHA-256 OK: {path.name}', flush=True)


def main():
    from huggingface_hub import hf_hub_download, snapshot_download
    asr = ROOT / 'models/faster-whisper-large-v3-turbo'
    print('Preparing faster-whisper large-v3-turbo...', flush=True)
    snapshot_download(repo_id='mobiuslabsgmbh/faster-whisper-large-v3-turbo',
                      revision='0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf',
                      local_dir=str(asr), cache_dir=str(CACHE / 'hub'), max_workers=4)
    verify(asr / 'model.bin', 'e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da')
    print('Preparing Qwen3-4B-Instruct-2507 Q4_K_M...', flush=True)
    path = hf_hub_download(repo_id='unsloth/Qwen3-4B-Instruct-2507-GGUF',
                           revision='a06e946bb6b655725eafa393f4a9745d460374c9',
                           filename='Qwen3-4B-Instruct-2507-Q4_K_M.gguf',
                           local_dir=str(ROOT / 'models'), cache_dir=str(CACHE / 'hub'))
    verify(Path(path), '3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597')


if __name__ == '__main__':
    main()
