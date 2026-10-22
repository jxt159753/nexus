"""Small public plugin context, shared by host execution and isolated tests."""

from pathlib import Path


class PluginContext:
    def __init__(self,workspace,*,testing=False,call=None):
        self.workspace=Path(workspace)
        self.testing=testing
        self._call=call

    def call(self,plugin_id,tool,input):
        if self._call is None: raise RuntimeError('tool bridge unavailable in this execution')
        return self._call(plugin_id,tool,input)
