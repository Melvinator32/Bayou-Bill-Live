"""Auto-discovery: import every module in this package and collect Connector
subclasses, so adding a connector file needs no wiring elsewhere."""
import importlib
import pkgutil

import connectors as _pkg
from connectors import Connector

_SKIP = {"registry"}


def discover():
    found = {}
    for mod_info in pkgutil.iter_modules(_pkg.__path__):
        name = mod_info.name
        if name in _SKIP or name.startswith("_"):
            continue
        module = importlib.import_module(f"connectors.{name}")
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, Connector)
                    and obj is not Connector):
                inst = obj()
                found[inst.key] = inst
    # stable order: sales first, then cost, then by label
    return dict(sorted(found.items(),
                       key=lambda kv: (kv[1].kind != "sales", kv[1].label)))
