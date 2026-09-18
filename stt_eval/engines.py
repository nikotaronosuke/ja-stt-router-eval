from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import time
from pathlib import Path, PureWindowsPath

from .audio import StreamingResampler
from .safety import AudioBudget, DiagnosticError, local_worker_environment, safe_code, safe_error

MODEL_OPENAI = 'gpt-live-transcribe'
MODEL_PARAKEET = 'nvidia/parakeet-tdt_ctc-0.6b-ja'
VOCABULARY = ['SharePoint', 'WordPress', '生成AI']


class OpenAIEngine:
    name, model = 'openai', MODEL_OPENAI

    def __init__(self, budget: AudioBudget, *, allow_network=False, hints=False,
                 delay='medium', rotation_s=3300., emit=None, keywords=None, prompt=None):
        if not allow_network:
            raise PermissionError('explicit fixture transmission approval required')
        if delay not in ['minimal', 'low', 'medium', 'high', 'xhigh']:
            raise ValueError('invalid delay')
        if not math.isfinite(rotation_s) or not 1 <= rotation_s <= 3500:
            raise ValueError('rotation must be 1..3500 seconds')
        self.budget, self.hints, self.delay, self.rotation_s = budget, hints, delay, rotation_s
        if keywords is not None:
            invalid_term = any(not isinstance(term, str) or not term.strip() or len(term) > 200
                               or any(char in term for char in '<>\r\n') for term in keywords)
            if not isinstance(keywords, list) or len(keywords) > 128 or invalid_term:
                raise ValueError('invalid vocabulary hints')
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > 2000):
            raise ValueError('invalid context prompt')
        self.keywords = list(keywords) if keywords is not None else list(VOCABULARY)
        self.prompt = prompt if prompt is not None else '日本語の音声認識テストです。'
        self.emit = emit or (lambda event: None)
        self.ws, self.reader = None, None
        self.connected_ns, self.connections, self.rotations = 0, 0, 0
        self.total_audio_s, self.total_connection_s = 0., 0.
        self.active = None
        self.completed_items = set()
        self.metadata = {'streaming_mode': 'native', 'delay': delay, 'hints': hints,
                         'languages': ['ja'], 'input_rate': 24000, 'turn_detection': None,
                         'rotation_s': rotation_s}
        self.metadata['hint_term_count'] = len(self.keywords) if hints else 0

    def session_update(self):
        transcription = {'model': self.model, 'languages': ['ja'], 'delay': self.delay}
        if self.hints:
            transcription.update(prompt=self.prompt, keywords=self.keywords)
        return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {
            'input': {'format': {'type': 'audio/pcm', 'rate': 24000},
                      'transcription': transcription, 'turn_detection': None,
                      'noise_reduction': None}}}}

    async def open(self):
        from websockets.asyncio.client import connect
        key = os.environ.get('OPENAI_API_KEY')
        if not key:
            raise DiagnosticError('OPENAI_API_KEY_missing')
        self.ws = await connect('wss://api.openai.com/v1/realtime?intent=transcription',
                                additional_headers={'Authorization': 'Bearer ' + key},
                                open_timeout=20, close_timeout=5, max_size=2**22)
        self.connected_ns = time.perf_counter_ns()
        self.connections += 1
        await self.ws.send(json.dumps(self.session_update(), ensure_ascii=False))
        try:
            async with asyncio.timeout(20):
                while True:
                    event = json.loads(await self.ws.recv())
                    if event['type'] == 'error':
                        raise DiagnosticError('session_configuration_' + safe_code(event.get('error', {}).get('code')))
                    if event['type'] in ['session.updated', 'transcription_session.updated']:
                        break
        except BaseException:
            await self.close()
            raise
        self.reader = asyncio.create_task(self._read())
        self.emit({'kind': 'status', 'engine': self.name, 'state': 'connected'})

    async def _read(self):
        try:
            async for message in self.ws:
                event = json.loads(message)
                kind = event.get('type')
                if kind == 'error':
                    raise DiagnosticError(safe_code(event.get('error', {}).get('code')))
                if kind == 'conversation.item.input_audio_transcription.failed':
                    raise DiagnosticError(safe_code(event.get('error', {}).get('code')))
                if not kind.startswith('conversation.item.input_audio_transcription.'):
                    continue
                item = event.get('item_id')
                if not self.active or item in self.completed_items:
                    continue
                now = time.perf_counter_ns()
                if self.active['item'] is None:
                    self.active['item'] = item
                if item != self.active['item']:
                    raise DiagnosticError('unexpected_transcription_item')
                if kind.endswith('.delta'):
                    self.active['text'] += event.get('delta', '')
                    self.active['callback'](self.active['text'], now, False)
                elif kind.endswith('.completed'):
                    self.active['callback'](event.get('transcript', ''), now, True)
                    self.completed_items.add(item)
                    if len(self.completed_items) > 256:
                        self.completed_items = {item}
                    if not self.active['done'].done():
                        self.active['done'].set_result(None)
            raise DiagnosticError('connection_closed')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.active and not self.active['done'].done():
                self.active['done'].set_exception(DiagnosticError('realtime_' + safe_error(error)))
            self.emit({'kind': 'status', 'engine': self.name, 'state': 'disconnected',
                       'error': safe_error(error)})

    async def begin(self, callback):
        if self.ws is None or (self.reader and self.reader.done()):
            await self.close()
            await self.open()
        elif (time.perf_counter_ns()-self.connected_ns) / 1e9 >= self.rotation_s:
            await self.close()
            self.rotations += 1
            await self.open()
        self.resampler = StreamingResampler(16000, 24000)
        self.active = {'callback': callback, 'text': '', 'item': None,
                       'done': asyncio.get_running_loop().create_future()}

    async def feed(self, chunk):
        if self.active['done'].done():
            await self.active['done']
            raise DiagnosticError('premature_transcription_completion')
        pcm = self.resampler.pcm16(chunk.pcm16)
        await self.ws.send(json.dumps({'type': 'input_audio_buffer.append',
                                       'audio': base64.b64encode(pcm).decode('ascii')}))
        self.total_audio_s += len(pcm) / 48000

    async def finish(self):
        await self.ws.send(json.dumps({'type': 'input_audio_buffer.commit'}))
        await asyncio.wait_for(asyncio.shield(self.active['done']), 45)
        self.active = None

    async def abort(self):
        if self.active and not self.active['done'].done():
            self.active['done'].cancel()
        elif self.active and not self.active['done'].cancelled():
            self.active['done'].exception()
        self.active = None
        await self.close()

    async def close(self):
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
            self.reader = None
        if self.ws:
            await self.ws.close()
            self.total_connection_s += (time.perf_counter_ns()-self.connected_ns) / 1e9
            self.ws = None


