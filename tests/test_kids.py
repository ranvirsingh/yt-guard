import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from pyytlounge import YtLoungeApi
from pyytlounge.exceptions import NotSupportedException

import yt_guard


class KidsTests(unittest.IsolatedAsyncioTestCase):
    async def run_kids_then_youtube(self, included, paused=False, close_results=(True,),
                                   discovered=None, kids_attempts=None):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            guard.rules.data["kids_grace_seconds"] = 0  # Exercise close/recovery independently of the grace period.
            guard.rules.include_kids(included)
            if paused:
                guard.rules.pause(15)
            api = YtLoungeApi("test", guard)
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            api.kids_video_id = "abcdefghijk"
            guard.info["abcdefghijk"] = {"title": "Minecraft video", "channel": "Channel", "handle": ""}
            attempts = 0

            async def connect():
                nonlocal attempts
                attempts += 1
                if attempts <= (kids_attempts if kids_attempts is not None else len(close_results)):
                    raise NotSupportedException("Unsupported client")
                api._sid, api._gsession, api._last_event_id = "session", "gsession", 1
                return True

            resumed = asyncio.Event()

            async def subscribe():
                await guard.now_playing_changed(SimpleNamespace(video_id=None, state=yt_guard.State.Stopped))
                resumed.set()
                await asyncio.Future()

            sleep = asyncio.sleep

            async def retry_sleep(_):
                await sleep(0)

            with (
                patch.object(api, "connect", connect),
                patch.object(api, "subscribe", subscribe),
                patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                patch.object(yt_guard, "close_youtube", AsyncMock(side_effect=close_results)) as close,
                patch.object(yt_guard, "discover_tv_ip", AsyncMock(return_value=discovered)),
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                patch.object(yt_guard.log, "info"),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    await asyncio.wait_for(resumed.wait(), 0.1)
                    self.assertTrue(yt_guard.status_snapshot(guard)["connected"])
                    self.assertFalse(guard.kids_detected)
                    self.assertIsNone(guard.last_error)
                    if discovered:
                        self.assertEqual(discovered, guard.tv_ip)
                    return close.await_count
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_included_kids_closes_then_regular_youtube_recovers(self):
        self.assertEqual(1, await self.run_kids_then_youtube(True))

    async def test_excluded_kids_is_left_open_and_regular_youtube_recovers(self):
        self.assertEqual(0, await self.run_kids_then_youtube(False))

    async def test_pause_allows_kids_even_when_included(self):
        self.assertEqual(0, await self.run_kids_then_youtube(True, paused=True))

    async def test_failed_kids_close_is_retried(self):
        self.assertEqual(2, await self.run_kids_then_youtube(True, close_results=(False, True)))

    async def test_kids_blocking_recovers_when_tv_address_changes_but_cloud_pairing_survives(self):
        self.assertEqual(2, await self.run_kids_then_youtube(True, close_results=(False, True),
                                                          discovered="192.0.2.2", kids_attempts=1))

    async def test_checkbox_enable_and_disable_persist_without_changing_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            rules = yt_guard.Rules(path)
            before = {kind: rules.data[kind][:] for kind in yt_guard.KINDS}
            guard = yt_guard.Guard(object(), rules)
            self.assertFalse(rules.data["include_kids"])
            async with TestClient(TestServer(yt_guard.make_app(guard))) as client:
                await client.post("/settings/kids", data={"include_kids": "1"})
                self.assertTrue(yt_guard.Rules(path).data["include_kids"])
                page = await (await client.get("/")).text()
                self.assertIn('aria-describedby="kids-help" checked', page)
                await client.post("/settings/kids", data={})
                self.assertFalse(yt_guard.Rules(path).data["include_kids"])
                self.assertEqual(before, {kind: rules.data[kind] for kind in yt_guard.KINDS})


if __name__ == "__main__":
    unittest.main()
