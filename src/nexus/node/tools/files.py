"""Bounded UTF-8 reads, name search, explicit diffs and atomic file replacement."""

import difflib
import fnmatch
import hashlib
import os
import stat
import tempfile

from nexus.contracts.basic import text_digest, write_digest
from nexus.contracts.tools import FileReadResult, FileSearchResult, FilePreviewResult, FileWriteResult
from nexus.node.tools.paths import guarded_path, hold_directory, read_handle, replace_file


def read_file(args, root):
    with guarded_path(root, args.path) as path, read_handle(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        # Whole-file hashes are bounded; large files must be narrowed externally.
        if size > 1024 * 1024:
            raise ValueError("file exceeds 1 MiB")
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("file exceeds 1 MiB")
        raw.decode("utf-8")  # Binary/invalid text is not silently transformed.
        head = raw[:args.max_bytes]
        while True:
            try:
                text = head.decode("utf-8")
                break
            except UnicodeDecodeError:
                head = head[:-1]
        return FileReadResult(root_id=args.root_id, path=args.path, text=text, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), truncated=len(head)<len(raw))


def find_files(args, root):
    paths, scanned, truncated = [], 0, False

    def visit(directory, prefix, depth):
        nonlocal scanned, truncated
        with hold_directory(directory), os.scandir(directory) as entries:
            for item in entries:
                if scanned >= 2000 or len(paths) >= args.limit:
                    truncated = True
                    return
                scanned += 1
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                relative = prefix + item.name
                from nexus.contracts.basic import relative_path
                try:
                    relative_path(relative)
                except ValueError:
                    truncated = True
                    continue
                # Windows DirEntry's cached stat can report st_nlink=0.
                links = os.stat(item.path, follow_symlinks=False).st_nlink if stat.S_ISREG(info.st_mode) else 0
                if stat.S_ISREG(info.st_mode) and links == 1 and fnmatch.fnmatchcase(item.name.casefold(), args.pattern.casefold()):
                    paths.append(relative)
                if stat.S_ISDIR(info.st_mode):
                    if depth < args.max_depth:
                        visit(directory / item.name, relative + "/", depth+1)
                    else:
                        truncated = True
                if scanned >= 2000 or len(paths) >= args.limit:
                    truncated = True
                    return

    # Also hold the configured root's ancestors throughout traversal.
    with guarded_path(root, "__nexus_search_guard__"):
        visit(root, "", 0)
    return FileSearchResult(root_id=args.root_id, paths=paths, scanned=scanned, truncated=truncated)


def previous(path):
    try:
        with read_handle(path) as stream:
            raw = stream.read(12001)
    except FileNotFoundError:
        return None, ""
    if len(raw) > 12000:
        raise ValueError("existing file exceeds write limit")
    return hashlib.sha256(raw).hexdigest(), raw.decode("utf-8")


def preview_write(args, root):
    with guarded_path(root, args.path) as path:
        old_hash, old_text = previous(path)
        diff = "".join(difflib.unified_diff(old_text.splitlines(keepends=True), args.content.splitlines(keepends=True), fromfile="before/"+args.path, tofile="after/"+args.path))
        digest = text_digest(args.content)
        return FilePreviewResult(root_id=args.root_id, path=args.path, previous_sha256=old_hash, content_sha256=digest, preview_digest=write_digest(args.root_id,args.path,old_hash,digest), diff=diff[:16000], truncated=len(diff)>16000)


def write_file(args, root):
    digest = text_digest(args.content)
    if args.preview_digest != write_digest(args.root_id,args.path,args.expected_sha256,digest):
        raise PermissionError("preview binding mismatch")
    with guarded_path(root, args.path) as path:
        if previous(path)[0] != args.expected_sha256:
            raise PermissionError("file changed since preview")
        descriptor, temporary = tempfile.mkstemp(prefix=".nexus-write-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(args.content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if previous(path)[0] != args.expected_sha256:
                raise PermissionError("file changed during write preparation")
            if args.expected_sha256 is None:
                # A hard link creates the destination atomically without clobbering
                # a concurrently-created file; the private temp is then unlinked.
                os.link(temporary, path, follow_symlinks=False)
                os.unlink(temporary)
            else:
                replace_file(temporary, path)
            with read_handle(path) as stream:
                if hashlib.sha256(stream.read(12001)).hexdigest() != digest:
                    raise ValueError("write verification failed")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return FileWriteResult(root_id=args.root_id,path=args.path,sha256=digest,bytes_written=len(args.content.encode()))