def wsl_worker_command(root: Path, precision: str) -> list[str]:
    project = PureWindowsPath(root)
    if not project.is_absolute() or len(project.drive) != 2 or project.drive[1] != ':':
        raise ValueError('absolute Windows drive path required for WSL worker')
    wsl_root = '/mnt/' + project.drive[0].lower() + '/' + '/'.join(project.parts[1:])
    wsl_root = wsl_root.rstrip('/')
    return ['wsl', '-d', 'Ubuntu', '--',
            wsl_root + '/.venv-wsl/bin/python', '-u',
            wsl_root + '/stt_eval/nemo_worker.py', '--precision', precision]


class ParakeetEngine:
    name, model = 'parakeet', MODEL_PARAKEET

    def __init__(self, root: Path, *, interval_s=.5, precision='fp32', emit=None,
                 command: list[str] | None = None, inference_timeout_s=60.):
        if precision not in ['fp32', 'fp16', 'bf16'] or not math.isfinite(interval_s) or not .1 <= interval_s <= 10:
            raise ValueError('invalid inference settings')
        if not math.isfinite(inference_timeout_s) or not 1 <= inference_timeout_s <= 60:
            raise ValueError('invalid inference timeout')
        self.inference_timeout_s = inference_timeout_s
        self.root, self.interval, self.precision = root, interval_s, precision
        self.emit = emit or (lambda event: None)
        self.command = command
        self.process, self.reader, self.partial_task = None, None, None
        self.worker_pid = None
        self.requests = {}
        self.request_number = 0
        self.metadata = {'streaming_mode': 'buffered_prefix', 'interval_s': interval_s,
                         'interval_definition': 'new_audio_wait_after_previous_inference',
                         'precision': precision, 'decoder': 'tdt', 'hints': 'unsupported',
                         'input_rate': 16000}

    async def open(self):
        command = self.command or wsl_worker_command(self.root.resolve(), self.precision)
        self.ready = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                            limit=4*1024*1024, env=local_worker_environment())
        self.reader = asyncio.create_task(self._read())
        try:
            # Cold imports / checkpoint extraction on a Windows-backed WSL folder
            # can take several minutes. This is setup time, never STT latency.
            details = await asyncio.wait_for(asyncio.shield(self.ready), 900)
            self.metadata.update(details)
        except BaseException:
            await self.close()
            raise

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                event = json.loads(line)
                kind = event['kind']
                if kind == 'started':
                    pid = event.get('pid')
                    if isinstance(pid,int) and pid > 1:
                        self.worker_pid = pid
                elif kind == 'ready':
                    if not self.ready.done():
                        self.ready.set_result(event['metadata'])
                elif kind == 'resource':
                    # Host receive timestamp avoids subtracting different OS clocks.
                    event['t_ns'] = time.perf_counter_ns()
                    self.emit(event)
                elif kind == 'maintenance':
                    event['t_ns'] = time.perf_counter_ns()
                    self.emit(event)
                elif kind in ['result', 'draft_result', 'error'] and event.get('request') in self.requests:
                    future = self.requests.pop(event['request'])
                    if not future.done():
                        if kind == 'error':
                            self.emit({'kind':'diagnostic','engine':self.name,
                                'cause':safe_code(event.get('cause')),'location':event.get('location')})
                            future.set_exception(DiagnosticError('nemo_' + safe_code(event.get('code'))))
                        elif kind == 'draft_result':
                            future.set_result(event['draft'])
                        else:
                            future.set_result((event['text'], time.perf_counter_ns()))
                elif kind == 'fatal':
                    self.emit({'kind':'diagnostic','engine':self.name,
                               'cause':safe_code(event.get('cause')), 'location':event.get('location')})
                    raise DiagnosticError('nemo_worker_' + safe_code(event.get('code')))
            raise DiagnosticError('nemo_worker_exited')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            for future in self.requests.values():
                if not future.done():
                    future.set_exception(error)
            self.requests.clear()

    async def _infer(self, pcm: bytes):
        started = time.perf_counter_ns()
        self.request_number += 1
        number = self.request_number
        future = asyncio.get_running_loop().create_future()
        self.requests[number] = future
        message = {'request': number, 'audio': base64.b64encode(pcm).decode('ascii')}
        try:
            self.process.stdin.write((json.dumps(message)+'\n').encode())
            await self.process.stdin.drain()
            result = await asyncio.wait_for(future, self.inference_timeout_s)
            self.emit({'kind': 'inference', 'engine': self.name, 't_ns': result[1],
                       'started_ns': started, 'audio_s': len(pcm)/32000,
                       'inference_ms': (result[1]-started)/1e6})
            return result
        finally:
            self.requests.pop(number, None)

    async def begin(self, callback):
        self.buffer = bytearray()
        self.callback, self.ending = callback, False
        self.last_size, self.last_result = 0, None

        async def partials():
            next_size = int(self.interval * 32000)
            while not self.ending:
                await asyncio.sleep(.02)
                if len(self.buffer) < next_size:
                    continue
                snapshot = bytes(self.buffer)
                text, when = await self._infer(snapshot)
                self.callback(text, when, False)
                self.last_size, self.last_result = len(snapshot), (text, when)
                # Coalesce missed windows instead of accumulating stale inference work.
                next_size = len(self.buffer) + int(self.interval * 32000)
        self.partial_task = asyncio.create_task(partials())

    async def release_unused_cache(self):
        if self.partial_task and not self.partial_task.done():
            raise DiagnosticError('cache_release_requires_idle_worker')
        self.request_number += 1
        number = self.request_number
        future = asyncio.get_running_loop().create_future()
        self.requests[number] = future
        try:
            self.process.stdin.write((json.dumps({'request':number,'operation':'release_unused_cache'})+'\n').encode())
            await self.process.stdin.drain()
            await asyncio.wait_for(future,30)
        finally:
            self.requests.pop(number,None)

    async def draft_timestamps(self, pcm):
        """Annotation aid only; the worker restores its original decoder settings."""
        if self.partial_task and not self.partial_task.done():
            raise DiagnosticError('annotation_requires_idle_worker')
        self.request_number += 1
        number = self.request_number
        future = asyncio.get_running_loop().create_future()
        self.requests[number] = future
        try:
            message = {'request': number, 'operation': 'draft_timestamps',
                       'audio': base64.b64encode(pcm).decode('ascii')}
            self.process.stdin.write((json.dumps(message)+'\n').encode())
            await self.process.stdin.drain()
            return await asyncio.wait_for(future,60)
        finally:
            self.requests.pop(number,None)

    async def feed(self, chunk):
        if self.partial_task.done():
            await self.partial_task
        if len(self.buffer) + len(chunk.pcm16) > 31*32000:
            raise DiagnosticError('nemo_turn_too_long')
        self.buffer.extend(chunk.pcm16)

    async def finish(self):
        self.ending = True
        await self.partial_task
        if self.last_size == len(self.buffer) and self.last_result:
            text, _ = self.last_result
        else:
            text, _ = await self._infer(bytes(self.buffer))
        self.callback(text, time.perf_counter_ns(), True)
        self.buffer.clear()

    async def abort(self):
        self.ending = True
        if self.partial_task:
            self.partial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.partial_task
        await self.close()

    async def close(self):
        self.ending = True
        if self.partial_task and self.partial_task is not asyncio.current_task():
            self.partial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.partial_task
            self.partial_task = None
        if hasattr(self,'buffer'):
            self.buffer.clear()
        if self.process and self.process.returncode is None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.process.stdin.write(b'{"quit":true}\n')
                await self.process.stdin.drain()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except asyncio.TimeoutError:
                if self.worker_pid and self.command is None:
                    killer = await asyncio.create_subprocess_exec('wsl','-d','Ubuntu','--',
                              'kill','-TERM',str(self.worker_pid), stdout=asyncio.subprocess.DEVNULL,
                              stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                elif self.worker_pid and os.name=='nt':
                    # A Windows venv launcher may have a separate model child.
                    # Kill only a still-owned descendant, never a name-wide match.
                    import psutil
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        child = psutil.Process(self.worker_pid)
                        if self.process.pid in [p.pid for p in child.parents()]:
                            child.terminate()
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
            self.reader = None
