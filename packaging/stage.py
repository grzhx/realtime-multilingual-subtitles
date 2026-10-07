"""Build release trees from explicit allowlists, never from the local workspace."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.2.0'
FILES = ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml',
         'requirements.txt', 'config.toml', 'config.no_translation.toml',
         'config.fallback_0_6b.toml', 'run.ps1', 'setup.ps1', 'initialize.ps1',
         'setup_gguf.ps1', 'download_models.py', 'launch.py', '.gitignore']


def copy_tree(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', 'site-packages', 'test', 'tests', 'idlelib',
        'turtledemo'), dirs_exist_ok=True)


def archive(source, destination):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive.write(path, Path(source.name) / path.relative_to(source))


def stage(python_home):
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    source = ROOT / 'build/source-stage'
    portable = ROOT / 'build/installer-stage'
    # Build stages contain only generated allowlisted copies. Never erase the
    # user's live models, caches, environment, or source checkout.
    for path in (source, portable):
        if path.exists():
            if path.resolve().parent != (ROOT / 'build').resolve():
                raise RuntimeError('Unexpected staging path')
            shutil.rmtree(path)
        path.mkdir(parents=True)
        for filename in FILES:
            shutil.copy2(ROOT / filename, path / filename)
        copy_tree(ROOT / 'src', path / 'src')
    copy_tree(ROOT / 'tests', source / 'tests')
    copy_tree(ROOT / 'packaging', source / 'packaging')
    for folder in ('models', 'cache', 'logs'):
        (source / folder).mkdir(exist_ok=True)
        (source / folder / '.gitkeep').touch()
    runtime = portable / 'runtime/python'
    runtime.mkdir(parents=True)
    required = ['python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll',
                'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt']
    for filename in required:
        path = python_home / filename
        if not path.is_file():
            raise FileNotFoundError(f'Python 3.14 x64 distribution missing: {filename}')
        shutil.copy2(path, runtime / filename)
    for folder in ('Lib', 'DLLs', 'tcl'):
        copy_tree(python_home / folder, runtime / folder)
    # Preserve third-party notices within the Tcl/Tk distribution.
    archive(source, output / f'realtime-multilingual-subtitles-{VERSION}-source.zip')
    archive(portable, output / f'realtime-multilingual-subtitles-{VERSION}-windows-x64-portable.zip')
    manifest = []
    for path in sorted(output.glob('*.zip')):
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        manifest.append(dict(file=path.name, bytes=path.stat().st_size, sha256=digest))
    (output / 'artifacts.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python-home', required=True, type=Path)
    stage(parser.parse_args().python_home.resolve())
