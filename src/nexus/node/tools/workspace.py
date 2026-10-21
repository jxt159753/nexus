"""Bounded line reads, path globs, literal grep and compare-and-swap edits."""

import hashlib
import os
import stat

from nexus.contracts.basic import FilePreviewArguments, FileWriteArguments, glob_matches, relative_path
from nexus.contracts.tools import (
    FileLinesResult, TextLine, FileGlobResult, FileGrepResult, GrepMatch,
    FileEditPreviewResult, FileEditResult,
)
from nexus.node.tools.files import previous, preview_write, write_file
from nexus.node.tools.paths import guarded_path, hold_directory, read_handle


def clip(text, limit):
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def read_lines(args, root):
    with guarded_path(root, args.path) as path, read_handle(path) as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("file exceeds 1 MiB")
    text = raw.decode("utf-8")
    if "\0" in text:
        raise ValueError("binary file")
    lines = text.splitlines()
    if args.offset > max(1, len(lines)):
        raise ValueError("offset exceeds line count")
    output, remaining = [], args.max_bytes
    for index in range(args.offset-1, min(len(lines), args.offset-1+args.limit)):
        if remaining == 0:
            break
        value = clip(lines[index], min(2048, remaining))
        # Do not consume an unread line when the remaining bytes cannot fit a character.
        if not value and lines[index] and output:
            break
        output.append(TextLine(number=index+1, text=value, truncated=value != lines[index]))
        remaining -= len(value.encode("utf-8"))
    end = output[-1].number if output else args.offset-1
    next_offset = end+1 if end < len(lines) else None
    return FileLinesResult(root_id=args.root_id, path=args.path, offset=args.offset,
        lines=output, total_lines=len(lines), next_offset=next_offset,
        sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
        truncated=next_offset is not None or any(line.truncated for line in output))


def search(args, root, *, content=False):
    paths, matches = [], []
    scanned = skipped = bytes_scanned = output_bytes = 0
    truncated = False
    excluded = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}

    def full():
        return scanned >= 2000 or len(matches if content else paths) >= args.limit or output_bytes >= 16000

    def visit(directory, prefix, depth):
        nonlocal scanned, skipped, bytes_scanned, output_bytes, truncated
        with hold_directory(directory), os.scandir(directory) as entries:
            for item in entries:
                if full():
                    truncated = True
                    return
                scanned += 1
                relative = prefix+item.name
                try:
                    relative_path(relative)
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        raise PermissionError("link")
                    if stat.S_ISDIR(info.st_mode):
                        if item.name.casefold() in excluded:
                            continue
                        if depth < args.max_depth:
                            visit(directory/item.name, relative+"/", depth+1)
                        else:
                            truncated = True
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise PermissionError("special file")
                    local = relative if args.path == "." else relative[len(args.path)+1:]
                    if not glob_matches(local, args.include if content else args.pattern):
                        continue
                    # Opening every returned file also checks hard links and replacement races.
                    with read_handle(directory/item.name) as stream:
                        if not content:
                            paths.append(relative)
                            output_bytes += len(relative.encode())
                            continue
                        if os.fstat(stream.fileno()).st_size > min(131072,4194304-bytes_scanned):
                            raise ValueError("grep byte limit")
                        raw = stream.read(min(131073,4194304-bytes_scanned))
                        bytes_scanned += len(raw)
                        if len(raw) > 131072:
                            raise ValueError("grep file limit")
                        text = raw.decode("utf-8")
                        if "\0" in text:
                            raise ValueError("binary file")
                    needle = args.pattern.casefold() if args.ignore_case else args.pattern
                    for number,line in enumerate(text.splitlines(),1):
                        value = line.casefold() if args.ignore_case else line
                        if needle not in value:
                            continue
                        if full():
                            truncated = True
                            return
                        preview = clip(line, 1024)
                        matches.append(GrepMatch(path=relative,number=number,text=preview,truncated=preview!=line))
                        output_bytes += len(preview.encode())+len(relative.encode())+64
                except (OSError, ValueError, PermissionError):
                    skipped += 1
                    truncated = True

    base = root if args.path == "." else root.joinpath(*args.path.split("/"))
    sentinel = "__nexus_search_guard__" if args.path == "." else args.path+"/__nexus_search_guard__"
    with guarded_path(root,sentinel):
        visit(base, "" if args.path == "." else args.path+"/", 0)
    if content:
        return FileGrepResult(root_id=args.root_id,matches=matches,scanned=scanned,
            skipped=skipped,bytes_scanned=bytes_scanned,truncated=truncated)
    return FileGlobResult(root_id=args.root_id,base_path=args.path,paths=paths,scanned=scanned,truncated=truncated)


def glob_files(args, root):
    return search(args,root)


def grep_files(args, root):
    return search(args,root,content=True)


def replacement(args, root):
    with guarded_path(root,args.path) as path:
        old_hash, text = previous(path)
    if old_hash != args.expected_sha256:
        raise PermissionError("file changed since read")
    count = text.count(args.old_string)
    if count == 0 or not args.replace_all and count != 1:
        raise ValueError("edit requires a unique match or explicit replace_all")
    result = text.replace(args.old_string,args.new_string)
    if len(result.encode()) > 12000:
        raise ValueError("edited file exceeds 12000 bytes")
    return result,count


def preview_edit(args, root):
    content,count = replacement(args,root)
    result = preview_write(FilePreviewArguments(root_id=args.root_id,path=args.path,content=content),root)
    if result.previous_sha256 != args.expected_sha256:
        raise PermissionError("file changed during preview")
    return FileEditPreviewResult(**result.model_dump(),replacements=count,bytes_written=len(content.encode()))


def edit_file(args, root):
    content,count = replacement(args,root)
    if hashlib.sha256(content.encode()).hexdigest() != args.content_sha256 or len(content.encode()) != args.bytes_written:
        raise PermissionError("edit preview content mismatch")
    fields = {key:getattr(args,key) for key in ("root_id","path","expected_sha256","preview_task_id","preview_digest")}
    result = write_file(FileWriteArguments(**fields,content=content),root)
    return FileEditResult(**result.model_dump(),replacements=count)
