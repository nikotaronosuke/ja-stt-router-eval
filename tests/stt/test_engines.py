import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from stt_eval.audio import AudioChunk
from stt_eval.engines import OpenAIEngine, ParakeetEngine, wsl_worker_command
from stt_eval.safety import AudioBudget

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_TMP = ROOT / '.cache' / 'test-tmp'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class FakeWebsocket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration()
        return json.dumps(event)

    async def close(self):
        self.closed = True


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def test_wsl_worker_follows_project_location_without_shell_quoting(self):
        command = wsl_worker_command(Path('D:/Projects/Example Project'), 'fp32')
        self.assertEqual(command, ['wsl', '-d', 'Ubuntu', '--',
                                   '/mnt/d/Projects/Example Project/.venv-wsl/bin/python', '-u',
                                   '/mnt/d/Projects/Example Project/stt_eval/nemo_worker.py', '--precision', 'fp32'])
        with self.assertRaises(ValueError):
            wsl_worker_command(Path('relative/project'), 'fp32')

    async def test_nemo_stdio_partial_final_and_close(self):
        engine = ParakeetEngine(ROOT, interval_s=.1,
                                command=[sys.executable, '-u', str(ROOT / 'tests' / 'stt' / 'fake_worker.py')])
        events = []
        await engine.open()
        try:
            draft = await engine.draft_timestamps(bytes(6400))
            self.assertEqual(draft['review'], 'model_estimate_only')
            await engine.release_unused_cache()
            await engine.begin(lambda *event: events.append(event))
            with self.assertRaises(RuntimeError):
                await engine.draft_timestamps(bytes(6400))
            await engine.feed(AudioChunk(0, bytes(6400), time.perf_counter_ns()))
            await asyncio.sleep(.1)
            await engine.finish()
            self.assertEqual(events[-1][0], '天気予報')
            self.assertTrue(events[-1][2])
            self.assertTrue(any(not event[2] for event in events))
        finally:
            await engine.close()
        self.assertIsNotNone(engine.process.returncode)

    async def test_openai_delta_accumulation_and_item_final(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            engine = OpenAIEngine(AudioBudget(Path(directory) / 'ledger', 5), allow_network=True)
            engine.ws = FakeWebsocket()
            engine.connected_ns = time.perf_counter_ns()
            engine.reader = asyncio.create_task(engine._read())
            events = []
            await engine.begin(lambda *event: events.append(event))
            await engine.ws.events.put({'type': 'conversation.item.input_audio_transcription.delta',
                                        'item_id': 'sample', 'delta': '天気'})
            await engine.ws.events.put({'type': 'conversation.item.input_audio_transcription.delta',
                                        'item_id': 'sample', 'delta': '予報'})
            await engine.ws.events.put({'type': 'conversation.item.input_audio_transcription.completed',
                                        'item_id': 'sample', 'transcript': '天気予報。'})
            await engine.finish()
            self.assertEqual([event[0] for event in events], ['天気', '天気予報', '天気予報。'])
            await engine.close()

    async def test_disconnect_unblocks_pending_turn(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            engine = OpenAIEngine(AudioBudget(Path(directory) / 'ledger', 5), allow_network=True)
            engine.ws = FakeWebsocket()
            engine.connected_ns = time.perf_counter_ns()
            engine.reader = asyncio.create_task(engine._read())
            await engine.begin(lambda *event: None)
            await engine.ws.events.put(None)
            with self.assertRaises(RuntimeError):
                await engine.finish()
            await engine.abort()

    async def test_close_cancels_partial_and_clears_audio(self):
        engine = ParakeetEngine(ROOT)
        engine.buffer = bytearray(b'private-dummy')
        engine.partial_task = asyncio.create_task(asyncio.sleep(30))
        task = engine.partial_task
        await engine.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(engine.buffer)

    def test_vocabulary_hints_are_validated(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            budget = AudioBudget(Path(directory) / 'ledger', 5)
            with self.assertRaises(ValueError):
                OpenAIEngine(budget, allow_network=True, keywords=['bad<term>'])
            with self.assertRaises(ValueError):
                OpenAIEngine(budget, allow_network=True, keywords='not-a-list')
            engine = OpenAIEngine(budget, allow_network=True, keywords=['天気予報'], hints=True)
            self.assertEqual(engine.metadata['hint_term_count'], 1)


if __name__ == '__main__':
    unittest.main()
