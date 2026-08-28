"""Interface translation.

Scope is deliberately GUI chrome only -- labels, buttons, headings, help
text, the chart readout. The `roastmesh` CLI's own output (which streams
into the GUI's Console widgets on the Network/Publish tabs) is left in
English on purpose: the GUI *parses* some of that text (the "ticket: "
prefix that populates the Network tab's ticket field -- see
gui/app.py's _on_serve_output), so translating it would need to happen
inside the CLI itself and be kept in lockstep with that parse, which is a
separate, larger change than this one.

English is the catalog key, not an opaque message id: `t("Search")`
returns "Rechercher" in French and "Search" in English, so an untranslated
or newly-added string degrades to correct English instead of a raw key
like `search.heading`. This also means English needs no catalog file at
all -- it's just the identity lookup.

Catalogs are plain JSON, one file per language, at
`src/roastmesh/gui/locales/<code>.json`, read the same way this project
already reads schema.sql (index/db.py's migrate()) so this survives a
PyInstaller onefile bundle: three separate declarations are required for
that (pyproject.toml's package-data, the PyInstaller spec's `datas`, and
this module's importlib.resources read) -- missing the spec entry is the
classic failure that works from source and breaks silently in the
packaged binary most users actually run.

The language is chosen once, at startup (RoastmeshApp.__init__, before any
tab is built), and is fixed for the process's lifetime -- switching in
Settings takes effect on next launch. This sidesteps rebuilding every
already-constructed widget, and (more importantly) rebuilding the Network
tab specifically would tear down and restart its always-on `node serve`
subprocess and LAN discovery as a side effect of what should be a purely
cosmetic change.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from importlib import resources

DEFAULT_LANGUAGE = "en"

# code -> (native name shown in Settings -- never translated, so a user who
# picks the wrong language can always find their way back -- and a
# plural-form picker: True means "use the plural template" for that count).
# English: 0 is plural ("0 results"). French: 0 and 1 are both singular
# ("0 résultat", "1 résultat"), 2+ is plural.
LANGUAGES: dict[str, tuple[str, Callable[[int], bool]]] = {
    "en": ("English", lambda n: n != 1),
    "fr": ("Français", lambda n: n > 1),
}

_current_language = DEFAULT_LANGUAGE
_catalogs: dict[str, dict[str, str]] = {}


def _load_catalog(code: str) -> dict[str, str]:
    if code == DEFAULT_LANGUAGE:
        return {}
    if code in _catalogs:
        return _catalogs[code]
    catalog: dict[str, str] = {}
    try:
        raw = resources.files("roastmesh.gui").joinpath(f"locales/{code}.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            catalog = {str(k): str(v) for k, v in data.items()}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        # A translation catalog is never load-bearing -- worst case is
        # English text, never a crash. Reported once, not raised, so a
        # missing/corrupt catalog can't take the whole app down with it.
        # `if sys.stderr` is load-bearing on Windows: the GUI runs under
        # pythonw, where sys.stderr is None, and a bare print() would raise
        # AttributeError here -- inverting this handler's whole purpose, since
        # set_language() runs before any tab is built. A corrupt catalog would
        # have taken down startup instead of falling back to English.
        if sys.stderr is not None:
            print(f"roastmesh: could not load the '{code}' translation catalog -- "
                  "falling back to English.", file=sys.stderr)
    _catalogs[code] = catalog
    return catalog


def resolve_language(configured: str | None) -> str:
    """Precedence: $ROASTMESH_LANG env override (matches the existing
    $ROASTMESH_UI_SCALE/$ROASTMESH_LINE_SCALE convention in gui/widgets.py)
    > the configured (persisted) language > the OS locale > English. An
    unrecognized value at any step falls through to the next, rather than
    sticking and silently degrading to raw-English-as-fallback."""
    env = os.environ.get("ROASTMESH_LANG", "").strip().lower()
    if env in LANGUAGES:
        return env

    configured = (configured or "").strip().lower()
    if configured in LANGUAGES:
        return configured

    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        code = value[:2].lower()
        if code in LANGUAGES:
            return code

    return DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    global _current_language
    _current_language = code if code in LANGUAGES else DEFAULT_LANGUAGE
    _load_catalog(_current_language)  # populate/validate the cache eagerly, not on first t()


def current_language() -> str:
    return _current_language


def t(text: str, **kwargs) -> str:
    """Translate `text` (English, used as the lookup key) to the current
    language, then `.format(**kwargs)` it if any were given. Never raises:
    an unknown key returns `text` unchanged, and a translation with a
    missing/misspelled `{placeholder}` falls back to formatting the
    original English rather than crashing on a translator's typo."""
    translated = _load_catalog(_current_language).get(text, text)
    if not kwargs:
        return translated
    try:
        return translated.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text


def tn(n: int, singular: str, plural: str, **kwargs) -> str:
    """Plural-aware translation. `singular`/`plural` are English templates
    (e.g. "{n} result" / "{n} results"); which one is looked up and shown
    depends on the current language's own plural rule for `n`, not
    English's -- French treats 0 as singular, English doesn't.

    `n` is already supplied to the template automatically -- callers must
    not also pass `n=` in kwargs (it's popped here rather than raising, to
    keep the "translation code never crashes the app" guarantee, but the
    caller's value would just be discarded either way)."""
    kwargs.pop("n", None)
    _, is_plural = LANGUAGES.get(_current_language, LANGUAGES[DEFAULT_LANGUAGE])
    template = plural if is_plural(n) else singular
    return t(template, n=n, **kwargs)
