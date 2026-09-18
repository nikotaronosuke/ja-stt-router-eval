"""The localhost recording and review study: saving, review binding, and the web gate."""
import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stt_eval.metrics import NATURAL_SPEECH_SOURCE
from stt_eval.recording_study import PROMPTS_FILE, CaptureStudy, study_handler

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


def prepared_root(directory):
    root = Path(directory).resolve()
    (root / 'fixtures').mkdir()
    (root / PROMPTS_FILE).write_bytes((ROOT / PROMPTS_FILE).read_bytes())
    return root


class CaptureStudyTests(unittest.TestCase):
    def test_meeting_save_accepts_relative_worker_output_and_matches_audio(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = prepared_root(directory)
            (root / 'trace').mkdir()
            pcm = bytes(32000)
            live = SimpleNamespace(output=Path(os.path.relpath(root / 'trace')),
                                   result={'canonical_sha256': hashlib.sha256(pcm).hexdigest()})
            study = CaptureStudy(root, live=live)
            study.pending = {'prompt': study.plan['tests'][0], 'route': 'meet_loopback'}
            study.recorder = SimpleNamespace(thread=Mock(is_alive=lambda: False), pcm=pcm, capture_summary={'errors': []})
            ident = study.save()
            row = study.data()['tests'][0]
            self.assertEqual(row['test_id'], ident)
            self.assertEqual(row['source'], NATURAL_SPEECH_SOURCE)
            self.assertTrue((root / row['live_trace']).is_file())
            self.assertTrue(study.audio(ident).startswith(b'RIFF'))

    def test_explicit_recording_consent_required_and_review_bound_to_audio(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = prepared_root(directory)
            study = CaptureStudy(root)
            prompt = study.plan['tests'][0]
            with self.assertRaises(ValueError):
                study.start({'prompt_id': prompt['test_id']})
            study.pending = {'prompt': prompt, 'route': 'direct_mic'}
            study.recorder = SimpleNamespace(thread=Mock(is_alive=lambda: False), pcm=bytes(32000))
            ident = study.save()
            row = study.data()['tests'][0]
            self.assertEqual(row['reference_review'], 'unreviewed')
            self.assertTrue(study.audio(ident).startswith(b'RIFF'))
            with self.assertRaises(ValueError):
                study.audio('../bad')
            review = {'test_id': ident, 'reference_text': '在庫確認', 'keyword': '在庫確認',
                      'start_s': .1, 'end_s': .8, 'confirmed': False}
            with self.assertRaises(ValueError):
                study.review(review)
            study.review({**review, 'confirmed': True, 'timing_confirmed': False, 'used_draft': True,
                          'start_s': None, 'end_s': None})
            row = study.data()['tests'][0]
            self.assertEqual(row['reference_review'], 'verified')
            self.assertEqual(row['reference_origin'], 'human_review_of_asr_draft')
            self.assertEqual(row['keyword_annotation']['spans'], [])
            study.review({**review, 'confirmed': True})
            row = study.data()['tests'][0]
            self.assertEqual(row['keyword_annotation']['spans'][0]['end_s'], .8)
            study.review_timing({'test_id': ident, 'start_s': .2, 'end_s': .9, 'confirmed': True})
            timed = study.data()['tests'][0]
            self.assertEqual(timed['reference_text'], row['reference_text'])
            self.assertEqual(timed['keyword_annotation']['spans'][0]['end_s'], .9)
            with self.assertRaises(ValueError):
                study.review_timing({'test_id': ident, 'start_s': .2, 'end_s': .9, 'confirmed': False})
            # A text-only re-review keeps the confirmed boundary.
            study.review({**review, 'confirmed': True, 'timing_confirmed': False, 'used_draft': True,
                          'start_s': None, 'end_s': None})
            row = study.data()['tests'][0]
            self.assertEqual(row['keyword_annotation']['spans'][0]['end_s'], .9)


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.study = CaptureStudy(prepared_root(self.directory.name))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = study_handler(self.study, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, method='GET', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def session_headers(self):
        status, headers, _ = self.request('/')
        self.assertEqual(status, 200)
        return {'Cookie': headers['Set-Cookie'].split(';')[0], 'Origin': self.origin}

    def test_data_requires_session_and_same_origin_including_audio(self):
        headers = self.session_headers()
        self.assertEqual(self.request('/api/state')[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Origin': self.origin})[0], 403)
        self.assertEqual(self.request('/api/state', headers=headers)[0], 200)
        self.assertEqual(self.request('/audio/pending.wav')[0], 403)
        for override in ({'Origin': 'https://outside.invalid'}, {'Host': 'outside.invalid'},
                         {'Sec-Fetch-Site': 'cross-site'}, {'Cookie': 'invalid'}):
            self.assertEqual(self.request('/api/state', headers={**headers, **override})[0], 403)
        self.assertEqual(self.request('/', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('/audio/../../private.wav', headers=headers)[0], 400)
        self.assertEqual(self.request('/.env', headers=headers)[0], 404)
        self.assertEqual(self.request('/timing')[0], 200)

    def test_post_consent_and_exception_do_not_expose_dummy_private_text(self):
        headers = {**self.session_headers(), 'Content-Type': 'application/json', 'X-Study-Request': '1'}
        payload = {'prompt_id': self.study.plan['tests'][0]['test_id']}
        self.assertEqual(self.request('/api/start', 'POST', headers, json.dumps(payload))[0], 400)
        self.assertIsNone(self.study.recorder)
        secret = 'dummy-secret-and-personal-sentence'
        with patch.object(self.study, 'state', side_effect=ValueError(secret)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                status, _, body = self.request('/api/state', headers=headers)
            self.assertEqual(status, 400)
            self.assertNotIn(secret, body.decode() + out.getvalue())
        self.assertEqual(self.request('/api/stop', 'POST', {**headers, 'Content-Length': 'bad'}, '{}')[0], 400)


if __name__ == '__main__':
    unittest.main()
