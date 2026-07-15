"""Policy enforcement: every adapter ships a complete registry record.

This is the CI teeth behind plan §8: adding a source without a complete
source-registry record fails the build.
"""

import importlib
import pkgutil

import app.adapters as adapters_pkg
from app.adapters.base import Adapter, AdapterManifest


def iter_adapters():
    for mod_info in pkgutil.iter_modules(adapters_pkg.__path__):
        if mod_info.name == "base":
            continue
        mod = importlib.import_module(f"app.adapters.{mod_info.name}")
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, Adapter) and obj is not Adapter:
                yield obj


def test_all_adapters_have_complete_manifests():
    problems = []
    found = 0
    for cls in iter_adapters():
        found += 1
        manifest = getattr(cls, "manifest", None)
        if not isinstance(manifest, AdapterManifest):
            problems.append(f"{cls.__name__}: no manifest")
            continue
        problems += manifest.validate()
    assert not problems, "\n".join(problems)
    # zero adapters is fine at scaffold stage; the gate activates with the first one
