"""Shared pytest setup.

The Windows event loop policy has to be installed before pytest-asyncio
creates its first loop, the same way roastmesh.cli installs it before its first
asyncio.run(). Without it the DHT tests fail on Windows for the reason
documented in roastmesh.asyncio_policy -- which is where this was first caught.

Every GUI test also constructs a real RoastmeshApp, which auto-starts a real
`node serve`. Left alone that node beacons on the real LAN port and announces
itself on the real public BitTorrent DHT, with a throwaway identity that
disappears when the test ends -- so running the suite quietly filled this
machine's peers.json with hundreds of dead peers (606 of the 876 there when
this was found) and handed the same junk to every other user via the DHT.
ROASTMESH_DISCOVERY_OFFLINE stops that at the source, for the whole session
and every subprocess it spawns. See net._discovery_is_offline.
"""
import os

from roastmesh import asyncio_policy

os.environ.setdefault("ROASTMESH_DISCOVERY_OFFLINE", "1")

asyncio_policy.apply()
