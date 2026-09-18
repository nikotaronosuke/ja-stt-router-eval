"""Explicit local test recording with human reference and timing review. No cloud API.

Serves a localhost page that records short test utterances, either from the
default microphone or from the far side of a meeting application through WASAPI
loopback, stores them as fixtures, and lets a person confirm the reference text
and the end position of the keyword. Model drafts only help navigation; nothing
becomes a reference or a timing annotation until a person confirms it.

    python -m stt_eval.recording_study --port 8766
    python -m stt_eval.recording_study --port 8766 --live-output artifacts/live-study --interval 0.5
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .capture import WasapiCapture
from .local_session import LocalSession
from .metrics import NATURAL_SPEECH_SOURCE
from .recorder import TestMicRecorder
from .safety import safe_error

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_FILE = 'fixtures/natural-speech-prompts.json'
AUDIO_DIR = 'fixtures/audio/natural'
MANIFEST_FILE = 'fixtures/manifests/natural-speech.json'
DRAFTS_DIR = 'artifacts/review-drafts'
ROUTES = ('direct_mic', 'meet_loopback', 'zoom_loopback')
MAX_CLIP_S = 30
MAX_REQUEST_BYTES = 16000
CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                           "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        writer.writeframes(pcm)
    return out.getvalue()


class LoopbackClip:
    """One bounded loopback recording, optionally fed to a live worker at the same time."""

    def __init__(self, live=None):
        self.stop_event = threading.Event()
        self.recording = threading.Event()
        self.pcm = None
        self.error = None
        self.capture_summary = None
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.live = live

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def work(self):
        blocks = []

        def on_chunk(chunk):
            blocks.append(chunk.pcm16)
            if self.live:
                self.live.feed(chunk)

        capturer = WasapiCapture(on_chunk=on_chunk, keep_render_active=True)
        try:
            capturer.start()
            self.recording.set()
            self.stop_event.wait(MAX_CLIP_S - .5)
        except Exception as error:
            self.error = safe_error(error)
        finally:
            capturer.stop()
            self.recording.clear()
            self.capture_summary = capturer.summary()
            if self.live:
                self.live.finish()
        if not self.error:
            self.pcm = b''.join(blocks)[:MAX_CLIP_S * 32000]
            if len(self.pcm) < 3200:
                self.error = 'recording_too_short'
                self.pcm = None


class CaptureStudy:
    page_filename = 'recording_study.html'

    def __init__(self, root=ROOT, live=None):
        self.root = root
        self.plan = json.loads((root / PROMPTS_FILE).read_text(encoding='utf-8'))
        self.directory = root / AUDIO_DIR
        self.manifest = root / MANIFEST_FILE
        self.recorder = None
        self.pending = None
        self.lock = threading.Lock()
        self.live = live

    def data(self):
        if self.manifest.exists():
            return json.loads(self.manifest.read_text(encoding='utf-8'))
        return {'schema_version': 1, 'tests': []}

    def state(self):
        recorder = self.recorder
        ready = bool(recorder and recorder.pcm)
        if self.pending and self.pending['route'] != 'direct_mic' and self.live and not self.live.result:
            ready = False
        takes = self.data()['tests']
        for take in takes:
            draft_path = self.root / DRAFTS_DIR / (take['test_id'] + '.json')
            if draft_path.is_file():
                draft = json.loads(draft_path.read_text(encoding='utf-8'))
                if draft.get('canonical_sha256') == take['canonical_sha256']:
                    take['review_draft'] = draft
                    from .spelling import timing_draft
                    take['timing_draft'] = timing_draft(take, draft)
        return {'plan': self.plan,
                'recording': bool(recorder and recorder.recording.is_set()),
                'busy': bool(recorder and recorder.thread.is_alive()),
                'ready': ready,
                'error': recorder.error if recorder else None,
                'pending': self.pending,
                'duration_s': len(recorder.pcm) / 32000 if recorder and recorder.pcm else None,
                'live_state': self.live.state if self.live else 'off',
                'live_error': self.live.error if self.live else None,
                'takes': takes}

    def start(self, data):
        if self.recorder and self.recorder.thread.is_alive():
            raise ValueError('already_recording')
        prompt = next(item for item in self.plan['tests'] if item['test_id'] == data['prompt_id'])
        route = data.get('route', 'direct_mic')
        if route not in ROUTES:
            raise ValueError('invalid_route')
        if data.get('consent_local_test_recording') is not True:
            raise ValueError('recording_consent_required')
        self.pending = {'prompt': prompt, 'route': route}
        if route != 'direct_mic' and self.live:
            self.live.begin()
        if route == 'direct_mic':
            self.recorder = TestMicRecorder(maximum_s=MAX_CLIP_S - .5)
        else:
            self.recorder = LoopbackClip(live=self.live)
        self.recorder.start()

    def save(self):
        if not self.recorder or self.recorder.thread.is_alive() or not self.recorder.pcm:
            raise ValueError('recording_not_ready')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        prompt, route = self.pending['prompt'], self.pending['route']
        name = f"{prompt['test_id']}_{route}"
        existing = self.data()
        for take in range(1, 10000):
            ident = f'{name}_{take:03}'
            destination = self.directory / (ident + '.wav')
            if not destination.exists():
                break
        else:
            raise ValueError('too_many_takes')
        pcm = self.recorder.pcm
        digest = hashlib.sha256(pcm).hexdigest()
        live_result = None
        if route != 'direct_mic' and self.live:
            live_result = self.live.result
            if not live_result:
                raise ValueError('live_result_not_ready')
            if live_result['canonical_sha256'] != digest:
                raise ValueError('live_recording_hash_mismatch')
            trace_path = self.live.output.resolve() / (ident + '.json')
            trace_relative = str(trace_path.relative_to(self.root.resolve())).replace('\\', '/')
        with destination.open('xb') as stream:
            stream.write(wav_bytes(pcm))
        row = {'test_id': ident, 'audio': '../audio/natural/' + ident + '.wav', 'condition': prompt['condition'],
               'route': route, 'audio_kind': 'human', 'reference_text': '未確認',
               'reference_review': 'unreviewed', 'keywords': [prompt['keyword']],
               'proper_nouns': [prompt['keyword']] if prompt['keyword'] == 'SharePoint' else [],
               'decision_keywords': [prompt['keyword']], 'source': NATURAL_SPEECH_SOURCE,
               'canonical_sha256': digest, 'duration_s': len(pcm) / 32000,
               'keyword_annotation': {'canonical_sha256': digest, 'spans': []}}
        if hasattr(self.recorder, 'capture_summary'):
            row['capture_summary'] = self.recorder.capture_summary
        if live_result:
            with trace_path.open('x', encoding='utf-8') as stream:
                json.dump(live_result, stream, ensure_ascii=False)
            row['live_trace'] = trace_relative
        existing['tests'].append(row)
        self.write(existing)
        self.recorder = None
        self.pending = None
        return ident

    def write(self, data):
        temporary = self.manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.manifest)

    def review(self, payload):
        from .keyword_timing import keyword_end_metrics
        data = self.data()
        row = next(item for item in data['tests'] if item['test_id'] == payload['test_id'])
        text = payload['reference_text'].strip()
        if not text or len(text) > 2000:
            raise ValueError('invalid_reference')
        keyword = payload['keyword'].strip()
        if not keyword or len(keyword) > 100:
            raise ValueError('invalid_keyword')
        # Text review and millisecond boundary review are separate human decisions.
        timing_confirmed = payload.get('timing_confirmed', payload.get('confirmed'))
        annotation = {'canonical_sha256': row['canonical_sha256'], 'spans': []}
        if not timing_confirmed and row['keywords'] == [keyword]:
            annotation = row['keyword_annotation']
        if timing_confirmed is True:
            annotation['spans'] = [{'keyword_index': 0, 'term': keyword, 'start_s': float(payload['start_s']),
                                    'end_s': float(payload['end_s']), 'occurrence': 1, 'review': 'human_verified'}]
        revised = {**row, 'keywords': [keyword], 'decision_keywords': [keyword]}
        keyword_end_metrics([], 0, revised, annotation, row['canonical_sha256'], row['duration_s'])
        if payload.get('confirmed') is not True:
            raise ValueError('human_review_required')
        row.update(reference_text=text, reference_review='verified', keywords=[keyword],
                   decision_keywords=[keyword], keyword_annotation=annotation,
                   proper_nouns=[keyword] if row['proper_nouns'] else [])
        assisted = payload.get('used_draft') is True or row.get('reference_origin') == 'human_review_of_asr_draft'
        row['reference_origin'] = 'human_review_of_asr_draft' if assisted else 'human_transcription'
        self.write(data)

    def audio(self, ident):
        if ident == 'pending':
            if self.recorder and self.recorder.pcm:
                return wav_bytes(self.recorder.pcm)
            raise ValueError('no_pending_audio')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', ident):
            raise ValueError('invalid_take')
        if not any(item['test_id'] == ident for item in self.data()['tests']):
            raise ValueError('unknown_take')
        return (self.directory / (ident + '.wav')).read_bytes()

    def review_timing(self, payload):
        from .keyword_timing import keyword_end_metrics
        from .spelling import occurrences
        data = self.data()
        row = next(item for item in data['tests'] if item['test_id'] == payload['test_id'])
        if (payload.get('confirmed') is not True or row['reference_review'] != 'verified'
                or len(occurrences(row['reference_text'], row['keywords'][0])) != 1):
            raise ValueError('unique_reviewed_keyword_required')
        annotation = {'canonical_sha256': row['canonical_sha256'], 'spans': [{
            'keyword_index': 0, 'term': row['keywords'][0], 'start_s': float(payload['start_s']),
            'end_s': float(payload['end_s']), 'occurrence': 1, 'review': 'human_verified',
            'review_method': 'auditory_boundary_review_with_model_navigation'}]}
        keyword_end_metrics([], 0, row, annotation, row['canonical_sha256'], row['duration_s'])
        row['keyword_annotation'] = annotation
        self.write(data)


def study_handler(study, port):
    live = study.live
    session = LocalSession(port)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, kind='application/json', code=200, bootstrap=False):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            if bootstrap:
                self.send_header('Set-Cookie', session.cookie())
            self.send_header('Content-Security-Policy', CONTENT_SECURITY_POLICY)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                path = self.path.split('?')[0]
                if path in ('/', '/timing'):
                    if not session.same_origin(self.headers, navigation=True):
                        return self.send({'error': 'same_origin_required'}, code=403)
                    if path == '/':
                        filename = getattr(study, 'page_filename', 'recording_study.html')
                    else:
                        filename = 'timing_review.html'
                    page = Path(__file__).with_name(filename).read_bytes()
                    return self.send(page, 'text/html; charset=utf-8', bootstrap=True)
                if not session.authorized(self.headers):
                    return self.send({'error': 'session_required'}, code=403)
                if path == '/api/state':
                    return self.send(study.state())
                if path.startswith('/audio/') and path.endswith('.wav'):
                    return self.send(study.audio(path[7:-4]), 'audio/wav')
                self.send({'error': 'not_found'}, code=404)
            except Exception as error:
                self.send({'error': safe_error(error)}, code=400)

        def do_POST(self):
            if (not session.authorized(self.headers) or self.headers.get('Origin') != session.origin
                    or self.headers.get('X-Study-Request') != '1'
                    or self.headers.get('Content-Type') != 'application/json'):
                return self.send({'error': 'same_origin_required'}, code=403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < MAX_REQUEST_BYTES:
                    return self.send({'error': 'invalid_length'}, code=400)
                data = json.loads(self.rfile.read(length))
                with study.lock:
                    if self.path == '/api/start':
                        study.start(data)
                    elif self.path == '/api/stop':
                        if study.recorder:
                            study.recorder.stop()
                    elif self.path == '/api/save':
                        return self.send({'test_id': study.save()})
                    elif self.path == '/api/review':
                        study.review(data)
                    elif self.path == '/api/review-timing':
                        study.review_timing(data)
                    elif self.path == '/api/prepare-drafts':
                        if study.state()['busy'] or study.pending:
                            raise ValueError('save_recording_first')
                        if not live:
                            raise ValueError('local_model_unavailable')
                        live.prepare_drafts()
                    elif self.path == '/api/close-model':
                        if study.state()['busy'] or study.pending:
                            raise ValueError('save_recording_first')
                        if live:
                            live.close()
                    elif self.path == '/api/shutdown':
                        if study.state()['busy'] or study.pending:
                            raise ValueError('save_recording_first')
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        return self.send({'error': 'not_found'}, code=404)
                self.send({'ok': True})
            except Exception as error:
                self.send({'error': safe_error(error)}, code=400)

    return Handler


def serve(port, live_output=None, interval_s=.5):
    live = None
    if live_output:
        from .live_worker import LiveStudyWorker
        live = LiveStudyWorker(ROOT, live_output, interval_s)
    study = CaptureStudy(live=live)
    server = ThreadingHTTPServer(('127.0.0.1', port), study_handler(study, port))
    print(json.dumps({'state': 'ready', 'port': port, 'cloud': False}), flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        if study.recorder:
            study.recorder.stop()
            study.recorder.thread.join(timeout=5)
        if live:
            live.close()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local recording and review page')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--live-output', type=Path)
    parser.add_argument('--interval', type=float, default=.5)
    args = parser.parse_args(argv)
    serve(args.port, args.live_output, args.interval)


if __name__ == '__main__':
    main()
