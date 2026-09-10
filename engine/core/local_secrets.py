"""Optional Windows DPAPI credentials, encrypted for the current OS user."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re


class _Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def _path(name):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]+',name):raise ValueError('invalid credential name')
    return Path(os.environ['LOCALAPPDATA'])/'Arcana'/'credentials'/f'{name}.dpapi'


def _crypt(content,*,decrypt):
    if os.name!='nt':raise RuntimeError('local credential storage requires Windows')
    buffer=ctypes.create_string_buffer(content)
    source=_Blob(len(content),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    target=_Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    function=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes=[ctypes.POINTER(_Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(_Blob)]
    function.restype=wintypes.BOOL
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:return ctypes.string_at(target.data,target.size)
    finally:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.LocalFree.argtypes=[ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(target.data,ctypes.c_void_p))


def save_local_secret(name,value):
    """Persist a caller-authorized key outside the repo, encrypted by Windows."""
    path=_path(name);path.parent.mkdir(parents=True,exist_ok=True)
    encrypted=_crypt(value.encode(),decrypt=False)
    temporary=path.with_suffix('.tmp')
    temporary.write_bytes(encrypted);temporary.replace(path)


def get_local_secret(name):
    value=os.getenv(name,'').strip()
    if value:return value
    if os.name!='nt':return ''
    path=_path(name)
    return _crypt(path.read_bytes(),decrypt=True).decode() if path.exists() else ''


def activate_local_secrets(names=('DART_API_KEY','ALPHA_VANTAGE_API_KEY')):
    """Expose configured keys only to the current pipeline process and children."""
    for name in names:
        value=get_local_secret(name)
        if value and not os.getenv(name):os.environ[name]=value
