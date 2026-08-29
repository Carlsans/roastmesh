# Kept in sync with pyproject.toml's version at each release -- read by
# index/ingest.py's version-gated refresh to know when the local index
# was last brought up to date (see refresh_known_sources).
#
# Nothing is imported here, deliberately. This module briefly imported asyncio
# to set the Windows event loop policy, and that broke the packaged GUI
# outright: in a frozen build `import asyncio` pulls in asyncio.windows_events,
# which needs the `_overlapped` C extension -- and PyInstaller had no reason to
# bundle it, because the GUI process never uses asyncio at all (it shells out to
# the CLI). roastmesh-gui.exe then died at startup with
# "ModuleNotFoundError: No module named '_overlapped'".
#
# The policy now lives in roastmesh.asyncio_policy, applied by the modules that
# actually run an event loop. Keep this file import-free: anything added here is
# paid for by every entry point, including ones that have no use for it.
__version__ = "0.6.1"
