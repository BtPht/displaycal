import atexit
import errno
import glob
import os
import socket
import subprocess as sp
import sys
import threading
from platform import platform as print_platform
from time import sleep

from config import (
    appbasename,
    confighome,
    datahome,
    enc,
    fs_enc,
    get_data_path,
    getcfg,
    initcfg,
    logdir,
    pyname,
    resfiles,
    runtype,
)
from debughelpers import ResourceError, handle_error
from log import log, safe_print
from meta import VERSION, VERSION_BASE, VERSION_STRING, build
from meta import name as appname
from multiprocess import mp
from options import verbose
from utils.util_os import FileLock
from utils.util_str import safe_str, safe_unicode

import logging


def _excepthook(etype, value, tb):
    handle_error((etype, value, tb))


sys.excepthook = _excepthook


def _main(module, name, applockfilename, probe_ports=True):
    # Allow multiple instances only for curve viewer, profile info,
    # scripting client, synthetic profile creator and testchart editor
    multi_instance = (
        "curve-viewer",
        "profile-info",
        "scripting-client",
        "synthprofile",
        "testchart-editor",
    )
    lock = AppLock(applockfilename, "a+", True, module in multi_instance)
    if not lock:
        # If a race condition occurs, do not start another instance
        print("Not starting another instance.")
        return
    log("=" * 80)
    if verbose >= 1:
        version = VERSION_STRING
        if VERSION > VERSION_BASE:
            version += " Beta"
        print(pyname + runtype, version, build)

    # TODO: the call to platform() crashes in a subprocess
    # logging.info(print_platform())
    print("Python " + sys.version)
    cafile = os.getenv("SSL_CERT_FILE")
    if cafile:
        print("CA file", cafile)
    # Enable faulthandler
    try:
        import faulthandler
    except Exception as exception:
        print(exception)
    else:
        try:
            faulthandler.enable(open(os.path.join(logdir, pyname + "-fault.log"), "w"))
        except Exception as exception:
            safe_print(exception)
        else:
            print("Faulthandler", getattr(faulthandler, "__version__", ""))
    from displaycal_wx.wxaddons import wx

    if "phoenix" in wx.PlatformInfo:
        pass
        # py2exe helper so wx.xml gets picked up
        # from wx import xml
    print("wxPython " + wx.version())
    print("Encoding: " + enc)
    print("File system encoding: " + fs_enc)
    initcfg(module)
    host = "127.0.0.1"
    defaultport = getcfg("app.port")
    lock2pids_ports = {}
    opid = os.getpid()
    if probe_ports:
        # Check for currently used ports
        lockfilenames = glob.glob(os.path.join(confighome, "*.lock"))
        for lockfilename in lockfilenames:
            safe_print("Lockfile", lockfilename)
            try:
                if lock and lockfilename == applockfilename:
                    lockfile = lock
                    lock.seek(0)
                else:
                    lockfile = AppLock(lockfilename, "r", True, True)
                if lockfile:
                    if not lockfilename in lock2pids_ports:
                        lock2pids_ports[lockfilename] = []
                    for ln, line in enumerate(lockfile.read().splitlines(), 1):
                        if ":" in line:
                            # DisplayCAL >= 3.8.8.2 with localhost blocked
                            pid, port = line.split(":", 1)
                            if pid:
                                try:
                                    pid = int(pid)
                                except ValueError as exception:
                                    # This shouldn't happen
                                    safe_print(
                                        "Warning - couldn't parse PID "
                                        "as int: %r (%s line %i)"
                                        % (pid, lockfilename, ln)
                                    )
                                    pid = None
                                else:
                                    safe_print("Existing client using PID", pid)
                        else:
                            # DisplayCAL <= 3.8.8.1 or localhost ok
                            pid = None
                            port = line
                        if port:
                            try:
                                port = int(port)
                            except ValueError as exception:
                                # This shouldn't happen
                                safe_print(
                                    "Warning - couldn't parse port as int: %r "
                                    "(%s line %i)" % (port, lockfilename, ln)
                                )
                                port = None
                            else:
                                safe_print("Existing client using port", port)
                        if pid or port:
                            lock2pids_ports[lockfilename].append((pid, port))
                if not lock or lockfilename != applockfilename:
                    lockfile.unlock()
            except EnvironmentError as exception:
                # This shouldn't happen
                safe_print(
                    "Warning - could not read lockfile %s:" % lockfilename, exception
                )
        if module not in multi_instance:
            # Check lockfile(s) and probe port(s)
            for lockfilename in [applockfilename]:
                incoming = None
                pids_ports = lock2pids_ports.get(lockfilename)
                if pids_ports:
                    pid, port = pids_ports[0]
                    appsocket = AppSocket()
                    if appsocket and port:
                        safe_print("Connecting to %s..." % port)
                        if appsocket.connect(host, port):
                            safe_print("Connected to", port)
                            # Other instance already running?
                            # Get appname to check if expected app is actually
                            # running under that port
                            safe_print("Getting instance name")
                            if appsocket.send("getappname"):
                                safe_print(
                                    "Sent scripting request, awaiting response..."
                                )
                                incoming = appsocket.read().rstrip("\4")
                                safe_print("Got response: %r" % incoming)
                                if incoming:
                                    if incoming != pyname:
                                        incoming = None
                                else:
                                    incoming = False
                        while incoming:
                            # Send args as UTF-8
                            if module == "apply-profiles":
                                # Always try to close currently running instance
                                safe_print("Closing existing instance")
                                cmd = "exit" if incoming == pyname else "close"
                                data = [cmd]
                                lock.unlock()
                            else:
                                # Send module/appname to notify running app
                                safe_print("Notifying existing instance")
                                data = [module or appname]
                                if module != "3DLUT-maker":
                                    for arg in sys.argv[1:]:
                                        data.append(
                                            safe_str(safe_unicode(arg), "UTF-8")
                                        )
                            data = sp.list2cmdline(data)
                            if appsocket.send(data):
                                safe_print(
                                    "Sent scripting request, awaiting response..."
                                )
                                incoming = appsocket.read().rstrip("\4")
                                safe_print("Got response: %r" % incoming)
                                if module == "apply-profiles":
                                    if incoming == "":
                                        # Successfully sent our close request.
                                        incoming = "ok"
                                    elif incoming == "invalid" and cmd == "exit":
                                        # < 3.8.8.1 didn't have exit command
                                        continue
                            break
                        appsocket.close()
                if incoming == "ok":
                    # Successfully sent our request
                    if module == "apply-profiles":
                        # Wait for lockfile to be removed, in which case
                        # we know the running instance has successfully
                        # closed.
                        safe_print(
                            "Waiting for existing instance to exit and "
                            "delete lockfile",
                            lockfilename,
                        )
                        while os.path.isfile(lockfilename):
                            sleep(0.05)
                        lock.lock()
                        safe_print("Existing instance exited.")
                        incoming = None
                        if lockfilename in lock2pids_ports:
                            del lock2pids_ports[lockfilename]
                    break
            if incoming is not None:
                # Other instance running?
                from . import localization as lang

                lang.init()
                if incoming == "ok":
                    # Successfully sent our request
                    safe_print(lang.getstr("app.otherinstance.notified"))
                elif module == "apply-profiles":
                    safe_print("Not starting another instance.")
                else:
                    # Other instance busy?
                    handle_error(lang.getstr("app.otherinstance", name))
                # Exit
                return
    # Use exclusive lock during app startup
    with lock:
        # Create listening socket
        appsocket = AppSocket()
        if appsocket:
            sys._appsocket = appsocket.socket
            if getcfg("app.allow_network_clients"):
                host = ""
            used_ports = [
                pid_port[1]
                for pids_ports in list(lock2pids_ports.values())
                for pid_port in pids_ports
            ]
            candidate_ports = [0]
            if not defaultport in used_ports:
                candidate_ports.insert(0, defaultport)
            for port in candidate_ports:
                try:
                    sys._appsocket.bind((host, port))
                except socket.error as exception:
                    if port == 0:
                        safe_print(
                            "Warning - could not bind to %s:%s:" % (host, port),
                            exception,
                        )
                        del sys._appsocket
                        break
                else:
                    try:
                        sys._appsocket.settimeout(0.2)
                    except socket.error as exception:
                        safe_print(
                            "Warning - could not set socket " "timeout:", exception
                        )
                        del sys._appsocket
                        break
                    try:
                        sys._appsocket.listen(1)
                    except socket.error as exception:
                        safe_print(
                            "Warning - could not listen on " "socket:", exception
                        )
                        del sys._appsocket
                        break
                    try:
                        port = sys._appsocket.getsockname()[1]
                    except socket.error as exception:
                        safe_print(
                            "Warning - could not get socket " "address:", exception
                        )
                        del sys._appsocket
                        break
                    sys._appsocket_port = port
                    break
        if not hasattr(sys, "_appsocket_port"):
            port = ""
        lock.seek(0)
        if module not in multi_instance:
            lock.truncate(0)
        if not port:
            lock.write("%s:%s" % (opid, port))
        else:
            lock.write(port)
        atexit.register(lambda: safe_print("Ran application exit handlers"))
        from displaycal_wx.wxwindows import BaseApp

        BaseApp.register_exitfunc(_exit, applockfilename, port)
        # Check for required resource files
        mod2res = {
            "3DLUT-maker": ["xrc/3dlut.xrc"],
            "curve-viewer": [],
            "profile-info": [],
            "scripting-client": [],
            "synthprofile": ["xrc/synthicc.xrc"],
            "testchart-editor": [],
            "VRML-to-X3D-converter": [],
        }
        for filename in mod2res.get(module, resfiles):
            path = get_data_path(os.path.sep.join(filename.split("/")))
            if not path or not os.path.isfile(path):
                from . import localization as lang

                lang.init()
                raise ResourceError(
                    lang.getstr("resources.notfound.error") + "\n" + filename
                )
        # Create main data dir if it does not exist
        if not os.path.exists(datahome):
            try:
                os.makedirs(datahome)
            except Exception as exception:
                handle_error(
                    UserWarning(
                        "Warning - could not create " "directory '%s'" % datahome
                    )
                )
        # Initialize & run
        if module == "3DLUT-maker":
            from displaycal_wx.wxLUT3DFrame import main
        elif module == "curve-viewer":
            from displaycal_wx.wxLUTViewer import main
        elif module == "profile-info":
            from displaycal_wx.wxProfileInfo import main
        elif module == "scripting-client":
            from displaycal_wx.wxScriptingClient import main
        elif module == "synthprofile":
            from displaycal_wx.wxSynthICCFrame import main
        elif module == "testchart-editor":
            from displaycal_wx.wxTestchartEditor import main
        elif module == "VRML-to-X3D-converter":
            from displaycal_wx.wxVRML2X3D import main
        elif module == "apply-profiles":
            from .profile_loader import main
        else:
            from .DisplayCAL import main
    # Run main after releasing lock
    main()


