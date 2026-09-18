import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pyytlounge import YtLoungeApi
from pyytlounge.models import BLACKLISTED_CLIENTS
from yarl import URL

import yt_guard


class HandshakeResponse:
    status = 200
    reason = "OK"

    def __init__(self, client, video_id=None):
        device = {"type": "LOUNGE_SCREEN", "name": "TV",
                  "deviceInfo": json.dumps({"clientName": client})}
        events = [[0, ["c", "session"]], [1, ["S", "gsession"]],
                  [2, ["loungeStatus", {"devices": json.dumps([device])}]]]
        if video_id:
            events.append([3, ["nowPlaying", {"videoId": video_id, "state": "1"}]])
        payload = json.dumps(events)
        self.body = f"{len(payload) + 1}\n{payload}\n"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def text(self):
        return self.body

    def raise_for_status(self):
        pass


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_playback_feed_reconnects_instead_of_staying_green(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            api = YtLoungeApi("test", guard, logger=unittest.mock.Mock())
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            api.session = SimpleNamespace(post=lambda **_: HandshakeResponse("TVHTML5"))

            async def subscribe():
                await asyncio.Future()  # commands succeed, but the TV sends no playback data

            sleep = asyncio.sleep

            async def retry_sleep(_):
                await sleep(0)

            with (
                patch.object(api, "connect", AsyncMock(wraps=api.connect)) as connect,
                patch.object(api, "subscribe", subscribe),
                patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                patch.object(yt_guard, "POLL_INTERVAL", 0.001),
                patch.object(yt_guard, "PLAYBACK_TIMEOUT", 0.001, create=True),
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                patch.object(yt_guard.log, "info"),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    for _ in range(30):
                        await asyncio.wait([task], timeout=0.001)
                        if connect.await_count >= 2:
                            break
                    self.assertGreaterEqual(connect.await_count, 2, "silent playback feed was never recovered")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_blocked_video_in_handshake_waits_for_complete_session(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            guard.rules.add("videos", "abcdefghijk")
            api = YtLoungeApi("test", guard, logger=unittest.mock.Mock())
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            sent = []

            def post(**kwargs):
                URL("https://example.invalid").with_query(kwargs.get("params", {}))
                if kwargs["data"].get("req0__sc"):
                    sent.append(kwargs["data"]["req0__sc"])
                return HandshakeResponse("TVHTML5", "abcdefghijk")

            api.session = SimpleNamespace(post=post)

            async def subscribe():
                await guard.now_playing_changed(SimpleNamespace(video_id="abcdefghijk", state=yt_guard.State.Playing))
                await asyncio.Future()

            sleep = asyncio.sleep

            async def retry_sleep(_):
                await sleep(0)

            with (
                patch.object(api, "subscribe", subscribe),
                patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                patch.object(yt_guard, "video_info", AsyncMock(return_value={"title": "Video", "channel": "Channel", "handle": "channel"})),
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                patch.object(yt_guard.log, "info"),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    for _ in range(20):
                        await asyncio.wait([task], timeout=0.001)
                        if sent:
                            break
                    self.assertEqual(["dpadCommand"], sent,
                                     "blocked handshake video prevented connection before Back could be sent")
                    self.assertIsNotNone(guard.connected_since)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_kids_handshake_does_not_prevent_regular_youtube_detection(self):
        """Replay the actual partial handshake, then return to regular YouTube."""
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            logger = unittest.mock.Mock()
            api = YtLoungeApi("test", guard, logger=logger)
            guard.api = api
            api.auth.screen_id = "screen"
            api.auth.lounge_id_token = "token"
            transport = SimpleNamespace(post=lambda **_: HandshakeResponse(next(clients)))
            clients = iter([BLACKLISTED_CLIENTS[0], "TVHTML5"])
            api.session = transport

            async def subscribe():
                # Reproduce aiohttp's exact failure if an incomplete session is reused.
                URL("https://example.invalid").with_query(api._common_connection_parameters())
                await guard.now_playing_changed(SimpleNamespace(video_id="abcdefghijk", state=yt_guard.State.Playing))
                await asyncio.Future()

            sleep = asyncio.sleep

            async def retry_sleep(_):
                await sleep(0)

            with (
                patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                patch.object(api, "subscribe", subscribe),
                patch.object(guard, "check", AsyncMock(return_value=None)) as check,
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                patch.object(yt_guard.log, "info"),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                # Yield via the event loop rather than the patched retry sleep.
                try:
                    for _ in range(20):
                        await asyncio.wait([task], timeout=0.001)
                        if check.await_count:
                            break
                    self.assertEqual(1, check.await_count,
                                     "regular YouTube video was never detected after the Kids handshake")
                    self.assertEqual("abcdefghijk", guard.video_id)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_poll_detects_and_closes_video_when_back_has_no_followup_event(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            guard.rules.add("videos", "abcdefghijk")
            api = YtLoungeApi("test", guard)
            guard.api = api
            api.auth.screen_id = "screen"
            api.auth.lounge_id_token = "token"
            api._sid, api._gsession, api._last_event_id = "session", "gsession", 1
            guard.connected_since = time.time()
            queries = 0
            updates = asyncio.Queue()

            async def get_now_playing():
                nonlocal queries
                queries += 1
                await updates.put(SimpleNamespace(video_id="abcdefghijk", state=yt_guard.State.Playing))
                return True

            async def subscribe():
                while True:
                    await guard.now_playing_changed(await updates.get())

            with (
                patch.object(api, "subscribe", subscribe),
                patch.object(api, "get_now_playing", get_now_playing),
                patch.object(api, "send_dpad_command", AsyncMock(return_value=True)) as back,
                patch.object(yt_guard, "video_info", AsyncMock(return_value={"title": "Video", "channel": "Channel", "handle": "channel"})),
                patch.object(yt_guard, "close_youtube", AsyncMock()) as close,
                patch.object(yt_guard, "POLL_INTERVAL", 0.001),
                patch.object(yt_guard, "ACTION_GAP", 0),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    for _ in range(20):
                        await asyncio.wait([task], timeout=0.001)
                        if close.await_count:
                            break
                    self.assertGreaterEqual(queries, 2)
                    back.assert_awaited_once()
                    self.assertGreaterEqual(close.await_count, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_empty_now_playing_clears_previous_video(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            guard.video_id, guard.backed = "abcdefghijk", "abcdefghijk"
            await guard.now_playing_changed(SimpleNamespace(video_id=None, state=yt_guard.State.Stopped))
            self.assertIsNone(guard.video_id)
            self.assertIsNone(guard.backed)

    async def test_history_keeps_original_timestamp_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            guard = yt_guard.Guard(object(), rules)
            with patch.object(yt_guard, "video_info", AsyncMock(return_value={"title": "Video", "channel": "Channel", "handle": "channel"})):
                await guard.check("abcdefghijk")
            restarted = yt_guard.Guard(object(), rules)
            self.assertEqual(list(guard.recent), list(restarted.recent))


if __name__ == "__main__":
    unittest.main()
