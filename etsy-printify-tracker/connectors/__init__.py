"""Connector framework. Drop a new module in this folder that defines a
Connector subclass and it appears automatically on the Connectors page and in
sync.py (see registry.discover)."""
from abc import ABC, abstractmethod


class SettingField:
    """One credential/field shown on a connector's card."""
    def __init__(self, key, label, kind="text", help=""):
        self.key = key          # settings key it reads/writes
        self.label = label
        self.kind = kind        # 'text' | 'password'
        self.help = help


class Connector(ABC):
    key = "base"
    label = "Base"
    kind = "cost"               # 'cost' (writes sku_costs) | 'sales' (writes orders)
    docs_url = ""
    implemented = True
    settings_fields = []

    def is_configured(self, settings):
        return all(settings.get(f.key) for f in self.settings_fields)

    @abstractmethod
    def test(self, settings):
        """Return {'ok': bool, 'message': str}."""

    @abstractmethod
    def sync(self, settings):
        """Return {'rows_seen', 'rows_new', 'rows_updated', ...}."""
