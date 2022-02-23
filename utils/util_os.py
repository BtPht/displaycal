# -*- coding: utf-8 -*-

import ctypes
import errno
import fnmatch
import glob
import importlib
import os
import re
import shutil
import struct
import subprocess as sp
import sys
import tempfile
import time


import grp
import pwd
import fcntl

try:
    reloaded
except NameError:
    # First import. All fine
    reloaded = 0
else:
    # Module is being reloaded. NOT recommended.
    reloaded += 1
    import warnings

    warnings.warn(
        "Module %s is being reloaded. This is NOT recommended." % __name__,
        RuntimeWarning,
    )
    warnings.warn("Implicitly reloading builtins", RuntimeWarning)
    if sys.platform == "win32":
        importlib.reload(__builtin__)
    warnings.warn("Implicitly reloading os", RuntimeWarning)
    importlib.reload(os)
    warnings.warn("Implicitly reloading os.path", RuntimeWarning)
    importlib.reload(os.path)
    if sys.platform == "win32":
        warnings.warn("Implicitly reloading win32api", RuntimeWarning)
        importlib.reload(win32api)


# Cache used for safe_shell_filter() function
_cache = {}
_MAXCACHE = 100

FILE_ATTRIBUTE_REPARSE_POINT = 1024
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # Junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C

from encoding import get_encodings

fs_enc = get_encodings()[1]

_listdir = os.listdir


def listdir(path):
    paths = _listdir(path)
    if isinstance(path, str):
        # Undecodable filenames will still be string objects. Ignore them.
        paths = [path for path in paths if isinstance(path, str)]
    return paths


os.listdir = listdir


def quote_args(args):
    """Quote commandline arguments where needed. It quotes all arguments that
    contain spaces or any of the characters ^!$%&()[]{}=;'+,`~"""
    args_out = []
    for arg in args:
        if re.search("[\^!$%&()[\]{}=;'+,`~\s]", arg):
            arg = '"' + arg + '"'
        args_out.append(arg)
    return args_out


def dlopen(name, handle=None):
    try:
        return ctypes.CDLL(name, handle=handle)
    except:
        pass


def find_library(pattern, arch=None):
    """
    Use ldconfig cache to find installed library.

    Can use fnmatch-style pattern matching.

    """
    try:
        p = sp.Popen(["/sbin/ldconfig", "-p"], stdout=sp.PIPE)
        stdout, stderr = p.communicate()
    except:
        return
    if not arch:
        try:
            p = sp.Popen(["file", "-L", sys.executable], stdout=sp.PIPE)
            file_stdout, file_stderr = p.communicate()
        except:
            pass
        else:
            # /usr/bin/python2.7: ELF 64-bit LSB shared object, x86-64,
            # version 1 (SYSV), dynamically linked, interpreter
            # /lib64/ld-linux-x86-64.so.2, for GNU/Linux 3.2.0,
            # BuildID[sha1]=41a1f0d4da3afee8f22d1947cc13a9f33f59f2b8, stripped
            parts = file_stdout.split(",")
            if len(parts) > 1:
                arch = parts[1].strip()
    for line in stdout.splitlines():
        # libxyz.so (libc6,x86_64) => /lib64/libxyz.so.1
        parts = line.split("=>", 1)
        candidate = parts[0].split(None, 1)
        if len(parts) < 2 or len(candidate) < 2:
            continue
        info = candidate[1].strip("( )").split(",")
        if arch and len(info) > 1 and info[1].strip() != arch:
            # Skip libs for wrong arch
            continue
        filename = candidate[0]
        if fnmatch.fnmatch(filename, pattern):
            path = parts[1].strip()
            return path


def expanduseru(path):
    """Unicode version of os.path.expanduser"""
    return str(os.path.expanduser(path))


def expandvarsu(path):
    """Unicode version of os.path.expandvars"""
    return str(os.path.expandvars(path))


def fname_ext(path):
    """Get filename and extension"""
    return os.path.splitext(os.path.basename(path))


def get_program_file(name, foldername):
    """Get path to program file"""
    paths = None
    exe_ext = ""
    return which(name + exe_ext, paths=paths)


def getenvu(name, default=None):
    """Unicode version of os.getenv"""
    var = os.getenv(name, default)
    if isinstance(var, str):
        return var if isinstance(var, str) else str(var, fs_enc)


