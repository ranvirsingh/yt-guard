import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyytlounge import YtLoungeApi
from pyytlounge.exceptions import NotSupportedException
from pyytlounge.models import BLACKLISTED_CLIENTS

import yt_guard


class Response:
    status, reason = 200, "OK"

    def __init__(self, batches):
        self.body = "".join(f"{len(payload) + 1}\n{payload}\n"
                            for payload in map(json.dumps, batches))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def text(self):
        return self.body


# Synthetic video IDs: do not retain household playback IDs in fixtures.
def handshake(client, video="blocked0001"):
    device = {"type": "LOUNGE_SCREEN", "name": "TV",
              "deviceInfo": json.dumps({"clientName": client})}
    # The current video arrives after loungeStatus, which rejects Kids.
    return [[[0, ["c", "sid"]], [1, ["S", "gsession"]],
             [2, ["loungeStatus", {"devices": json.dumps([device])}]]],
            [[3, ["playlistModified", {"videoId": video, "firstVideoId": "first000001"}]],
             [4, ["autoplayUpNext", {"videoId": "next0000001"}]]]]


class KidsSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_keeps_blacklist_and_sends_only_existing_handshake(self):
        api = yt_guard.SnapshotLoungeApi("test", logger=Mock())
        api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
        post = Mock(return_value=Response(handshake("TVHTML5_FOR_KIDS")))
        api.session = SimpleNamespace(post=post)
        with self.assertRaises(NotSupportedException):
            await api.connect()
        self.assertEqual("blocked0001", api.kids_video_id)
        self.assertIsNone(api._last_event_id, "Kids must still be rejected before establishing a session")
        self.assertIn("TVHTML5_FOR_KIDS", BLACKLISTED_CLIENTS)
        post.assert_called_once()
        self.assertNotIn("TYPE", post.call_args.kwargs["data"])
        self.assertNotIn("req0__sc", post.call_args.kwargs["data"])
        baseline = YtLoungeApi("test", logger=Mock())
        baseline.auth.screen_id, baseline.auth.lounge_id_token = "screen", "token"
        baseline_post = Mock(return_value=Response(handshake("TVHTML5_FOR_KIDS")))
        baseline.session = SimpleNamespace(post=baseline_post)
        with self.assertRaises(NotSupportedException):
            await baseline.connect()
        self.assertEqual(baseline_post.call_args, post.call_args,
                         "Extracting the ID must not change the TV handshake")

    async def test_snapshot_clears_previous_id_on_empty_invalid_and_regular_handshakes(self):
        api = yt_guard.SnapshotLoungeApi("test", logger=Mock())
        api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
        for client, video in [("TVHTML5_FOR_KIDS", "abcdefghijk"),
                              ("TVHTML5_FOR_KIDS", ""),
                              ("TVHTML5_FOR_KIDS", "invalid"), ("TVHTML5", "abcdefghijk")]:
            api._connection_lost()
            api.session = SimpleNamespace(post=Mock(return_value=Response(handshake(client, video))))
            if client == "TVHTML5_FOR_KIDS":
                with self.assertRaises(NotSupportedException):
                    await api.connect()
                self.assertEqual(video if len(video) == 11 else None, api.kids_video_id)
            else:
                self.assertTrue(await api.connect())
                self.assertIsNone(api.kids_video_id)

    async def test_up_next_without_current_id_does_not_become_current_video(self):
        api = yt_guard.SnapshotLoungeApi("test", logger=Mock())
        api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
        batches = handshake("TVHTML5_FOR_KIDS", "")
        batches[1].pop(0)
        api.session = SimpleNamespace(post=Mock(return_value=Response(batches)))
        with self.assertRaises(NotSupportedException):
            await api.connect()
        self.assertIsNone(api.kids_video_id)

    async def test_disconnected_screen_clears_snapshot(self):
        api = yt_guard.SnapshotLoungeApi("test", logger=Mock())
        api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
        batches = handshake("TVHTML5_FOR_KIDS")
        batches.append([[5, ["loungeScreenDisconnected", {}]]])
        api.session = SimpleNamespace(post=Mock(return_value=Response(batches)))
        with self.assertRaises(NotSupportedException):
            await api.connect()
        self.assertIsNone(api.kids_video_id)

    async def replay(self, steps, included=True, paused=False, video_rule=False, visible=True):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            rules.include_kids(included)
            if video_rule:
                rules.add("videos", "abcdefghijk")
            if paused:
                rules.pause(15)
            guard = yt_guard.Guard(object(), rules)
            api = YtLoungeApi("test", logger=Mock())
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            guard.info = {"blocked0001": {"title": "Minecraft", "channel": "Example Creator", "handle": ""},
                          "abcdefghijk": {"title": "Science", "channel": "Teacher", "handle": ""}}
            iterator, current = iter(steps), [0, None]
            closed, snapshots = [], []
            finished = asyncio.Event()

            async def connect():
                current[:] = next(iterator)
                if current[1] == "end":
                    api._sid, api._gsession, api._last_event_id = "sid", "gsession", 1
                    return True
                api.kids_video_id = current[1]
                raise NotSupportedException("Unsupported client")

            async def subscribe():
                finished.set()
                await asyncio.Future()

            async def close(video, why):
                closed.append((current[0], video, why))
                return True

            sleep = asyncio.sleep

            async def retry_sleep(_):
                snapshots.append(yt_guard.status_snapshot(guard))
                await sleep(0)

            with (patch.object(api, "connect", connect), patch.object(api, "subscribe", subscribe),
                  patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                  patch.object(guard, "close_app", close),
                  patch.object(yt_guard, "youtube_visible", AsyncMock(
                      side_effect=[False, True, False, True] if not visible else None,
                      return_value=visible)),
                  patch.object(yt_guard, "monotonic", lambda: 100 + current[0]),
                  patch.object(yt_guard.asyncio, "sleep", retry_sleep), patch.object(yt_guard.log, "info")):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    await asyncio.wait_for(finished.wait(), 0.5)
                    return closed, snapshots
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_allowed_kids_video_stays_open_beyond_grace(self):
        closed, snapshots = await self.replay([(0, "abcdefghijk"), (60, "abcdefghijk"), (61, "end")])
        self.assertEqual([], closed)
        self.assertEqual("abcdefghijk", snapshots[-1]["video_id"])
        self.assertIn("Science", snapshots[-1]["now_html"])

    async def test_cached_kids_id_from_hidden_app_is_never_enforced(self):
        # The last screen can stay registered in Lounge after the app closes.
        closed, snapshots = await self.replay([(0, "blocked0001"), (60, "blocked0001"),
                                               (61, "end")], visible=False)
        self.assertEqual([], closed)
        self.assertIsNone(snapshots[-1]["video_id"])
        self.assertFalse(snapshots[-1]["kids_detected"])

    async def test_closed_app_waits_for_reopen_before_reconnecting_lounge(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = yt_guard.Guard(object(), yt_guard.Rules(Path(directory) / "rules.json"))
            guard.kids_hidden = True
            guard.kids_detected = True
            guard.video_id = "blocked0001"
            api = YtLoungeApi("test", logger=Mock())
            guard.api = api
            api.auth.screen_id, api.auth.lounge_id_token = "screen", "token"
            resumed = asyncio.Event()
            visibility = AsyncMock(side_effect=[False, False, True])

            async def connect():
                self.assertEqual(3, visibility.await_count)
                api._sid, api._gsession, api._last_event_id = "sid", "gsession", 1
                return True

            async def subscribe():
                resumed.set()
                await asyncio.Future()

            sleep = asyncio.sleep

            async def retry_sleep(_):
                self.assertIsNone(guard.video_id)
                self.assertFalse(guard.kids_detected)
                await sleep(0)

            with (patch.object(yt_guard, "youtube_visible", visibility),
                  patch.object(api, "connect", AsyncMock(side_effect=connect)) as reconnect,
                  patch.object(api, "subscribe", subscribe),
                  patch.object(api, "get_now_playing", AsyncMock(return_value=True)),
                  patch.object(yt_guard.asyncio, "sleep", retry_sleep)):
                task = asyncio.create_task(yt_guard.guard_loop(api, guard))
                try:
                    await asyncio.wait_for(resumed.wait(), 0.5)
                    reconnect.assert_awaited_once()
                    self.assertFalse(guard.kids_hidden)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_blocked_kids_video_closes_after_grace_with_actual_rule_reason(self):
        closed, _ = await self.replay([(0, "blocked0001"), (29, "blocked0001"),
                                       (30, "blocked0001"), (31, "end")])
        self.assertEqual([(30, "blocked0001", "keyword: minecraft")], closed)

    async def test_switching_to_allowed_video_cancels_pending_close(self):
        closed, _ = await self.replay([(0, "blocked0001"), (29, "abcdefghijk"),
                                       (35, "blocked0001"), (64, "blocked0001"), (65, "end")])
        self.assertEqual([], closed)

    async def test_missing_snapshot_never_closes_previous_blocked_video(self):
        closed, snapshots = await self.replay([(0, "blocked0001"), (40, None), (41, "end")])
        self.assertEqual([], closed)
        self.assertIsNone(snapshots[-1]["video_id"])
        self.assertIn("unavailable", snapshots[-1]["status_title"].lower())

    async def test_pause_and_exclusion_allow_matched_kids_video(self):
        for included, paused in [(False, False), (True, True)]:
            closed, snapshots = await self.replay([(0, "blocked0001"), (60, "blocked0001"), (61, "end")],
                                                  included=included, paused=paused)
            self.assertEqual([], closed)
            self.assertNotIn(">Blocked<", snapshots[-1]["recent_html"])

    async def test_specific_video_rule_blocks_even_without_keyword_match(self):
        closed, _ = await self.replay([(0, "abcdefghijk"), (30, "abcdefghijk"), (31, "end")], video_rule=True)
        self.assertEqual([(30, "abcdefghijk", "video")], closed)

    async def test_channel_rule_uses_kids_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            rules.include_kids(True)
            rules.add("channels", "@science")
            guard = yt_guard.Guard(object(), rules)
            guard.info["abcdefghijk"] = {"title": "Science", "channel": "Teacher", "handle": "science"}
            self.assertEqual("channel: science", await guard.check("abcdefghijk", app="kids"))

    async def test_failed_metadata_is_retried_instead_of_cached_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = yt_guard.Rules(Path(directory) / "rules.json")
            rules.include_kids(True)
            guard = yt_guard.Guard(object(), rules)
            blank = {"title": "", "channel": "", "handle": ""}
            info = {"title": "Minecraft", "channel": "Example Creator", "handle": ""}
            with patch.object(yt_guard, "video_info", AsyncMock(side_effect=[blank, info])) as fetch:
                self.assertIsNone(await guard.check("blocked0001", app="kids"))
                self.assertEqual("keyword: minecraft", await guard.check("blocked0001", app="kids"))
                self.assertEqual(2, fetch.await_count)
                self.assertEqual("Minecraft", guard.recent[0]["title"])


if __name__ == "__main__":
    unittest.main()
