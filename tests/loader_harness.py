#!/usr/bin/env python3
"""
Loader harness: prove Nautobot job discovery works WITHOUT a Nautobot install.

Nautobot's Git-repository job loading has sharp edges that only surface at sync
time (e.g. pkgutil.walk_packages silently skips directories with no
__init__.py — the "No jobs were registered" failure). This harness catches
them locally, pre-push, by running the repo through Nautobot's REAL loader:

  1. copies this repo into <tmp>/gitroot/proxmox — exactly how Nautobot lays
     out a synced Git repository under GIT_ROOT;
  2. stubs the import surface (nautobot.*, django.*, requests) so job modules
     import without Nautobot/Django installed — Job is a real base class and
     register_jobs() records what the jobs register;
  3. downloads Nautobot's actual nautobot/core/utils/module_loading.py (branch
     ltm-2.4, stdlib-only; cached in tests/.cache/, override with the
     NAUTOBOT_MODULE_LOADING env var for offline runs) and drives it the way
     the sync does;
  4. asserts every expected job class registered.

Run:  python3 tests/loader_harness.py
Add a job? Add its class name to EXPECTED_JOBS below.
"""

import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import types
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE_LOADING_URL = (
    "https://raw.githubusercontent.com/nautobot/nautobot/ltm-2.4/"
    "nautobot/core/utils/module_loading.py"
)
CACHE = pathlib.Path(__file__).resolve().parent / ".cache" / "module_loading.py"

EXPECTED_JOBS = {
    "BootstrapNfvSchema",
    "DeployVnfDevice",
    "DecommissionVnfDevice",
    "IngestImage",
    "DiscoverSe350Platform",
    "InstallProxmoxNode",
}

REGISTRY = []


# ---- import-surface stubs ---------------------------------------------------
# Any module under these top-level names is fabricated on demand; attribute
# access yields a permissive placeholder class. Job/register_jobs are real so
# registration is observable.
STUB_ROOTS = ("nautobot", "django", "requests")


def _placeholder(name):
    return type(name, (), {"__init__": lambda self, *a, **k: None})


def _make_stub_module(fullname):
    mod = types.ModuleType(fullname)
    mod.__path__ = []  # behaves as a package so submodule imports resolve here too
    if fullname == "nautobot.apps.jobs":
        mod.Job = type("Job", (), {})
        mod.register_jobs = lambda *classes: REGISTRY.extend(classes)
    cache = {}

    def _getattr(name, _cache=cache, _fullname=fullname):
        if name.startswith("__"):
            raise AttributeError(name)
        if name not in _cache:
            _cache[name] = _placeholder(name)
        return _cache[name]

    mod.__getattr__ = _getattr
    return mod


class _StubFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in STUB_ROOTS:
            return None
        return importlib.util.spec_from_loader(fullname, _StubLoader())


class _StubLoader(importlib.abc.Loader if hasattr(importlib, "abc") else object):
    def create_module(self, spec):
        return _make_stub_module(spec.name)

    def exec_module(self, module):
        pass


def _get_module_loading():
    src = os.environ.get("NAUTOBOT_MODULE_LOADING")
    if src:
        path = pathlib.Path(src)
    else:
        path = CACHE
        if not path.exists():
            print(f"fetching {MODULE_LOADING_URL}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(MODULE_LOADING_URL, timeout=30) as r:
                path.write_bytes(r.read())
    spec = importlib.util.spec_from_file_location("nb_module_loading", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    import importlib.abc  # noqa: F401  (ensure importlib.abc is loaded pre-stub)

    sys.meta_path.insert(0, _StubFinder())
    module_loading = _get_module_loading()

    with tempfile.TemporaryDirectory() as tmp:
        # realpath: the loader's conflict check compares realpath(origin)
        # against this path; macOS's symlinked /var tempdirs would otherwise
        # flag every module as a foreign conflict.
        gitroot = pathlib.Path(os.path.realpath(tmp)) / "gitroot"
        # "proxmox" stands in for the synced repo's slug (any identifier works)
        shutil.copytree(
            REPO,
            gitroot / "proxmox",
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".cache"),
        )
        module_loading.import_modules_privately(str(gitroot), ignore_import_errors=False)

    registered = {cls.__name__ for cls in REGISTRY}
    missing = EXPECTED_JOBS - registered
    extra = registered - EXPECTED_JOBS
    print(f"registered: {sorted(registered)}")
    if missing:
        print(f"FAIL: expected jobs not registered: {sorted(missing)}")
        print("(most common cause: a directory missing __init__.py — "
              "pkgutil.walk_packages skips it silently)")
        return 1
    if extra:
        print(f"note: unlisted jobs registered (add to EXPECTED_JOBS): {sorted(extra)}")
    print("OK: all expected jobs registered via Nautobot's real loader")
    return 0


if __name__ == "__main__":
    sys.exit(main())