def main(module=None):
    mp.freeze_support()
    if mp.current_process().name != "MainProcess":
        return
    if module:
        name = "%s-%s" % (appbasename, module)
    else:
        name = appbasename
    applockfilename = os.path.join(confighome, "%s.lock" % name)
    try:
        _main(module, name, applockfilename)
    except Exception as exception:
        if isinstance(exception, ResourceError):
            error = exception
        else:
            error = Error("Fatal error: " + safe_unicode(exception))
        handle_error(error)
        _exit(applockfilename, getattr(sys, "_appsocket_port", ""))


def _exit(lockfilename, oport):
    for process in mp.active_children():
        if not "Manager" in process.name:
            safe_print("Terminating zombie process", process.name)
            process.terminate()
            safe_print(process.name, "terminated")
    for thread in threading.enumerate():
        if (
            thread.isAlive()
            and thread is not threading.currentThread()
            and not thread.isDaemon()
        ):
            safe_print("Waiting for thread %s to exit" % thread.getName())
            thread.join()
            safe_print(thread.getName(), "exited")
    if lockfilename and os.path.isfile(lockfilename):
        with AppLock(lockfilename, "r+", True, True) as lock:
            _update_lockfile(lockfilename, oport, lock)
    safe_print("Exiting", pyname)


def _update_lockfile(lockfilename, oport, lock):
    if lock:
        # Each lockfile may contain multiple ports of running instances
        try:
            pids_ports = lock.read().splitlines()
        except EnvironmentError as exception:
            safe_print(
                "Warning - could not read lockfile %s: %r" % (lockfilename, exception)
            )
            filtered_pids_ports = []
        else:
            opid = os.getpid()

            # Determine if instances still running. If not still running,
            # remove from list of ports
            for i in reversed(range(len(pids_ports))):
                pid_port = pids_ports[i]
                if ":" in pid_port:
                    # DisplayCAL >= 3.8.8.2 with localhost blocked
                    pid, port = pid_port.split(":", 1)
                    if pid:
                        try:
                            pid = int(pid)
                        except ValueError:
                            # This shouldn't happen
                            pid = None
                else:
                    # DisplayCAL <= 3.8.8.1 or localhost ok
                    pid = None
                    port = pid_port
                if port:
                    try:
                        port = int(port)
                    except ValueError:
                        # This shouldn't happen
                        continue
                if (pid and pid == opid and not port) or (port and port == oport):
                    # Remove ourself
                    pids_ports[i] = ""
                    continue
                if not port:
                    continue
                appsocket = AppSocket()
                if not appsocket:
                    break
                if not appsocket.connect("127.0.0.1", port):
                    # Other instance probably died
                    pids_ports[i] = ""
                appsocket.close()
            # Filtered PIDs & ports (only used for checking)
            filtered_pids_ports = [pid_port for pid_port in pids_ports if pid_port]
            if filtered_pids_ports:
                # Write updated lockfile
                try:
                    lock.seek(0)
                    lock.truncate(0)
                except EnvironmentError as exception:
                    safe_print(
                        "Warning - could not update lockfile %s: %r"
                        % (lockfilename, exception)
                    )
                else:
                    lock.write("\n".join(pids_ports))
            else:
                lock.close()
                try:
                    os.remove(lockfilename)
                except EnvironmentError as exception:
                    safe_print(
                        "Warning - could not remove lockfile %s: %r"
                        % (lockfilename, exception)
                    )


