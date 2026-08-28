"""PyInstaller entry script for the `roastmesh` CLI binary.

PyInstaller's Analysis wants a script to run, not a module:function
reference the way pyproject.toml's [project.scripts] does -- this is the
thinnest possible wrapper around that same entry point.
"""
from roastmesh.cli import main

if __name__ == "__main__":
    main()
