import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from pyytlounge import YtLoungeApi
from pyytlounge.exceptions import NotSupportedException

import yt_guard
from tests.test_discovery import FakeSession, SAMSUNG_DIAL_XML


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_guard_fails_liveness_but_idle_tv_remains_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            async with TestClient(TestServer(yt_guard.make_app(guard))) as client:
                self.assertEqual(200, (await client.get("/livez")).status)
                guard.last_progress = time.monotonic() - yt_guard.LIVENESS_TIMEOUT - 1
                self.assertEqual(503, (await client.get("/livez")).status)
                self.assertEqual(200, (await client.get("/healthz")).status)

    async def test_hung_pairing_has_a_deadline(self):
        async def hung(*_):
            await asyncio.Future()

        api = SimpleNamespace(auth=SimpleNamespace(screen_id=None), pair_with_screen_id=hung)
        screen = SimpleNamespace(screen_id="screen", screen_name="TV")
        with (
            patch.object(yt_guard, "get_screen_id_from_dial", AsyncMock(return_value=screen)),
            patch.object(yt_guard, "PAIR_REQUEST_TIMEOUT", 0.001),
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await yt_guard.pair_api(api, "192.0.2.1")

    async def test_cached_screen_can_refresh_after_restart_while_kids_disables_dial(self):
        with tempfile.TemporaryDirectory() as directory:
            api = YtLoungeApi("test")
            api.auth.screen_id = "known-screen"
            api.session = FakeSession({"192.0.2.1": SAMSUNG_DIAL_XML})
            with (
                patch.object(api, "pair_with_screen_id", AsyncMock(return_value=True)) as refresh,
                patch.object(yt_guard, "get_screen_id_from_dial", AsyncMock(side_effect=ConnectionError("DIAL is closed"))) as dial,
                patch.object(yt_guard, "AUTH", Path(directory) / "auth.json"),
            ):
                self.assertTrue(await yt_guard.pair_api(api, "192.0.2.1"))
                refresh.assert_awaited_once_with("known-screen")
                dial.assert_not_awaited()
                self.assertEqual(0o600, yt_guard.AUTH.stat().st_mode & 0o777)

    async def test_cached_pairing_does_not_prevent_discovery_after_dhcp_moves_tv(self):
        with tempfile.TemporaryDirectory() as directory:
            api = YtLoungeApi("test")
            api.auth.screen_id = "known-screen"
            api.session = FakeSession({"192.0.2.2": SAMSUNG_DIAL_XML})
            screen = SimpleNamespace(screen_id="known-screen", screen_name="TV")
            with (
                patch.object(api, "pair_with_screen_id", AsyncMock(return_value=True)),
                patch.object(yt_guard, "get_screen_id_from_dial", AsyncMock(return_value=screen)),
                patch.object(yt_guard, "AUTH", Path(directory) / "auth.json"),
                patch.object(yt_guard, "DISCOVERY_CIDR", "192.0.2.0/30"),
            ):
                paired, address, _ = await yt_guard.pair_with_discovery(api, api.session, "192.0.2.99", -60)
                self.assertTrue(paired)
                self.assertEqual("192.0.2.2", address, "cached cloud pairing concealed the TV's address change")

    async def test_repeated_failures_rebuild_transport_and_pair_again(self):
        await self.rebuild_recovers()

    async def test_failed_transport_shutdown_is_retried_instead_of_ending_guard(self):
        await self.rebuild_recovers(failed_stage="close")

    async def test_failed_transport_reopen_is_retried_instead_of_ending_guard(self):
        await self.rebuild_recovers(failed_stage="reopen")

    async def rebuild_recovers(self, failed_stage=None):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            api = YtLoungeApi("test", guard)
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            attempts = 0

            async def connect():
                nonlocal attempts
                attempts += 1
                if attempts <= 3:
                    raise ConnectionError("broken transport")
                api._sid, api._gsession, api._last_event_id = "session", "gsession", 1
                return True

            async def pair(*_):
                api.auth.lounge_id_token = "fresh-token"
                return True, "192.0.2.1", 100.0

            async def subscribe():
                await guard.now_playing_changed(SimpleNamespace(video_id="abcdefghijk", state=yt_guard.State.Playing))
                await asyncio.Future()

            sleep = asyncio.sleep

            async def retry_sleep(_):
                await sleep(0)

            with (
                patch.object(api, "connect", connect),
                patch.object(api, "close", AsyncMock(side_effect=[ConnectionError("shutdown failed"), None]
                             if failed_stage == "close" else None)) as close,
                patch.object(api, "__aenter__", AsyncMock(side_effect=[ConnectionError("reopen failed"), None]
                             if failed_stage == "reopen" else None)) as reopen,
                patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                patch.object(api, "subscribe", subscribe),
                patch.object(yt_guard, "pair_with_discovery", pair),
                patch.object(guard, "check", AsyncMock(return_value=None)) as check,
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                patch.object(yt_guard.log, "info"),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    for _ in range(30):
                        await asyncio.wait([task], timeout=0.001)
                        if check.await_count:
                            break
                    self.assertEqual(1 if failed_stage is None else 2, close.await_count)
                    self.assertEqual(2 if failed_stage == "reopen" else 1, reopen.await_count)
                    self.assertEqual(1, check.await_count)
                    self.assertEqual("fresh-token", api.auth.lounge_id_token)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_kids_observation_failure_retries_and_does_not_log_request_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            api = YtLoungeApi("test", guard, logger=unittest.mock.Mock())
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            sleep = asyncio.sleep

            async def retry_sleep(_):
                await sleep(0)

            with (
                patch.object(api, "connect", AsyncMock(side_effect=NotSupportedException("test-sensitive-query"))),
                patch.object(guard, "observe_kids", AsyncMock(side_effect=[ConnectionError("test-sensitive-query"), None])) as observe,
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                self.assertLogs(yt_guard.log, level="INFO") as logs,
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    for _ in range(20):
                        await asyncio.wait([task], timeout=0.001)
                        if observe.await_count >= 2:
                            break
                    self.assertGreaterEqual(observe.await_count, 2)
                    self.assertFalse(task.done(), "the guard ended after a recoverable Kids check error")
                    self.assertNotIn("test-sensitive-query", "\n".join(logs.output))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
