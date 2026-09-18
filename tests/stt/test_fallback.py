import time
import unittest

from stt_eval.audio import AudioChunk
from stt_eval.fallback import FallbackEngine


class Budget:
    def __init__(self):
        self.reservations = []

    def reserve(self, seconds):
        self.reservations.append(seconds)


class Engine:
    model = 'fake'
    metadata = {}

    def __init__(self, fail=None):
        self.fail = fail
        self.chunks = []
        self.budget = Budget()
        self.opened = False

    async def open(self):
        self.opened = True
        if self.fail == 'open':
            raise RuntimeError('injected')

    async def begin(self, callback):
        self.callback = callback

    async def feed(self, chunk):
        if self.fail == 'feed' and chunk.sequence == 2:
            raise RuntimeError('injected')
        self.chunks.append(chunk)

    async def finish(self):
        if self.fail == 'finish':
            raise RuntimeError('injected')
        self.callback('テスト', time.perf_counter_ns(), True)

    async def abort(self):
        pass

    async def close(self):
        pass


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_mid_turn_and_final_failure_preserve_each_frame_exactly_once(self):
        for failure in ['open', 'feed', 'finish']:
            primary, secondary = Engine(failure), Engine()
            engine = FallbackEngine(primary, secondary, allow_cloud_test_audio=True)
            await engine.open()
            await engine.begin(lambda *args: None)
            chunks = [AudioChunk(index, bytes([index, 0]) * 320, time.perf_counter_ns()) for index in range(6)]
            for chunk in chunks:
                await engine.feed(chunk)
            await engine.finish()
            self.assertEqual(chunks, secondary.chunks)
            self.assertEqual(engine.stats['dropped_frames'], 0)
            self.assertEqual(secondary.budget.reservations, [31.])
            await engine.close()

    async def test_healthy_local_turn_never_opens_or_charges_cloud(self):
        primary, secondary = Engine(), Engine()
        engine = FallbackEngine(primary, secondary, allow_cloud_test_audio=True)
        await engine.open()
        await engine.begin(lambda *args: None)
        await engine.feed(AudioChunk(0, bytes(640), time.perf_counter_ns()))
        await engine.finish()
        self.assertFalse(secondary.opened)
        self.assertEqual(secondary.budget.reservations, [])

    async def test_replay_buffer_is_bounded_and_cloud_requires_opt_in(self):
        with self.assertRaises(PermissionError):
            FallbackEngine(Engine(), Engine())
        engine = FallbackEngine(Engine(), Engine(), allow_cloud_test_audio=True, max_turn_s=1)
        await engine.open()
        await engine.begin(lambda *args: None)
        with self.assertRaises(ValueError):
            await engine.feed(AudioChunk(0, bytes(32002), time.perf_counter_ns()))

    async def test_fixture_approval_does_not_authorize_live_audio(self):
        engine = FallbackEngine(Engine(), Engine(), allow_cloud_test_audio=True)
        await engine.open()
        await engine.begin(lambda *args: None)
        with self.assertRaises(PermissionError):
            await engine.feed(AudioChunk(0, bytes(640), time.perf_counter_ns(), stream='loopback'))


if __name__ == '__main__':
    unittest.main()
