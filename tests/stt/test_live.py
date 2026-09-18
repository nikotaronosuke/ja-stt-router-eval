import asyncio
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stt_eval.audio import AudioChunk
from stt_eval.live import listen


class WaitingEngine:
    name = 'parakeet'

    def __init__(self):
        self.closed = False

    async def open(self):
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_loopback_packets_finalize_active_turn_before_stop(self):
        class Engine(WaitingEngine):
            def __init__(self):
                super().__init__()
                self.finished = asyncio.Event()

            async def open(self):
                pass

            async def begin(self, callback):
                pass

            async def feed(self, chunk):
                pass

            async def finish(self):
                self.finished.set()

        class Capture:
            errors = []

            def __init__(self, on_chunk, on_meter):
                self.on_chunk = on_chunk

            def start(self):
                self.on_chunk(AudioChunk(0, b'\x00\x10' * 320, time.perf_counter_ns()))

            def stop(self):
                pass

            def summary(self):
                return {'errors': []}

        engine, stop = Engine(), threading.Event()
        with patch('stt_eval.live.WasapiCapture', Capture), patch('stt_eval.live.ResourceSampler') as sampler:
            sampler.return_value.summary.return_value = {}
            task = asyncio.create_task(listen([engine], stop, lambda _: None, seconds=10, silence_s=.1))
            try:
                await asyncio.wait_for(engine.finished.wait(), 1)
                self.assertFalse(stop.is_set())
                self.assertFalse(task.done())
            finally:
                stop.set()
                await asyncio.wait_for(task, 1)

    async def test_stop_during_model_loading_closes_engine_before_capture(self):
        engine, stop = WaitingEngine(), threading.Event()

        async def stop_soon():
            await asyncio.sleep(.03)
            stop.set()

        with patch('stt_eval.live.WasapiCapture') as capture:
            stopper = asyncio.create_task(stop_soon())
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(listen([engine], stop, lambda _: None), 1)
            await stopper
            capture.return_value.start.assert_not_called()
            self.assertTrue(engine.closed)

    async def test_cancellation_during_shutdown_waits_for_worker_release(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Engine(WaitingEngine):
            async def open(self):
                pass

            async def close(self):
                entered.set()
                await release.wait()
                self.closed = True

        engine, stop = Engine(), threading.Event()
        stop.set()
        with patch('stt_eval.live.WasapiCapture'), patch('stt_eval.live.ResourceSampler'):
            task = asyncio.create_task(listen([engine], stop, lambda _: None))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(engine.closed)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(engine.closed)

    async def test_unlimited_session_still_stops_on_request(self):
        engine, stop = WaitingEngine(), threading.Event()

        async def opened():
            stop.set()  # stop requested right after the model is ready

        engine.open = opened
        with patch('stt_eval.live.WasapiCapture') as capture, patch('stt_eval.live.ResourceSampler') as sampler:
            sampler.return_value.summary.return_value = {}
            capture.return_value.summary.return_value = {'errors': []}
            result = await asyncio.wait_for(listen([engine], stop, lambda _: None, seconds=None), 1)
        self.assertEqual(result['turns'], 0)
        self.assertTrue(engine.closed)
        with self.assertRaises(ValueError):
            await listen([WaitingEngine()], threading.Event(), lambda _: None, seconds=0)

    async def test_invalid_live_duration_does_not_open_device(self):
        with patch('stt_eval.live.WasapiCapture') as capture:
            with self.assertRaises(ValueError):
                await listen([WaitingEngine()], threading.Event(), lambda _: None, seconds=float('nan'))
            capture.assert_not_called()

    async def test_cloud_disabled_error_and_timeout_do_not_open_network(self):
        opened = []
        original = socket.socket.connect

        def guard(self, address, *args, **kwargs):
            opened.append(address)
            raise AssertionError('network_access_attempted')

        socket.socket.connect = guard
        try:
            for error in (ValueError('dummy-private'), TimeoutError('dummy-secret')):
                async def opening(error=error):
                    raise error

                async def closing():
                    pass

                engine = SimpleNamespace(name='parakeet', open=opening, close=closing)
                with patch('stt_eval.live.WasapiCapture') as capture:
                    with self.assertRaises(type(error)):
                        await listen([engine], threading.Event(), lambda _: None, seconds=1)
                    capture.return_value.start.assert_not_called()
            cloud = SimpleNamespace(name='openai')
            with self.assertRaises(PermissionError):
                await listen([cloud], threading.Event(), lambda _: None, seconds=1)
        finally:
            socket.socket.connect = original
        self.assertEqual(opened, [])


if __name__ == '__main__':
    unittest.main()
