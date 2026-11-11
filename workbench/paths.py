"""A Windows user keeps one database when the application directory changes."""
import os
import ctypes
import uuid
from pathlib import Path


def windows_user_profile():
    """User profile storage avoids AppData virtualization by a packaged parent."""
    class Guid(ctypes.Structure):
        _fields_ = [('data1', ctypes.c_ulong), ('data2', ctypes.c_ushort),
                    ('data3', ctypes.c_ushort), ('data4', ctypes.c_ubyte * 8)]
    folder = Guid.from_buffer_copy(uuid.UUID('5E6C858F-0E22-4760-9AFE-EA3317B67173').bytes_le)
    value = ctypes.c_wchar_p()
    function = ctypes.windll.shell32.SHGetKnownFolderPath
    function.argtypes = [ctypes.POINTER(Guid), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    function.restype = ctypes.c_long
    # KF_FLAG_NO_PACKAGE_REDIRECTION also covers launches from the desktop app.
    if function(ctypes.byref(folder), 0x10000, None, ctypes.byref(value)) != 0:
        raise RuntimeError('Windows user profile directory is unavailable')
    try:
        return Path(value.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoTaskMemFree(value)


def storage_root():
    # Explicit override is used by isolated tests; normal launchers never set it.
    override = os.environ.get('GPTPLUS_WORKBENCH_STORAGE_ROOT')
    if override is not None:
        if not override or not Path(override).is_absolute():
            raise RuntimeError('Workbench storage override must be an absolute directory')
        return Path(override).resolve()
    local = windows_user_profile()
    if not local.is_absolute():
        raise RuntimeError('Windows user profile directory is not absolute')
    return (local / 'GPTPlusWorkbench').resolve()


def data_directory():
    return storage_root() / 'data'
