# Third-party notices

roastmesh itself is licensed under the PolyForm Noncommercial License 1.0.0 (see
`LICENSE`). It ships with, and builds on, third-party software under the licenses
below. Those licenses are all permissive and impose no restriction on roastmesh's
own non-commercial terms; they only require that their notices travel with any
distribution — which this file satisfies.

## Bundled Python dependencies

- **iroh** (Python bindings, `n0-computer/iroh-ffi`) — Apache-2.0 OR MIT.
  QUIC/relay transport and endpoint identity.
- **cryptography** (`pyca/cryptography`) — Apache-2.0 OR BSD-3-Clause.
  Ed25519 signing/verification.
- **click** (`pallets/click`) — BSD-3-Clause. Command-line interface.
- **Sun Valley ttk theme** (`rdbende/Sun-Valley-ttk-theme`, via `sv-ttk`) — MIT.
  GUI theme (light/dark chrome).

## Bundled by the binary build (PyInstaller)

The prebuilt binaries are produced with **PyInstaller**, whose bootloader is
GPLv2 **with the well-known runtime exception** that explicitly permits bundling
and distributing an application under any license of the application author's
choosing. Distributing roastmesh's binaries under the PolyForm Noncommercial
License is therefore compatible with PyInstaller's terms.

The binaries also embed **Tcl/Tk** (BSD-style "Tcl/Tk License") for the GUI and
the standard **Python** runtime (PSF License) — both permissive.

## Reference implementations (algorithms only, no copied source)

`src/roastmesh/dht.py` is an original Python implementation of the BitTorrent
Mainline DHT (BEP 5) and its security extension (BEP 42). It ports *mechanics and
numeric parameters* — routing-table and search behavior, rate-limit constants —
studied from Juliusz Chroboczek's `dht.c` (a permissively licensed C
implementation, which Transmission merely bundles) and from libtorrent
(BSD-3-Clause). Algorithms and numeric constants are not copyrightable, and no
verbatim third-party source is included; the file is a clean re-implementation.
No GPL-licensed source is copied into roastmesh.