def getgroups(username=None, names_only=False):
    """
    Return a list of groups that user is member of, or groups of current
    process if username not given

    """
    if username is None:
        groups = [grp.getgrgid(g) for g in os.getgroups()]
    else:
        groups = [g for g in grp.getgrall() if username in g.gr_mem]
        gid = pwd.getpwnam(username).pw_gid
        groups.append(grp.getgrgid(gid))
    if names_only:
        groups = [g.gr_name for g in groups]
    return groups


def islink(path):
    return os.path.islink(path)


def is_superuser():
    return os.geteuid() == 0


def launch_file(filepath):
    """
    Open a file with its assigned default app.

    Return tuple(returncode, stdout, stderr) or None if functionality not available

    """
    filepath = filepath.encode(fs_enc)
    retcode = None
    kwargs = dict(stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE)
    if which("xdg-open"):
        retcode = sp.call(["xdg-open", filepath], **kwargs)
    return retcode


def listdir_re(path, rex=None):
    """Filter directory contents through a regular expression"""
    files = os.listdir(path)
    if rex:
        rex = re.compile(rex, re.IGNORECASE)
        files = list(filter(rex.search, files))
    return files


def mkstemp_bypath(path, dir=None, text=False):
    """
    Wrapper around mkstemp that uses filename and extension from path as prefix
    and suffix for the temporary file, and the directory component as temporary
    file directory if 'dir' is not given.

    """
    fname, ext = fname_ext(path)
    if not dir:
        dir = os.path.dirname(path)
    return tempfile.mkstemp(ext, fname + "-", dir, text)


def mksfile(filename):
    """
    Create a file safely and return (fd, abspath)

    If filename already exists, add '(n)' as suffix before extension (will
    try up to os.TMP_MAX or 10000 for n)

    Basically, this works in a similar way as _mkstemp_inner from the
    standard library 'tempfile' module.

    """

    flags = tempfile._bin_openflags

    fname, ext = os.path.splitext(filename)

    for seq in range(tempfile.TMP_MAX):
        if not seq:
            pth = filename
        else:
            pth = "%s(%i)%s" % (fname, seq, ext)
        try:
            fd = os.open(pth, flags, 0o600)
            tempfile._set_cloexec(fd)
            return (fd, os.path.abspath(pth))
        except OSError as e:
            if e.errno == errno.EEXIST:
                continue  # Try again
            raise

    raise IOError(errno.EEXIST, "No usable temporary file name found")


def movefile(src, dst, overwrite=True):
    """Move a file to another location.

    dst can be a directory in which case a file with the same basename as src
    will be created in it.

    Set overwrite to True to make sure existing files are overwritten.

    """
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    if os.path.isfile(dst) and overwrite:
        os.remove(dst)
    shutil.move(src, dst)


def putenvu(name, value):
    """Unicode version of os.putenv (also correctly updates os.environ)"""
    if sys.platform == "win32" and isinstance(value, str):
        ctypes.windll.kernel32.SetEnvironmentVariableW(str(name), value)
    else:
        os.environ[name] = value.encode(fs_enc)


def parse_reparse_buffer(buf):
    """Implementing the below in Python:

    typedef struct _REPARSE_DATA_BUFFER {
            ULONG  ReparseTag;
            USHORT ReparseDataLength;
            USHORT Reserved;
            union {
                    struct {
                            USHORT SubstituteNameOffset;
                            USHORT SubstituteNameLength;
                            USHORT PrintNameOffset;
                            USHORT PrintNameLength;
                            ULONG Flags;
                            WCHAR PathBuffer[1];
                    } SymbolicLinkReparseBuffer;
                    struct {
                            USHORT SubstituteNameOffset;
                            USHORT SubstituteNameLength;
                            USHORT PrintNameOffset;
                            USHORT PrintNameLength;
                            WCHAR PathBuffer[1];
                    } MountPointReparseBuffer;
                    struct {
                            UCHAR  DataBuffer[1];
                    } GenericReparseBuffer;
            } DUMMYUNIONNAME;
    } REPARSE_DATA_BUFFER, *PREPARSE_DATA_BUFFER;

    """
    # See https://docs.microsoft.com/en-us/windows-hardware/drivers/ddi/content/ntifs/ns-ntifs-_reparse_data_buffer

    data = {
        "tag": struct.unpack("<I", buf[:4])[0],
        "data_length": struct.unpack("<H", buf[4:6])[0],
        "reserved": struct.unpack("<H", buf[6:8])[0],
    }
    buf = buf[8:]

    if data["tag"] in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK):
        keys = [
            "substitute_name_offset",
            "substitute_name_length",
            "print_name_offset",
            "print_name_length",
        ]
        if data["tag"] == IO_REPARSE_TAG_SYMLINK:
            keys.append("flags")

        # Parsing
        for k in keys:
            if k == "flags":
                fmt, sz = "<I", 4
            else:
                fmt, sz = "<H", 2
            data[k] = struct.unpack(fmt, buf[:sz])[0]
            buf = buf[sz:]

    # Using the offset and lengths grabbed, we'll set the buffer.
    data["buffer"] = buf

    return data


