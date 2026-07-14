from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import stat


@dataclass(frozen=True, slots=True)
class AclInspection:
    protected: bool
    current_user_full_control: bool
    system_full_control: bool
    inheritable: bool
    has_inherited_aces: bool
    world_or_users_write: bool


def _check_plain_directory(path: Path) -> None:
    info = os.lstat(path)
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise OSError("runner directory must not be a link or reparse point")


if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class _ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", _ACE_HEADER),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    _advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    _advapi32.GetAclInformation.restype = wintypes.BOOL
    _advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetAce.restype = wintypes.BOOL


def _win_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _local_free(pointer: ctypes.c_void_p) -> None:
    if pointer and _kernel32.LocalFree(pointer):
        raise _win_error()


def _sid_string(sid: ctypes.c_void_p) -> str:
    output = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(output)):
        raise _win_error()
    try:
        return ctypes.wstring_at(output)
    finally:
        _local_free(output)


def _current_user_sid() -> str:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise _win_error()
    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise _win_error()
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
            token, 1, buffer, size, ctypes.byref(size)
        ):
            raise _win_error()
        user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
        return _sid_string(user.User.Sid)
    finally:
        if not _kernel32.CloseHandle(token):
            raise _win_error()


def _set_protected_dacl(path: Path, current_sid: str) -> None:
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sddl = f"D:P(A;OICI;FA;;;{current_sid})(A;OICI;FA;;;SY)"
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)
    ):
        raise _win_error()
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not _advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present or not dacl:
            raise OSError("security descriptor has no DACL")
        result = _advapi32.SetNamedSecurityInfoW(
            str(path), 1, 0x00000004 | 0x80000000, None, None, dacl, None
        )
        if result:
            raise ctypes.WinError(result)
    finally:
        _local_free(descriptor)


def inspect_windows_acl(path: Path) -> AclInspection:
    if os.name != "nt":
        raise OSError("Windows ACL inspection is unavailable")
    _check_plain_directory(path)
    current_sid = _current_user_sid()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = _advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x00000004, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if result:
        raise ctypes.WinError(result)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise _win_error()
        info = _ACL_SIZE_INFORMATION()
        if not _advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), 2
        ):
            raise _win_error()
        current_full = system_full = False
        current_inheritable = system_inheritable = False
        current_inherited = system_inherited = False
        unsafe_write = False
        unsafe_sids = {"S-1-1-0", "S-1-5-11", "S-1-5-32-545"}
        write_mask = 0x40000000 | 0x000D0116
        for index in range(info.AceCount):
            ace_pointer = ctypes.c_void_p()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise _win_error()
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(_ACCESS_ALLOWED_ACE)).contents
            if ace.Header.AceType != 0:
                continue
            sid_pointer = ctypes.c_void_p(
                ace_pointer.value + _ACCESS_ALLOWED_ACE.SidStart.offset
            )
            sid = _sid_string(sid_pointer)
            full = (ace.Mask & 0x001F01FF) == 0x001F01FF
            ace_inheritable = (ace.Header.AceFlags & 0x03) == 0x03
            ace_inherited = bool(ace.Header.AceFlags & 0x10)
            if sid == current_sid:
                current_full = current_full or full
                current_inheritable = current_inheritable or ace_inheritable
                current_inherited = current_inherited or ace_inherited
            elif sid == "S-1-5-18":
                system_full = system_full or full
                system_inheritable = system_inheritable or ace_inheritable
                system_inherited = system_inherited or ace_inherited
            if sid in unsafe_sids and ace.Mask & write_mask:
                unsafe_write = True
        return AclInspection(
            protected=bool(control.value & 0x1000),
            current_user_full_control=current_full,
            system_full_control=system_full,
            inheritable=current_inheritable and system_inheritable,
            has_inherited_aces=current_inherited and system_inherited,
            world_or_users_write=unsafe_write,
        )
    finally:
        _local_free(descriptor)


def secure_runner_root(path: Path) -> AclInspection:
    path.mkdir(parents=True, exist_ok=True)
    _check_plain_directory(path)
    if os.name != "nt":
        os.chmod(path, 0o700)
        return AclInspection(True, True, True, True, False, False)
    _set_protected_dacl(path, _current_user_sid())
    _check_plain_directory(path)
    inspection = inspect_windows_acl(path)
    if not (
        inspection.protected
        and inspection.current_user_full_control
        and inspection.system_full_control
        and inspection.inheritable
        and not inspection.world_or_users_write
    ):
        raise OSError("runner root DACL verification failed")
    return inspection


def verify_inherited_call_directory(path: Path) -> AclInspection:
    _check_plain_directory(path)
    if os.name != "nt":
        os.chmod(path, 0o700)
        return AclInspection(False, True, True, False, True, False)
    inspection = inspect_windows_acl(path)
    if not (
        inspection.current_user_full_control
        and inspection.system_full_control
        and inspection.has_inherited_aces
        and not inspection.world_or_users_write
    ):
        raise OSError("runner call directory DACL verification failed")
    return inspection
