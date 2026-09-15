import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import aiohttp

import yt_guard


SAMSUNG_DIAL_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dialreceiver:1</deviceType>
    <manufacturer>Samsung Electronics</manufacturer>
  </device>
</root>
"""


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def text(self):
        return self.body


class FakeSession:
    def __init__(self, descriptions: dict[str, str]):
        self.descriptions = descriptions
        self.requested = []

    def get(self, url, **_):
        ip = urlsplit(url).hostname
        self.requested.append(ip)
        if ip not in self.descriptions:
            raise aiohttp.ClientConnectionError(ip)
        return FakeResponse(self.descriptions[ip])


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_the_only_samsung_dial_receiver(self):
        session = FakeSession({"192.0.2.2": SAMSUNG_DIAL_XML})
        screen = SimpleNamespace(screen_id="screen-id", screen_name="YouTube on TV")

        with patch.object(yt_guard, "get_screen_id_from_dial", AsyncMock(return_value=screen)):
            found = await yt_guard.discover_tv_ip(session, "192.0.2.99", "192.0.2.0/30")

        self.assertEqual("192.0.2.2", found)
        self.assertCountEqual(["192.0.2.1", "192.0.2.2"], session.requested)

    async def test_refuses_an_ambiguous_network(self):
        session = FakeSession({
            "192.0.2.1": SAMSUNG_DIAL_XML,
            "192.0.2.2": SAMSUNG_DIAL_XML,
        })
        screen = SimpleNamespace(screen_id="screen-id", screen_name="YouTube on TV")

        with patch.object(yt_guard, "get_screen_id_from_dial", AsyncMock(return_value=screen)):
            found = await yt_guard.discover_tv_ip(session, "192.0.2.99", "192.0.2.0/30")

        self.assertIsNone(found)

    async def test_refuses_to_scan_more_than_one_class_c(self):
        session = FakeSession({})

        found = await yt_guard.discover_tv_ip(session, "192.0.2.99", "10.0.0.0/16")

        self.assertIsNone(found)
        self.assertEqual([], session.requested)

    async def test_failed_pairing_discovers_and_retries_the_new_address(self):
        api = object()
        session = object()
        pair = AsyncMock(side_effect=lambda _, ip: ip == "192.0.2.54")
        discover = AsyncMock(return_value="192.0.2.54")

        with (
            patch.object(yt_guard, "pair_api", pair),
            patch.object(yt_guard, "discover_tv_ip", discover),
            patch.object(yt_guard.time, "monotonic", return_value=100.0),
        ):
            paired, ip, last_discovery = await yt_guard.pair_with_discovery(
                api, session, "192.0.2.66", -60.0
            )

        self.assertTrue(paired)
        self.assertEqual("192.0.2.54", ip)
        self.assertEqual(100.0, last_discovery)
        self.assertEqual(
            [unittest.mock.call(api, "192.0.2.66"), unittest.mock.call(api, "192.0.2.54")],
            pair.await_args_list,
        )

    async def test_discovery_is_throttled_after_failed_pairing(self):
        pair = AsyncMock(return_value=False)
        discover = AsyncMock()

        with (
            patch.object(yt_guard, "pair_api", pair),
            patch.object(yt_guard, "discover_tv_ip", discover),
            patch.object(yt_guard.time, "monotonic", return_value=100.0),
        ):
            paired, ip, last_discovery = await yt_guard.pair_with_discovery(
                object(), object(), "192.0.2.66", 90.0
            )

        self.assertFalse(paired)
        self.assertEqual("192.0.2.66", ip)
        self.assertEqual(90.0, last_discovery)
        discover.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
