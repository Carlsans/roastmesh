"""Shared pytest setup.

The Windows event loop policy has to be installed before pytest-asyncio
creates its first loop, the same way roastmesh.cli installs it before its first
asyncio.run(). Without it the DHT tests fail on Windows for the reason
documented in roastmesh.asyncio_policy -- which is where this was first caught.
"""
from roastmesh import asyncio_policy

asyncio_policy.apply()
