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


class KidsGraceTests(unittest.IsolatedAsyncioTestCase):
    async def replay_profiles(self, steps, grace=None):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            rules.include_kids(True)
            if grace is not None:
                rules.data["kids_grace_seconds"] = grace
            guard = yt_guard.Guard(object(), rules)
            api = YtLoungeApi("test", guard)
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            api.kids_video_id = "abcdefghijk"
            guard.info["abcdefghijk"] = {"title": "Minecraft video", "channel": "Channel", "handle": ""}
            profiles = iter(steps)
            current = [None, 0]
            closed_at, snapshots = [], []
            finished = asyncio.Event()

            async def connect():
                current[:] = next(profiles)
                if current[0] == "kids":
                    raise NotSupportedException("Unsupported client")
                api._sid, api._gsession, api._last_event_id = "session", "gsession", 1
                return True

            async def subscribe():
                await guard.now_playing_changed(SimpleNamespace(video_id=None, state=yt_guard.State.Stopped))
                if current[0] == "end":
                    finished.set()
                    await asyncio.Future()
                # A regular profile was confirmed; return to reconnect to the next profile.

            async def close(*_):
                closed_at.append(current[1])
                return True

            sleep = asyncio.sleep

            async def retry_sleep(_):
                snapshots.append(yt_guard.status_snapshot(guard))
                await sleep(0)

            with (
                patch.object(api, "connect", connect),
                patch.object(api, "subscribe", subscribe),
                patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                patch.object(yt_guard, "close_youtube", close),
                patch.object(yt_guard, "monotonic", lambda: 100 + current[1], create=True),
                patch.object(yt_guard.asyncio, "sleep", retry_sleep),
                patch.object(yt_guard.log, "info"),
            ):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    await asyncio.wait_for(finished.wait(), 0.5)
                    return closed_at, snapshots, guard
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_switching_to_regular_profile_during_default_grace_keeps_youtube_open(self):
        closed, _, _ = await self.replay_profiles([("kids", 0), ("kids", 29), ("end", 29.5)])
        self.assertEqual([], closed, "YouTube was closed before there was time to switch profiles")

    async def test_remaining_in_kids_closes_only_after_default_thirty_seconds(self):
        closed, snapshots, guard = await self.replay_profiles([("kids", 0), ("kids", 29), ("kids", 30), ("end", 31)])
        self.assertEqual([30], closed)
        self.assertEqual([30, 1], [s["kids_grace_remaining"] for s in snapshots[:2]])
        self.assertIn("Switch", snapshots[0]["status_title"])
        self.assertFalse(guard.kids_detected)
        self.assertIsNone(guard.kids_since)

    async def test_configurable_grace_is_used_for_kids_closing(self):
        closed, _, _ = await self.replay_profiles([("kids", 0), ("kids", 30), ("kids", 44), ("kids", 45), ("end", 46)], grace=45)
        self.assertEqual([45], closed)

    async def test_returning_to_kids_after_regular_profile_starts_a_new_grace(self):
        closed, snapshots, _ = await self.replay_profiles([("kids", 0), ("youtube", 20), ("kids", 40), ("kids", 69), ("end", 69.5)])
        self.assertEqual([], closed)
        kids = [s for s in snapshots if s["kids_detected"]]
        self.assertEqual([30, 30, 1], [s["kids_grace_remaining"] for s in kids])

    async def test_grace_ui_defaults_to_thirty_and_saves_with_checkbox(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            rules = yt_guard.Rules(path)
            guard = yt_guard.Guard(object(), rules)
            before = {k: rules.data[k][:] for k in yt_guard.KINDS}
            async with TestClient(TestServer(yt_guard.make_app(guard))) as client:
                page = await (await client.get("/")).text()
                self.assertIn('name="kids_grace_seconds"', page)
                self.assertEqual(30, rules.data["kids_grace_seconds"])
                await client.post("/settings/kids", data={"include_kids": "1", "kids_grace_seconds": "45"})
                reloaded = yt_guard.Rules(path)
                self.assertTrue(reloaded.data["include_kids"])
                self.assertEqual(45, reloaded.data["kids_grace_seconds"])
                self.assertEqual(before, {k: reloaded.data[k] for k in yt_guard.KINDS})
                for invalid in ("-1", "601", "nan", "1.5"):
                    response = await client.post("/settings/kids", data={"kids_grace_seconds": invalid})
                    self.assertEqual(400, response.status)
                    self.assertTrue(rules.data["include_kids"])
                    self.assertEqual(45, rules.data["kids_grace_seconds"])
                guard.kids_since = 100
                await client.post("/settings/kids", data={"kids_grace_seconds": "30"})
                self.assertFalse(rules.data["include_kids"])
                self.assertIsNone(guard.kids_since)

    async def test_banner_tracks_deadline_and_disappears_when_protection_is_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            rules.include_kids(True)
            guard = yt_guard.Guard(object(), rules)
            guard.kids_detected = True
            guard.kids_since = 75.5
            guard.kids_why = "keyword: minecraft"
            guard.video_id = "abcdefghijk"
            guard.info[guard.video_id] = {"title": '<Blocked & "video">', "channel": "Channel", "handle": ""}
            with patch.object(yt_guard, "monotonic", return_value=100):
                async with TestClient(TestServer(yt_guard.make_app(guard))) as client:
                    data = await (await client.get("/api/status")).json()
                    self.assertEqual(5.5, data["kids_countdown"]["remaining_seconds"])
                    self.assertEqual(30, data["kids_countdown"]["total_seconds"])
                    page = await (await client.get("/")).text()
                    self.assertIn('data-remaining="5.5"', page)
                    self.assertIn('&lt;Blocked &amp; &quot;video&quot;&gt;', page)
                    guard.kids_why = None  # An allowed video cancels the banner.
                    self.assertIsNone((await (await client.get("/api/status")).json())["kids_countdown"])
                    guard.kids_why = "video"
                    guard.kids_close_ok = True
                    self.assertIsNone(yt_guard.status_snapshot(guard)["kids_countdown"])
                    guard.kids_close_ok = None
                    rules.pause(15)
                    self.assertIsNone(yt_guard.status_snapshot(guard)["kids_countdown"])
                    rules.pause(0)
                    await client.post("/settings/kids", data={"kids_grace_seconds": "30"})
                    self.assertIsNone((await (await client.get("/api/status")).json())["kids_countdown"])
                    page = await (await client.get("/")).text()
                    self.assertIn('data-retrying="false" hidden', page)

    async def test_expired_failed_close_banner_reports_retry_instead_of_success(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            rules.include_kids(True)
            guard = yt_guard.Guard(object(), rules)
            guard.kids_detected = True
            guard.kids_since = 60
            guard.kids_why = "video"
            guard.video_id = "abcdefghijk"
            guard.kids_close_ok = False
            with patch.object(yt_guard, "monotonic", return_value=100):
                countdown = yt_guard.status_snapshot(guard)["kids_countdown"]
                self.assertEqual(0, countdown["remaining_seconds"])
                self.assertTrue(countdown["retrying"])


if __name__ == "__main__":
    unittest.main()
