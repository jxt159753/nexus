"""A tested, approved Python plugin runs in an owned current-user process."""

import importlib.metadata
from nexus.plugins.workflow import program


def execute(runtime,release,name,value,context,deadline,cancel):
    if release is None: raise PermissionError('plugin release unavailable')
    for package,version in release.manifest.dependencies.items():
        if importlib.metadata.version(package)!=version: raise PermissionError('plugin dependency version differs')
    answer=program(runtime,release.source,value,context,deadline,cancel,tool=name)
    return answer['result']
