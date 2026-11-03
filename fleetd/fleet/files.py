"""The Files tab's work on one machine, standard library only: fleetd runs it as
`python3 -c SCRIPT ACTION BASE64-JSON`, here or over ssh, so stdin stays free for uploads and stdout
carries downloads. JSON actions print one JSON object; read and zip stream raw bytes. A failure exits
1 with the message as stderr's last line."""

import base64
import fcntl
import json
import mimetypes
import os
import re
import shutil
import stat as st
import sys
import time
import zipfile

HOME = os.path.expanduser("~")
LIST_MAX = 5000  # entries shown per folder
CHUNK = 1 << 16
PARTIAL = ".fleet-upload-"  # an unfinished upload, beside where the file will go
PARTIAL_KEEP = 3 * 86400  # seconds an untouched one is kept for the upload to continue


class Problem(Exception):
    pass


def resolve(path):
    """An absolute path from what the page sends: ~ and ~/x are the home folder, relative is under it."""
    path = (path or "~").strip()
    path = os.path.expanduser(path if path.startswith(("/", "~")) else "~/" + path)
    return os.path.normpath(os.path.abspath(path))


def tilde(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def is_text(path):
    """Text if its first 8 KB has no NUL byte and reads as UTF-8 (a character cut at the end is fine)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start >= len(head) - 3


def mime(path):
    kind = mimetypes.guess_type(path)[0]
    if kind is None and path.lower().endswith((".md", ".markdown")):
        kind = "text/markdown"
    return kind or ""


def entry(de):
    try:
        s = de.stat()
        is_dir = st.S_ISDIR(s.st_mode)
        return {"name": de.name, "dir": is_dir, "size": None if is_dir else s.st_size, "mtime": int(s.st_mtime),
                "link": de.is_symlink()}
    except OSError:  # a dangling link, or no permission
        return {"name": de.name, "dir": False, "size": None, "mtime": None, "link": de.is_symlink(), "broken": True}


def list_(a):
    path = resolve(a.get("path"))
    if not os.path.isdir(path):
        raise Problem(f"There's no folder {tilde(path)}." if not os.path.exists(path) else f"{tilde(path)} isn't a folder.")
    try:
        with os.scandir(path) as it:
            entries = [entry(de) for de in it]
    except PermissionError:
        raise Problem(f"No permission to open {tilde(path)}.")
    entries.sort(key=lambda e: (not e["dir"], e["name"].startswith("."), e["name"].lower()))
    return {"path": tilde(path), "abs": path, "home": HOME, "entries": entries[:LIST_MAX],
            "more": max(0, len(entries) - LIST_MAX)}


def stat(a):
    path = resolve(a.get("path"))
    try:
        s = os.stat(path)
    except FileNotFoundError:
        raise Problem(f"There's no {tilde(path)}.")
    except PermissionError:
        raise Problem(f"No permission to read {tilde(path)}.")
    is_dir = st.S_ISDIR(s.st_mode)
    return {"path": tilde(path), "abs": path, "name": os.path.basename(path) or path, "dir": is_dir,
            "size": None if is_dir else s.st_size, "mtime": int(s.st_mtime),
            "mime": "" if is_dir else mime(path), "text": not is_dir and is_text(path)}


def read(a):
    path = resolve(a.get("path"))
    offset, limit = int(a.get("offset") or 0), a.get("limit")
    left = None if limit is None else int(limit)
    out = sys.stdout.buffer
    try:
        f = open(path, "rb")
    except OSError as e:
        raise Problem(f"Can't read {tilde(path)}: {e.strerror}.")
    with f:
        f.seek(offset)
        while left is None or left > 0:
            chunk = f.read(CHUNK if left is None else min(CHUNK, left))
            if not chunk:
                break
            out.write(chunk)
            if left is not None:
                left -= len(chunk)
    out.flush()


def zip_(a):
    """The folder as a zip, written as it's read (zipfile handles an unseekable stdout)."""
    root = resolve(a.get("path"))
    if not os.path.isdir(root):
        raise Problem(f"{tilde(root)} isn't a folder.")
    base = os.path.basename(root) or "root"
    with zipfile.ZipFile(sys.stdout.buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for folder, dirs, names in os.walk(root, onerror=lambda e: None):
            dirs.sort()
            rel = os.path.relpath(folder, root)
            prefix = base if rel == "." else os.path.join(base, rel)
            if not names and not dirs:
                z.writestr(prefix + "/", b"")
            for name in sorted(names):
                full = os.path.join(folder, name)
                if name.startswith(PARTIAL) or not os.path.isfile(full):
                    continue
                try:
                    z.write(full, os.path.join(prefix, name))
                except OSError:
                    pass  # unreadable or vanished: leave it out


def free_name(path):
    """path, or "name (1).ext", "name (2).ext", ... when it's taken."""
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    if stem.endswith(".tar"):
        stem, ext = stem[:-4], ".tar" + ext
    n = 1
    while os.path.lexists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def write(a):
    """Stdin into the file, through a temporary file beside it, so a cut-off upload leaves nothing half
    written. An existing file is replaced only with "overwrite" (the editor); uploads get a free name."""
    target = resolve(a.get("path"))
    folder = os.path.dirname(target)
    if not os.path.isdir(folder):
        raise Problem(f"There's no folder {tilde(folder)}.")
    if os.path.isdir(target):
        raise Problem(f"{tilde(target)} is a folder.")
    if not a.get("overwrite"):
        target = free_name(target)
    if a.get("mtime") and os.path.exists(target) and int(os.stat(target).st_mtime) != int(a["mtime"]):
        raise Problem(f"{tilde(target)} changed since you opened it (an agent may have edited it). "
                      "Copy your text, reload, and edit again.")
    mode = st.S_IMODE(os.stat(target).st_mode) if os.path.exists(target) else None
    tmp = os.path.join(folder, f".{os.path.basename(target)}.fleet-{os.getpid()}")
    size = 0
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = sys.stdin.buffer.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        if a.get("size") is not None and size != int(a["size"]):
            raise Problem(f"The upload was cut off ({size} of {a['size']} bytes); nothing was saved.")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError as e:
        raise Problem(f"Can't write {tilde(target)}: {e.strerror}.")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"path": tilde(target), "size": size}


def upload(a):
    """A big upload, one piece per request, so no request is bigger than the tunnel allows and a dropped
    connection costs one piece. The pieces build up in a hidden file beside the target, named after
    the upload's id. Steps: start (how much is there already), piece (stdin at offset; the last one moves
    the file into place under a free name), cancel."""
    target = resolve(a.get("path"))
    folder = os.path.dirname(target)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", a.get("upload") or ""):
        raise Problem("Bad upload id.")
    if not os.path.isdir(folder):
        raise Problem(f"There's no folder {tilde(folder)}.")
    if os.path.isdir(target):
        raise Problem(f"{tilde(target)} is a folder.")
    tmp = os.path.join(folder, PARTIAL + a["upload"])
    step, total = a.get("step"), int(a.get("total") or 0)
    try:
        if step == "cancel":
            if os.path.exists(tmp):
                os.unlink(tmp)
            return {"have": 0}
        if step == "start":
            return upload_start(folder, tmp, total)
        if step == "piece":
            return upload_piece(target, tmp, int(a.get("offset") or 0), total)
    except OSError as e:
        raise Problem(f"Can't write {tilde(target)}: {e.strerror}.")
    raise Problem("Bad upload step.")


def upload_start(folder, tmp, total):
    with os.scandir(folder) as it:  # uploads given up on
        for de in it:
            if de.name.startswith(PARTIAL) and de.path != tmp and time.time() - de.stat().st_mtime > PARTIAL_KEEP:
                os.unlink(de.path)
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if have > total:
        os.unlink(tmp)
        have = 0
    free = shutil.disk_usage(folder).free
    if free < total - have:
        raise Problem(f"Not enough space: {(total - have) >> 20} MB more is needed and {free >> 20} MB is free.")
    return {"have": have}


def upload_piece(target, tmp, offset, total):
    with os.fdopen(os.open(tmp, os.O_RDWR | os.O_CREAT, 0o666), "r+b") as f:
        for _ in range(150):  # the previous try of this piece may be finishing
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            raise Problem("Another page is uploading this file right now.")
        # It finished while this waited: this handle is the finished file now, not to be touched.
        if not os.path.exists(tmp) or os.stat(tmp).st_ino != os.fstat(f.fileno()).st_ino:
            raise Problem("This upload has already finished.")
        have = os.fstat(f.fileno()).st_size
        if offset > have:  # pieces went missing: say where to continue
            while sys.stdin.buffer.read(CHUNK):
                pass
            return {"have": have}
        f.seek(offset)
        f.truncate()  # a retry: what's past here was an earlier try of this piece
        while True:
            chunk = sys.stdin.buffer.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
        f.flush()
        have = f.tell()
        if have > total:
            os.unlink(tmp)
            raise Problem(f"The upload is bigger than announced ({have} of {total} bytes); nothing was saved.")
        if have < total:
            return {"have": have}
        target = free_name(target)
        os.replace(tmp, target)
    return {"have": have, "done": True, "path": tilde(target), "size": have}


def plain_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise Problem("Names can't be empty or contain /.")
    return name


def mkdir(a):
    path = os.path.join(resolve(a.get("path")), plain_name(a.get("name")))
    try:
        os.mkdir(path)
    except FileExistsError:
        raise Problem(f"{tilde(path)} already exists.")
    except OSError as e:
        raise Problem(f"Can't make {tilde(path)}: {e.strerror}.")
    return {"path": tilde(path)}


def rename(a):
    path = resolve(a.get("path"))
    new = os.path.join(os.path.dirname(path), plain_name(a.get("name")))
    if os.path.lexists(new):
        raise Problem(f"{tilde(new)} already exists.")
    try:
        os.rename(path, new)
    except OSError as e:
        raise Problem(f"Can't rename {tilde(path)}: {e.strerror}.")
    return {"path": tilde(new)}


def delete(a):
    path = resolve(a.get("path"))
    if path in ("/", HOME) or len(path.split("/")) < 3:
        raise Problem(f"Fleet won't delete {tilde(path)}.")
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        raise Problem(f"There's no {tilde(path)}.")
    except OSError as e:
        raise Problem(f"Can't delete {tilde(path)}: {e.strerror}.")
    return {"path": tilde(path), "parent": tilde(os.path.dirname(path))}


ACTIONS = {"list": list_, "stat": stat, "read": read, "zip": zip_, "write": write, "upload": upload, "mkdir": mkdir,
           "rename": rename, "delete": delete}
STREAMS = {"read", "zip"}


if __name__ == "__main__":
    action = sys.argv[1]
    args = json.loads(base64.b64decode(sys.argv[2])) if len(sys.argv) > 2 else {}
    try:
        result = ACTIONS[action](args)
    except (Problem, KeyError, ValueError) as e:
        sys.stderr.write(f"\n{e}\n")
        sys.exit(1)
    if action not in STREAMS:
        print(json.dumps(result))
