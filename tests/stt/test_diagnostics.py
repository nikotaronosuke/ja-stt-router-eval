"""Capture drain, memory gate, waveform diagnostics, the gated benchmark and the route probe."""
import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from stt_eval.capture import WasapiCapture
from stt_eval.diagnostics import MemoryGate, wave_metrics
from stt_eval.gated_benchmark import aggregate, compare, trial_metrics
from stt_eval.resources import ResourceSampler
from stt_eval.route_probe import RouteProbe, RouteStudy, aligned_capture, result_paths

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class DiagnosticTests(unittest.TestCase):
    def test_route_retry_preserves_failure_and_allows_only_one_startup_retry(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            output, capture = result_paths(root)
            output.parent.mkdir(parents=True)
            failed = json.dumps({'status': 'local_failure', 'startup_s': None, 'rows': []})
            output.write_text(failed, encoding='utf-8')
            retry, retry_capture = result_paths(root)
            self.assertNotEqual(output, retry)
            retry.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                result_paths(root)
            self.assertEqual(output.read_text(encoding='utf-8'), failed)

    def test_route_alignment_finds_the_offset(self):
        from stt_eval.audio import encode_pcm16
        source = encode_pcm16(np.random.default_rng(42).normal(0, .04, 3200))
        matched, metrics = aligned_capture(source, bytes(3200) + source + bytes(6400))
        self.assertEqual(matched, source)
        self.assertAlmostEqual(metrics['waveform_correlation'], 1)
        self.assertEqual(metrics['alignment_offset_ms'], 100)
        with self.assertRaises(ValueError):
            aligned_capture(source, bytes(100))

    def test_route_study_requires_consent(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            study = RouteStudy(Path(directory))
            with self.assertRaises(ValueError):
                study.start({})
            self.assertIsNone(study.recorder)
            self.assertEqual(study.state()['phase'], 'idle')

    @unittest.skipUnless(os.name == 'nt', 'winsound playback is Windows-only')
    def test_route_cancel_releases_capture_without_saving(self):
        from stt_eval.audio import encode_pcm16
        source = encode_pcm16(np.random.default_rng(42).normal(0, .04, 3200))
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            probe = RouteProbe(root)
            capture = Mock()
            capture.start.side_effect = probe.stop
            inputs = [{'source': 'nonpersonal_local_windows_tts', '_audio_path': Path('unused')} for _ in range(5)]
            with patch('stt_eval.route_probe.validate_manifest', return_value=inputs), \
                    patch('stt_eval.route_probe.load_fixture', return_value=(source, {'duration_s': .2})), \
                    patch('stt_eval.route_probe.WasapiCapture', return_value=capture), \
                    patch('winsound.PlaySound') as playback:
                probe.work()
            capture.stop.assert_called()
            self.assertEqual(probe.phase, 'cancelled')
            self.assertFalse(probe.recording.is_set())
            self.assertFalse(list(root.rglob('*')))
            self.assertTrue(all(call.args[0] is None for call in playback.call_args_list))

    def test_aggregate_never_copies_dummy_private_reference_or_transcript(self):
        private = 'テスト専用の非公開ダミー文章'
        test = {'reference_text': private, 'keywords': ['ダミー'], 'proper_nouns': [],
                'reference_review': 'verified', 'keyword_annotation': None}
        events = [{'text': private, 't_ns': 1000000, 'final': True}]
        row = trial_metrics(test, events, 0, {'canonical_sha256': 'dummy', 'duration_s': 1}, 'clip_01')
        report = json.dumps({'rows': [row], 'summary': aggregate([row])}, ensure_ascii=False)
        self.assertNotIn(private, report)
        self.assertNotIn('ダミー', report)
        self.assertEqual(row['edit_count'], 0)
        self.assertFalse(aggregate([row])['meaning_review_complete'])

    def test_capture_stop_drains_queued_blocks_without_recording_files(self):
        chunks = []
        capture = WasapiCapture(on_chunk=chunks.append)
        capture.formats = {name: {'rate': 48000, 'channels': 2} for name in ('loopback', 'microphone')}
        block = np.ones((960, 2), dtype='<f4') * .1
        capture.queue.put(('loopback', block.tobytes(), 960, time.perf_counter_ns()))
        capture.stop_event.set()
        capture._consume()
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].pcm16), 640)
        self.assertEqual(capture.stats['loopback']['input_samples'], 1920)
        self.assertFalse(capture.summary()['audio_saved'])
        self.assertTrue(capture.queue.empty())

    def test_live_resource_retention_has_an_explicit_bound(self):
        sampler = ResourceSampler(max_samples=2)
        for number in range(5):
            sampler.samples.append({'sample': number})
        self.assertEqual([row['sample'] for row in sampler.samples], [3, 4])

    def test_memory_threshold_duration_reset_acute_missing_and_drop(self):
        gate = MemoryGate()
        for second in range(5):
            self.assertIsNone(gate.observe(900, 1000, second))
        self.assertEqual(gate.observe(900, 1000, 5), 'memory_persistent_pressure')
        self.assertIsNone(gate.observe(3000, 3000, 6))
        self.assertEqual(gate.observe(1900, 3000, 7), 'memory_rapid_drop')
        self.assertEqual(MemoryGate().observe(500, 3000, 0), 'memory_acute_pressure')
        self.assertEqual(MemoryGate().observe(3000, 250, 0), 'memory_acute_pressure')
        self.assertEqual(MemoryGate().observe(3000, None, 0), 'memory_counter_missing')

    def test_stereo_cancellation_and_one_channel_silence_are_distinguished(self):
        tone = np.sin(np.arange(4800) * 2 * np.pi * 440 / 48000) * .2
        normal = wave_metrics(np.column_stack([tone, tone]), 48000)
        canceled = wave_metrics(np.column_stack([tone, -tone]), 48000)
        one = wave_metrics(np.column_stack([tone, np.zeros_like(tone)]), 48000)
        self.assertAlmostEqual(normal['mono_power_ratio'], 1)
        self.assertEqual(canceled['mono_power_ratio'], 0)
        self.assertAlmostEqual(one['mono_power_ratio'], .5)
        self.assertEqual(one['channel_rms'][1], 0)


class GatedBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_memory_collection_does_not_block_audio_pacing(self):
        instances = []

        class Engine:
            def __init__(self, *args, **kwargs):
                self.process = None
                self.closed = False
                instances.append(self)

            async def open(self):
                pass

            async def close(self):
                self.closed = True

            async def _infer(self, pcm):
                return '天気予報', time.perf_counter_ns()

            async def begin(self, callback):
                self.callback = callback

            async def feed(self, chunk):
                self.callback('天気予報', time.perf_counter_ns(), False)

            async def finish(self):
                self.callback('天気予報', time.perf_counter_ns(), True)

        class Counters:
            def sample(self):
                return {'committed_bytes': 2**30, 'commit_limit_bytes': 8 * 2**30}

            def close(self):
                pass

        def slow_apps():
            time.sleep(.08)
            return {}

        template = {'reference_text': '天気予報', 'keywords': ['天気予報'], 'proper_nouns': [],
                    'reference_review': 'verified', 'keyword_annotation': None, 'canonical_sha256': 'dummy',
                    '_audio_path': Path('dummy'), 'duration_s': .1}
        opened = []
        original = socket.socket.connect

        def guard(self, address, *args, **kwargs):
            opened.append(address)
            raise AssertionError('network_access_attempted')

        socket.socket.connect = guard
        try:
            with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
                root = Path(directory)
                memory = ({'system_available_mib': 7000, 'system_ram_mib': 9000, 'app_ram_mib': 100}, None)
                with patch('stt_eval.gated_benchmark.validate_manifest', return_value=[template.copy() for _ in range(28)]), \
                        patch('stt_eval.gated_benchmark.load_fixture',
                              return_value=(bytes(3200), {'canonical_sha256': 'dummy', 'duration_s': .1})), \
                        patch('stt_eval.gated_benchmark.memory_baseline',
                              return_value={'load_allowed': True, 'physical_available_min_mib': 8000}), \
                        patch('stt_eval.gated_benchmark.ParakeetEngine', Engine), \
                        patch('stt_eval.gated_benchmark.PagingCounters', Counters), \
                        patch('stt_eval.gated_benchmark.application_memory', slow_apps), \
                        patch('stt_eval.gated_benchmark._windows_memory', return_value=memory):
                    result = await compare(root, 'parakeet', 5, root / 'result.json')
                record = json.loads((root / 'result.json').read_text(encoding='utf-8'))
                self.assertEqual(result['status'], 'ok')
                self.assertTrue(record['pacing_valid'])
                self.assertEqual(record['measurement_version'], 2)
                self.assertTrue(instances[0].closed)
                self.assertFalse((root / 'artifacts' / '.run.lock').exists())
        finally:
            socket.socket.connect = original
        self.assertEqual(opened, [])

    async def test_the_memory_gate_refuses_to_load(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            template = {'canonical_sha256': 'dummy', '_audio_path': Path('dummy')}
            with patch('stt_eval.gated_benchmark.validate_manifest', return_value=[template] * 5), \
                    patch('stt_eval.gated_benchmark.memory_baseline',
                          return_value={'load_allowed': True, 'physical_available_min_mib': 1000}):
                result = await compare(root, 'parakeet', 5, root / 'gated.json')
            self.assertEqual(result['status'], 'not_started_memory_gate')
            record = json.loads((root / 'gated.json').read_text(encoding='utf-8'))
            self.assertEqual(record['preflight']['stop_reason'], 'historical_startup_peak_headroom_insufficient')

    async def test_manifest_shape_is_checked_before_anything_loads(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root = Path(directory)
            with patch('stt_eval.gated_benchmark.validate_manifest', return_value=[{}] * 3):
                with self.assertRaises(ValueError):
                    await compare(root, 'parakeet', 5, root / 'short.json')
                with self.assertRaises(ValueError):
                    await compare(root, 'parakeet', 3, root / 'total.json', expected_total=28)
            with self.assertRaises(ValueError):
                await compare(root, 'unknown', 5, root / 'engine.json')


if __name__ == '__main__':
    unittest.main()
