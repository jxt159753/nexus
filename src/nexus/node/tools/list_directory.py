import json
import os
import stat
from pathlib import Path

from nexus.contracts.task import ListDirectoryArguments
from nexus.contracts.tools import DirectoryEntry, ListDirectoryResult
from nexus.node.config import local_directory


def list_directory(
    arguments: ListDirectoryArguments, root: Path, max_bytes: int,
) -> ListDirectoryResult:
    root = local_directory(root)  # Recheck immediately before each observation.
    entries: list[DirectoryEntry] = []
    truncated = False
    # Reserve the larger false encoding so toggling truncated never exceeds budget.
    used = len(ListDirectoryResult(root_id=arguments.root_id, entries=[], truncated=False)
               .model_dump_json().encode("utf-8"))
    with os.scandir(root) as iterator:
        for item in iterator:
            if len(entries) == arguments.limit:
                truncated = True
                break
            info = item.stat(follow_symlinks=False)
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                kind = "link"
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
            entry = DirectoryEntry(name=item.name, kind=kind)
            size = len(json.dumps(entry.model_dump(), ensure_ascii=False, separators=(",", ":"))
                       .encode("utf-8")) + bool(entries)
            if used + size > max_bytes:
                truncated = True
                break
            entries.append(entry)
            used += size
    return ListDirectoryResult(root_id=arguments.root_id, entries=entries, truncated=truncated)