def readlink(path):
    return os.readlink(path)


def relpath(path, start):
    """Return a relative version of a path"""
    path = os.path.abspath(path).split(os.path.sep)
    start = os.path.abspath(start).split(os.path.sep)
    if path == start:
        return "."
    elif path[: len(start)] == start:
        return os.path.sep.join(path[len(start) :])
    elif start[: len(path)] == path:
        return os.path.sep.join([".."] * (len(start) - len(path)))


def safe_glob(pathname):
    """
    Return a list of paths matching a pathname pattern.

    The pattern may contain simple shell-style wildcards a la
    fnmatch. However, unlike fnmatch, filenames starting with a
    dot are special cases that are not matched by '*' and '?'
    patterns.

    Like fnmatch.glob, but suppresses re.compile errors by escaping
    uncompilable path components.

    See https://bugs.python.org/issue738361

    """
    return list(safe_iglob(pathname))


def safe_iglob(pathname):
    """
    Return an iterator which yields the paths matching a pathname pattern.

    The pattern may contain simple shell-style wildcards a la
    fnmatch. However, unlike fnmatch, filenames starting with a
    dot are special cases that are not matched by '*' and '?'
    patterns.

    Like fnmatch.iglob, but suppresses re.compile errors by escaping
    uncompilable path components.

    See https://bugs.python.org/issue738361

    """
    dirname, basename = os.path.split(pathname)
    if not glob.has_magic(pathname):
        if basename:
            if os.path.lexists(pathname):
                yield pathname
        else:
            # Patterns ending with a slash should match only directories
            if os.path.isdir(dirname):
                yield pathname
        return
    if not dirname:
        for name in safe_glob1(os.curdir, basename):
            yield name
        return
    # `os.path.split()` returns the argument itself as a dirname if it is a
    # drive or UNC path.  Prevent an infinite recursion if a drive or UNC path
    # contains magic characters (i.e. r'\\?\C:').
    if dirname != pathname and glob.has_magic(dirname):
        dirs = safe_iglob(dirname)
    else:
        dirs = [dirname]
    if glob.has_magic(basename):
        glob_in_dir = safe_glob1
    else:
        glob_in_dir = glob.glob0
    for dirname in dirs:
        for name in glob_in_dir(dirname, basename):
            yield os.path.join(dirname, name)


def safe_glob1(dirname, pattern):
    if not dirname:
        dirname = os.curdir
    if isinstance(pattern, str) and not isinstance(dirname, str):
        dirname = str(dirname, sys.getfilesystemencoding() or sys.getdefaultencoding())
    try:
        names = os.listdir(dirname)
    except os.error:
        return []
    if pattern[0] != ".":
        names = [x for x in names if x[0] != "."]
    return safe_shell_filter(names, pattern)


def safe_shell_filter(names, pat):
    """
    Return the subset of the list NAMES that match PAT

    Like fnmatch.filter, but suppresses re.compile errors by escaping
    uncompilable path components.

    See https://bugs.python.org/issue738361

    """
    import posixpath

    result = []
    pat = os.path.normcase(pat)
    try:
        re_pat = _cache[pat]
    except KeyError:
        res = safe_translate(pat)
        if len(_cache) >= _MAXCACHE:
            _cache.clear()
        _cache[pat] = re_pat = re.compile(res)
    match = re_pat.match
    if os.path is posixpath:
        # normcase on posix is NOP. Optimize it away from the loop.
        for name in names:
            if match(name):
                result.append(name)
    else:
        for name in names:
            if match(os.path.normcase(name)):
                result.append(name)
    return result


