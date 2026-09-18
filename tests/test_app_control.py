import asyncio
import unittest
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

import yt_guard


class RoutedSession:
    def __init__(self, session, server):
        self.session, self.server = session, server

    def get(self, url, **kwargs):
        return self.session.get(self.server.make_url(urlsplit(url).path), **kwargs)

    def delete(self, url, **kwargs):
        return self.session.delete(self.server.make_url(urlsplit(url).path), **kwargs)


class AppControlTests(unittest.IsolatedAsyncioTestCase):
    async def close_with_native_api(self, name="YouTube", stays_visible=False, standalone_kids=False,
                                    confirmation_times_out=False, dial_status=503, native_status=200):
        closed = []
        active_id = "3201611010983" if standalone_kids else "111299001912"
        confirmation_attempts = 0

        async def dial(_):
            return web.Response(status=dial_status)

        async def native(request):
            nonlocal confirmation_attempts
            if native_status != 200:
                return web.Response(status=native_status)
            app_id = request.match_info["app_id"]
            if app_id != active_id:
                return web.Response(status=404)
            if request.method == "DELETE":
                closed.append(app_id)
                return web.json_response({})
            if closed:
                confirmation_attempts += 1
                if confirmation_times_out and confirmation_attempts == 1:
                    await asyncio.sleep(2.1)  # The TV briefly stops answering during shutdown.
            return web.json_response({"id": app_id, "name": "YouTube Kids" if standalone_kids else name,
                                      "running": stays_visible or not closed,
                                      "visible": stays_visible or not closed})

        app = web.Application()
        app.router.add_delete("/ws/app/YouTube/run", dial)
        app.router.add_route("*", "/api/v2/applications/{app_id}", native)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            ok = await yt_guard.close_youtube(RoutedSession(session, server), "192.0.2.1", "YouTube Kids", "included")
        return ok, closed

    async def test_native_api_closes_kids_inside_regular_youtube_when_dial_is_down(self):
        ok, closed = await self.close_with_native_api()
        self.assertTrue(ok, "Kids remains open when the DIAL launcher is unavailable")
        self.assertEqual(["111299001912"], closed)

    async def test_native_api_closes_standalone_kids_app(self):
        ok, closed = await self.close_with_native_api(standalone_kids=True)
        self.assertTrue(ok)
        self.assertEqual(["3201611010983"], closed)

    async def test_dial_success_that_leaves_app_visible_uses_native_close(self):
        ok, closed = await self.close_with_native_api(dial_status=200)
        self.assertTrue(ok)
        self.assertEqual(["111299001912"], closed)

    async def test_dial_acknowledgement_without_visibility_does_not_claim_success(self):
        ok, closed = await self.close_with_native_api(dial_status=200, native_status=503)
        self.assertFalse(ok, "an unavailable status endpoint cannot confirm that YouTube closed")
        self.assertEqual([], closed)

    async def test_visibility_distinguishes_hidden_visible_and_unknown_apps(self):
        for primary, kids, expected in [
            ({"name": "YouTube", "visible": False}, None, False),
            ({"name": "YouTube", "visible": False}, {"name": "YouTube Kids", "visible": True}, True),
            ({"name": "Other app", "visible": True}, None, None),
            (None, None, None),
        ]:
            async def native(request):
                state = primary if request.match_info["app_id"] == "111299001912" else kids
                return web.json_response(state) if state is not None else web.Response(status=404)

            app = web.Application()
            app.router.add_get("/api/v2/applications/{app_id}", native)
            async with TestServer(app) as server, aiohttp.ClientSession() as session:
                result = await yt_guard.youtube_visible(RoutedSession(session, server), "192.0.2.1")
                self.assertIs(result, expected)

    async def test_successful_delete_does_not_claim_blocked_when_app_stays_visible(self):
        ok, closed = await self.close_with_native_api(stays_visible=True)
        self.assertFalse(ok)
        self.assertEqual(["111299001912"], closed)

    async def test_native_api_does_not_close_an_unrelated_app(self):
        ok, closed = await self.close_with_native_api(name="Another app")
        self.assertFalse(ok)
        self.assertEqual([], closed)

    async def test_confirmation_timeout_retries_before_reporting_close_failed(self):
        ok, closed = await self.close_with_native_api(confirmation_times_out=True)
        self.assertTrue(ok, "the app closed, but the first confirmation timeout was reported as failure")
        self.assertEqual(["111299001912"], closed)


if __name__ == "__main__":
    unittest.main()