def main_3dlut_maker():
    main("3DLUT-maker")


def main_curve_viewer():
    main("curve-viewer")


def main_profile_info():
    main("profile-info")


def main_synthprofile():
    main("synthprofile")


def main_testchart_editor():
    main("testchart-editor")


class AppLock(object):
    def __init__(self, lockfilename, mode, exclusive=False, blocking=False):
        self._lockfilename = lockfilename
        self._mode = mode
        self._lockfile = None
        self._lock = None
        self._exclusive = exclusive
        self._blocking = blocking
        self.lock()

    def __enter__(self):
        return self

    def __exit__(self, etype, value, traceback):
        self.unlock()

    def __getattr__(self, name):
        return getattr(self._lockfile, name)

    def __iter__(self):
        return self._lockfile

    def __bool__(self):
        return bool(self._lock)

    def lock(self):
        lockdir = os.path.dirname(self._lockfilename)
        try:
            if not os.path.isdir(lockdir):
                os.makedirs(lockdir)
            # Create lockfile
            self._lockfile = open(self._lockfilename, self._mode)
        except EnvironmentError as exception:
            # This shouldn't happen
            safe_print(
                "Error - could not open lockfile %s:" % self._lockfilename, exception
            )
        else:
            try:
                self._lock = FileLock(self._lockfile, self._exclusive, self._blocking)
            except FileLock.LockingError as exception:
                pass
            except EnvironmentError as exception:
                # This shouldn't happen
                safe_print(
                    "Error - could not lock lockfile %s:" % self._lockfile.name,
                    exception,
                )
            else:
                return True
        return False

    def unlock(self):
        if self._lockfile:
            try:
                self._lockfile.close()
            except EnvironmentError as exception:
                # This shouldn't happen
                safe_print(
                    "Error - could not close lockfile %s:" % self._lockfile.name,
                    exception,
                )
        if self._lock:
            try:
                self._lock.unlock()
            except FileLock.UnlockingError as exception:
                # This shouldn't happen
                safe_print(
                    "Warning - could not unlock lockfile %s:" % self._lockfile.name,
                    exception,
                )

    def write(self, contents):
        if self._lockfile:
            try:
                self._lockfile.write("%s\n" % contents)
            except EnvironmentError as exception:
                # This shouldn't happen
                safe_print(
                    "Error - could not write to lockfile %s:" % self._lockfile.name,
                    exception,
                )


class AppSocket(object):
    def __init__(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error as exception:
            # This shouldn't happen
            safe_print("Warning - could not create TCP socket:", exception)

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def __bool__(self):
        return hasattr(self, "socket")

    def connect(self, host, port):
        try:
            self.socket.connect((host, port))
        except socket.error as exception:
            # Other instance probably died
            safe_print("Connection to %s:%s failed:" % (host, port), exception)
            return False
        return True

    def read(self):
        incoming = ""
        while not "\4" in incoming:
            try:
                data = self.socket.recv(1024)
            except socket.error as exception:
                if exception.errno == errno.EWOULDBLOCK:
                    sleep(0.05)
                    continue
                safe_print("Warning - could not receive data:", exception)
                break
            if not data:
                break
            incoming += data
        return incoming

    def send(self, data):
        try:
            self.socket.sendall(data + "\n")
        except socket.error as exception:
            # Connection lost?
            safe_print("Warning - could not send data %r:" % data, exception)
            return False
        return True


class Error(Exception):
    pass


if __name__ == "__main__":
    main()