def safe_translate(pat):
    """
    Translate a shell PATTERN to a regular expression.

    Like fnmatch.translate, but suppresses re.compile errors by escaping
    uncompilable path components.

    See https://bugs.python.org/issue738361

    """
    if isinstance(getattr(os.path, "altsep", None), str):
        # Normalize path separators
        pat = pat.replace(os.path.altsep, os.path.sep)
    components = pat.split(os.path.sep)
    for i, component in enumerate(components):
        translated = fnmatch.translate(component)
        try:
            re.compile(translated)
        except re.error:
            translated = re.escape(component)
        components[i] = translated
    return re.escape(os.path.sep).join(components)


def waccess(path, mode):
    """Test access to path"""
    if mode & os.R_OK:
        try:
            test = open(path, "rb")
        except EnvironmentError:
            return False
        test.close()
    if mode & os.W_OK:
        if os.path.isdir(path):
            dir = path
        else:
            dir = os.path.dirname(path)
        try:
            if os.path.isfile(path):
                test = open(path, "ab")
            else:
                test = tempfile.TemporaryFile(prefix=".", dir=dir)
        except EnvironmentError:
            return False
        test.close()
    if mode & os.X_OK:
        return os.access(path, mode)
    return True


def which(executable, paths=None):
    """Return the full path of executable"""
    if not paths:
        paths = getenvu("PATH", os.defpath).split(os.pathsep)
    for cur_dir in paths:
        filename = os.path.join(cur_dir, executable)
        if os.path.isfile(filename):
            try:
                # make sure file is actually executable
                if os.access(filename, os.X_OK):
                    return filename
            except Exception as exception:
                pass
    return None


def whereis(
    names,
    bin=True,
    bin_paths=None,
    man=True,
    man_paths=None,
    src=True,
    src_paths=None,
    unusual=False,
    list_paths=False,
):
    """
    Wrapper around whereis

    """
    args = []
    if bin:
        args.append("-b")
    if bin_paths:
        args.append("-B")
        args.extend(bin_paths)
    if man:
        args.append("-m")
    if man_paths:
        args.append("-M")
        args.extend(man_paths)
    if src:
        args.append("-s")
    if src_paths:
        args.append("-S")
        args.extend(src_paths)
    if bin_paths or man_paths or src_paths:
        args.append("-f")
    if unusual:
        args.append("-u")
    if list_paths:
        args.append("-l")
    if isinstance(names, str):
        names = [names]
    p = sp.Popen(["whereis"] + args + names, stdout=sp.PIPE)
    stdout, stderr = p.communicate()
    result = {}
    for line in stdout.strip().splitlines():
        # $ whereis abc xyz
        # abc: /bin/abc
        # xyz: /bin/xyz /usr/bin/xyz
        match = line.split(":", 1)
        if match:
            result[match[0]] = match[-1].split()
    return result


class FileLock(object):
    _exception_cls = IOError

    def __init__(self, file_, exclusive=False, blocking=False):
        self._file = file_
        self.exclusive = exclusive
        self.blocking = blocking
        self.lock()

    def __enter__(self):
        return self

    def __exit__(self, etype, value, traceback):
        self.unlock()

    def lock(self):
        if self.exclusive:
            op = fcntl.LOCK_EX
        else:
            op = fcntl.LOCK_SH
        if not self.blocking:
            op |= fcntl.LOCK_NB
        fn = fcntl.flock
        args = (self._file.fileno(), op)
        self._call(fn, args, FileLock.LockingError)

    def unlock(self):
        if self._file.closed:
            return
        fn = fcntl.flock
        args = (self._file.fileno(), fcntl.LOCK_UN)
        self._call(fn, args, FileLock.UnlockingError)

    @staticmethod
    def _call(fn, args, exception_cls):
        try:
            fn(*args)
        except FileLock._exception_cls as exception:
            raise exception_cls(*exception.args)

    class Error(Exception):
        pass

    class LockingError(Error):
        pass

    class UnlockingError(Error):
        pass
