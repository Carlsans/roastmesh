"""Self-update: check GitHub for a newer release and, where the installation
supports it, download that release and swap this machine's binaries in place.

Kept out of the GUI so it is testable and reusable from the CLI
(`roastmesh update`) -- which is also how the headless Pi/Windows seed nodes can
update themselves. Standard library only (no new dependency), and the *check*
path never raises: a failed or offline check must be silent, never fatal, so it
can run on every GUI launch without risk.

What "auto-update" means per platform
-------------------------------------
- **Linux binary** (installed by install.sh into ~/.local/bin, or any writable
  dir holding the two frozen binaries): download `roastmesh{suffix}` and
  `roastmesh-gui{suffix}`, verify the CLI one runs and reports the expected new
  version, then `os.replace` both over the installed paths. Replacing a running
  executable is safe on Linux -- the running process keeps its open inode; the
  new file simply takes the path for the next launch -- so the GUI can then
  relaunch itself onto the new build.
- **Windows installer** (installed by the NSIS setup into
  %LOCALAPPDATA%\\Programs\\roastmesh): the files are locked while running, so we
  cannot swap them in place. Instead download the installer and spawn a detached
  helper that waits for the app to exit, runs the installer silently, and
  reopens roastmesh. The GUI closes itself so the files unlock.
- **Anything else** (running from source, a portable-zip build, an arch with no
  prebuilt binary): unsupported -- the caller points the user at the releases
  page.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import roastmesh

REPO = "Carlsans/roastmesh"  # the same slug install.sh uses
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_DOWNLOAD = f"https://github.com/{REPO}/releases/latest/download"

_CHECK_TIMEOUT = 6.0      # a slow network must not stall a launch-time check
_DOWNLOAD_TIMEOUT = 120.0  # a whole binary/installer, not a tiny JSON blob

Progress = Callable[[str], None]


@dataclass(frozen=True)
class UpdateInfo:
    latest_version: str
    page_url: str
    is_newer: bool


class UpdateError(Exception):
    """A self-update could not be completed. Carries the releases page URL so
    the caller can send the user there to download it by hand instead."""

    def __init__(self, message: str, page_url: str = RELEASES_PAGE) -> None:
        super().__init__(message)
        self.page_url = page_url


# -- version comparison -----------------------------------------------------

def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse "X.Y.Z" (tolerating a leading 'v' and trailing non-numerics like
    "-rc1") into a comparable tuple. Anything unparseable becomes (0,), which
    compares lowest -- so junk never looks "newer" than a real version."""
    v = (v or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in v.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


# -- HTTPS --------------------------------------------------------------------

# System CA bundle locations, most-common first. Debian/Ubuntu/Arch, then
# Fedora/RHEL, then Alpine/BSD/macOS.
_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that actually finds a CA bundle inside a frozen
    build.

    A PyInstaller binary ships its own OpenSSL whose compiled-in cert path
    (OPENSSLDIR) points at the *build container*, not the user's machine -- so
    ssl.create_default_context()'s load_default_certs() finds nothing and every
    HTTPS request fails with CERTIFICATE_VERIFY_FAILED (confirmed: the update
    check worked only with SSL_CERT_FILE set). On Linux, add the system bundle
    explicitly. Windows and macOS load the OS trust store here already, so these
    Unix paths simply don't exist and are skipped."""
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        for path in _CA_BUNDLES:
            try:
                if os.path.exists(path):
                    ctx.load_verify_locations(path)
                    break
            except OSError:
                continue
    return ctx


# -- the check --------------------------------------------------------------

def check_latest(current: str | None = None, timeout: float = _CHECK_TIMEOUT) -> UpdateInfo | None:
    """Ask GitHub for the latest release. Returns an UpdateInfo, or None on any
    failure (offline, rate-limited, malformed) -- never raises."""
    current = current or roastmesh.__version__
    try:
        req = urllib.request.Request(
            LATEST_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"roastmesh/{roastmesh.__version__}",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- a failed check must be silent, never fatal
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    latest = tag.lstrip("vV")
    page = str(data.get("html_url") or RELEASES_PAGE)
    return UpdateInfo(latest_version=latest, page_url=page, is_newer=_is_newer(latest, current))


# -- install-kind detection -------------------------------------------------

def _install_dir() -> Path | None:
    """Directory holding the running frozen binaries, or None if not frozen.
    Under a PyInstaller build sys.executable IS the binary (same fact
    gui/runner.roastmesh_argv relies on)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def _same_path(a: Path, b: Path) -> bool:
    na = os.path.normcase(os.path.normpath(str(a)))
    nb = os.path.normcase(os.path.normpath(str(b)))
    return na == nb


def asset_suffix() -> str | None:
    """Release-asset arch suffix for this machine ("" for x86_64, "-aarch64"),
    or None for an arch with no prebuilt binary. Mirrors install.sh's mapping."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return ""
    if machine in ("aarch64", "arm64"):
        return "-aarch64"
    return None


def installation_kind() -> str:
    """One of "linux-binary", "windows-installer", or "unsupported"."""
    install_dir = _install_dir()
    if install_dir is None:
        return "unsupported"  # running from source / pip / a dev checkout

    if sys.platform == "win32":
        # Only the NSIS install can be updated in place (by re-running the
        # installer). A portable-zip build unpacked somewhere has no installer
        # and its files are locked while running -> unsupported, use the page.
        local = os.environ.get("LOCALAPPDATA", "")
        expected = Path(local) / "Programs" / "roastmesh" if local else None
        if expected is not None and _same_path(install_dir, expected):
            return "windows-installer"
        return "unsupported"

    # Linux: we can only swap binaries we can actually write, and only for an
    # arch we publish a build for.
    if asset_suffix() is None:
        return "unsupported"
    if not os.access(install_dir, os.W_OK):
        return "unsupported"
    for name in ("roastmesh", "roastmesh-gui"):
        p = install_dir / name
        if not (p.exists() and os.access(p, os.W_OK)):
            return "unsupported"
    return "linux-binary"


def is_supported() -> bool:
    return installation_kind() != "unsupported"


# -- performing the update --------------------------------------------------

def _download(url: str, dest: Path, progress: Progress) -> None:
    progress(f"downloading {url.rsplit('/', 1)[-1]} ...")
    req = urllib.request.Request(url, headers={"User-Agent": f"roastmesh/{roastmesh.__version__}"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT, context=_ssl_context()) as resp, \
            open(dest, "wb") as f:  # noqa: S310
        shutil.copyfileobj(resp, f)


def _binary_version(binary: Path) -> str | None:
    """The version a freshly downloaded binary reports, or None if it won't run.
    Used as an integrity gate before swapping: a truncated or wrong-arch
    download must never replace a working binary."""
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    text = f"{out.stdout} {out.stderr}"  # "roastmesh, version 0.6.18"
    for token in text.replace(",", " ").split():
        if token[:1].isdigit() and "." in token:
            return token
    return None


def perform_update(progress: Progress = print, wait_pid: int | None = None) -> None:
    """Update this installation to the latest release. Raises UpdateError on any
    failure (with the releases page URL attached). `wait_pid` is the Windows-only
    PID the detached installer helper should wait to exit before installing."""
    kind = installation_kind()
    if kind == "unsupported":
        raise UpdateError("auto-update is not supported for this installation")
    info = check_latest()
    target = info.latest_version if info else None
    if kind == "linux-binary":
        _update_linux(_install_dir(), target, progress)
    elif kind == "windows-installer":
        _update_windows(progress, wait_pid)


def _update_linux(install_dir: Path, target: str | None, progress: Progress) -> None:
    suffix = asset_suffix() or ""
    # Staging dir lives *inside* install_dir so os.replace() stays on one
    # filesystem and is therefore atomic.
    staging = Path(tempfile.mkdtemp(prefix=".roastmesh-update-", dir=install_dir))
    try:
        swaps: list[tuple[Path, Path]] = []
        for name in ("roastmesh", "roastmesh-gui"):
            tmp = staging / name
            _download(f"{_DOWNLOAD}/{name}{suffix}", tmp, progress)
            os.chmod(tmp, 0o755)
            swaps.append((tmp, install_dir / name))

        progress("verifying download ...")
        got = _binary_version(staging / "roastmesh")
        if got is None:
            raise UpdateError("the downloaded binary did not run -- update aborted")
        if target and got != target:
            raise UpdateError(f"downloaded version {got} did not match expected {target} -- aborted")

        for tmp, dest in swaps:
            os.replace(tmp, dest)
        progress(f"updated to {got}. restart roastmesh to run it.")
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any I/O failure -> a clean UpdateError
        raise UpdateError(f"update failed: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _update_windows(progress: Progress, wait_pid: int | None) -> None:
    try:
        tmp = Path(tempfile.gettempdir())
        installer = tmp / "roastmesh-setup-x86_64.exe"
        _download(f"{_DOWNLOAD}/roastmesh-setup-x86_64.exe", installer, progress)
        gui_exe = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "roastmesh" / "roastmesh-gui.exe"

        # A detached .cmd that outlives both this process and the GUI: it waits
        # for the app to exit (so the installer can replace locked files), runs
        # the installer silently, then reopens roastmesh. Detached + its own
        # process group so it survives the app closing.
        wait_block = ""
        if wait_pid:
            wait_block = (
                ":wait\r\n"
                f'tasklist /FI "PID eq {wait_pid}" | find "{wait_pid}" >nul '
                "&& ( timeout /t 1 /nobreak >nul & goto wait )\r\n"
            )
        helper = tmp / "roastmesh-update.cmd"
        helper.write_text(
            "@echo off\r\n"
            + wait_block
            + f'"{installer}" /S\r\n'
            "timeout /t 3 /nobreak >nul\r\n"
            f'start "" "{gui_exe}"\r\n'
            'del "%~f0"\r\n',
            encoding="ascii",
        )
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(["cmd", "/c", str(helper)], creationflags=flags, close_fds=True, cwd=str(tmp))
        progress("installer downloaded; roastmesh will close, update, and reopen.")
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(f"update failed: {exc}") from exc
