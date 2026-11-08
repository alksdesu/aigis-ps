from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


def game_processes(executable: Path) -> list[int]:
    if os.name != "nt":
        return []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
    psapi.EnumProcesses.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD))
    identifiers = (wintypes.DWORD * 32768)()
    used = wintypes.DWORD()
    if not psapi.EnumProcesses(identifiers, ctypes.sizeof(identifiers), ctypes.byref(used)):
        raise ctypes.WinError(ctypes.get_last_error())
    if used.value >= ctypes.sizeof(identifiers):
        raise RuntimeError("The process inventory exceeded its limit")
    wanted = os.path.normcase(os.path.abspath(executable))
    matches = []
    for identifier in identifiers[:used.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel.OpenProcess(0x1000, False, identifier)
        if not handle:
            continue
        try:
            name = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(name))
            if kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
                if os.path.normcase(os.path.abspath(name.value)) == wanted:
                    matches.append(identifier)
        finally:
            kernel.CloseHandle(handle)
    return matches
