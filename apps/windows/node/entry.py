import os
import sys

# Frozen Python ignores PYTHONIOENCODING. Worker pipes use the same UTF-8 JSON
# contract as the source runtime, regardless of the Windows system code page.
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

# PyInstaller does not implement `python -m`; the frozen Node re-enters this
# executable with --worker for each bounded tool execution.
if "--worker" in sys.argv[1:]:
    from nexus.node.worker import main as worker_main

    raise SystemExit(worker_main())

for flag,module in {
    '--plugin-process':'nexus.plugins.process_worker',
    '--plugin-program':'nexus.plugins.program_worker',
    '--plugin-files':'nexus.plugins.files',
    '--plugin-protocol':'nexus.plugins.protocols',
    '--plugin-browser':'nexus.plugins.browser',
}.items():
    if flag in sys.argv[1:]:
        from importlib import import_module
        raise SystemExit(import_module(module).main())

from nexus.node.__main__ import main

if __name__ == "__main__":
    main()
