"""Project-local llama.cpp server, with schema-constrained translation output."""
from __future__ import annotations

import json
import logging
import socket
import secrets
import subprocess
import time
import threading
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
SCHEMA = {
    'type': 'object',
    'properties': {'translation': {'type': 'string'}},
    'required': ['translation'],
    'additionalProperties': False,
}


class LlamaServer:
    def __init__(self, config, project_root):
        self.config = config
        self.root = Path(project_root)
        self.process = None
        self.log_file = None
        self.url = ''
        self.api_key = secrets.token_urlsafe(24)
        self._stop_lock = threading.Lock()

    def start(self, stop_event):
        executable = Path(self.config.server_executable)
        if not executable.is_file():
            raise FileNotFoundError(f'llama.cpp runtime missing: {executable}')
        if not Path(self.config.model_id).is_file():
            raise FileNotFoundError(f'GGUF model missing: {self.config.model_id}')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        self.log_file = (logs / 'llama-server.log').open('w', encoding='utf-8')
        command = [str(executable), '-m', str(self.config.model_id), '--host', '127.0.0.1',
                   '--port', str(port), '-ngl', '99', '-c', str(self.config.server_context),
                   '-t', '2', '-tb', '2', '-np', '1', '--jinja',
                   '--no-webui', '--reasoning', 'off', '-fa', 'on', '--api-key', self.api_key]
        logger.info('Starting project-local Q4 GGUF server on port %s', port)
        self.process = subprocess.Popen(
            command, cwd=executable.parent, stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS,
        )
        deadline = time.monotonic() + 90
        while not stop_event.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'llama-server exited with {self.process.returncode}; see logs/llama-server.log')
            try:
                if self.request('/health', timeout=1).get('status') == 'ok':
                    logger.info('GGUF CUDA translation server ready')
                    return
            except (URLError, TimeoutError, OSError):
                pass
            stop_event.wait(0.2)
        self.stop()
        if not stop_event.is_set():
            raise TimeoutError('llama-server did not become ready in 90 seconds')

    def request(self, path, payload=None, timeout=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = Request(self.url + path, data=body, headers={
            'Content-Type': 'application/json', 'Authorization': f'Bearer {self.api_key}'})
        with urlopen(request, timeout=timeout or self.config.server_timeout) as response:
            return json.load(response)

    def translate(self, text, mode, max_tokens, strict=False):
        target = '简体中文' if mode.rsplit('_to_', 1)[-1] == 'zh' else 'English'
        system = (
            f'Translate the input into {target}. Preserve meaning, tone, names and numbers. '
            'Return a JSON object with only the translation field. '
            'The translation field must contain only the translated subtitle: no labels, '
            'no headings, no explanations, no XML/HTML tags and no thinking. '
            'If input is already in the target language, copy it unchanged. '
            'Do not add information or complete unfinished sentences.'
        )
        if strict:
            system += f' Every ordinary word must be translated into {target}. Never label the output.'
        result = self.request('/v1/chat/completions', {
            'model': 'local-qwen', 'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': text},
            ], 'temperature': 0, 'max_tokens': max_tokens,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'subtitle_translation', 'strict': True, 'schema': SCHEMA}},
            'chat_template_kwargs': {'enable_thinking': False},
        })
        choice = result['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ValueError('Translation exceeded token budget')
        parsed = json.loads(choice['message']['content'])
        if set(parsed) != {'translation'} or not isinstance(parsed['translation'], str):
            raise ValueError('Invalid structured translation')
        return parsed['translation'].strip()

    def stop(self):
        with self._stop_lock:
            process = self.process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if self.log_file:
                self.log_file.close()
                self.log_file = None
