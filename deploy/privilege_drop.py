"""Linux container-only, fail-closed transition from secret bootstrap to app UID.

The container grants only SETUID/SETGID. No DAC override, persistent key store,
setuid executable, shell, or helper service is used.
"""
from __future__ import annotations
import ctypes
import os
from pathlib import Path

APP_UID = 10001
APP_GID = 10001
ID_CAPABILITIES = (1 << 6) | (1 << 7)  # SETGID, SETUID


def process_status():
    return dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)


def check_bootstrap_capabilities(status=None):
    s = process_status() if status is None else status
    if os.geteuid() != 0 or int(s['NoNewPrivs']) != 1:
        raise RuntimeError('Secret bootstrap requires root and no-new-privileges.')
    for field in ('CapEff', 'CapPrm', 'CapBnd'):
        if int(s[field], 16) != ID_CAPABILITIES:
            raise RuntimeError('Secret bootstrap requires exactly SETUID/SETGID capabilities.')
    if any(int(s[field],16) for field in ('CapInh','CapAmb')):
        raise RuntimeError('Secret bootstrap must not inherit or carry ambient capabilities.')


def _prctl(option, arg=0):
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.prctl
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    function.restype = ctypes.c_int
    if function(option, arg, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Process privilege transition failed.')


def clear_capabilities():
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
    header = Header(0x20080522, 0)  # Linux capability ABI v3, current process
    data = (Data * 2)()
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.capset
    function.argtypes = [ctypes.POINTER(Header), ctypes.POINTER(Data)]
    function.restype = ctypes.c_int
    if function(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), 'Process capability clearing failed.')
    _prctl(47, 4)  # PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL


def verify_application_identity(status=None):
    s = process_status() if status is None else status
    if list(map(int, s['Uid'].split())) != [APP_UID] * 4:
        raise RuntimeError('Application user transition failed.')
    if list(map(int, s['Gid'].split())) != [APP_GID] * 4 or s['Groups'].split():
        raise RuntimeError('Application group transition failed.')
    if int(s['NoNewPrivs']) != 1:
        raise RuntimeError('Application requires no-new-privileges.')
    if any(int(s[field],16) for field in ('CapEff','CapPrm','CapInh','CapAmb')):
        raise RuntimeError('Application still has capabilities.')
    # Bounding bits alone grant no power. NNP and empty permitted/inheritable/
    # ambient sets prevent restoring them through exec or a file capability.
    if int(s['CapBnd'],16) & ~ID_CAPABILITIES:
        raise RuntimeError('Unexpected capability bounding set.')
    return {'uid':APP_UID,'gid':APP_GID,'supplementary_groups':[],
            'effective_permitted_inheritable_ambient_caps':0,'no_new_privileges':True}


def drop_to_application():
    check_bootstrap_capabilities()
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    _prctl(8, 0)   # PR_SET_KEEPCAPS: explicitly do not retain capabilities
    _prctl(47, 4)
    os.setgroups([])
    os.setresgid(APP_GID, APP_GID, APP_GID)
    os.setresuid(APP_UID, APP_UID, APP_UID)
    clear_capabilities()
    return verify_application_identity()
