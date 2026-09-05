"""Windows DPAPI, bound to the logged-in user, reached through ctypes.

No new dependency. ``crypt32.dll`` ships with the OS and the two calls this
needs have a stable ANSI-free signature, so a wheel would buy nothing but a
supply-chain edge.

User-bound, not machine-bound: ``CRYPTPROTECT_LOCAL_MACHINE`` is deliberately
NOT set, so a copy of the store file decrypts under the operator's account and
nowhere else. That is the property the phase asks for, and setting the flag
would silently turn it off while every test still passed.

``CRYPTPROTECT_UI_FORBIDDEN`` is set on both calls. A protected blob can carry
a prompt structure that pops a dialog at unprotect time; an overnight run has
nobody to answer it, so the runtime asks the OS to fail instead of block.

Off Windows this raises rather than degrading to plaintext. A credential store
that quietly stopped encrypting would be the worst possible failure here: the
panel would say "saved" and mean something entirely different.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DpapiUnavailable(RuntimeError):
    """This platform has no DPAPI, so there is nowhere safe to put a value."""


class DpapiError(RuntimeError):
    """crypt32 refused. Carries the Windows error code it refused with."""

    def __init__(self, operation: str, code: int) -> None:
        super().__init__(
            f"{operation} failed with Windows error {code}: "
            f"{ctypes.FormatError(code)}"
        )
        self.code = code


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def is_available() -> bool:
    """Whether this process can protect a value at all."""
    return sys.platform == "win32"


def _require() -> None:
    if not is_available():
        raise DpapiUnavailable(
            "the credential store encrypts with Windows DPAPI, which this "
            f"platform ({sys.platform}) does not have. No credential was "
            "stored; nothing was written in plaintext instead."
        )


def _blob_in(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _Blob) -> bytes:
    """Copy a crypt32-allocated blob out and free it.

    The free is unconditional. crypt32 allocates with ``LocalAlloc`` and hands
    ownership over; leaking it leaks a decrypted secret into the process heap
    for as long as the process lives, which is exactly the proliferation this
    store exists to stop.
    """
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


#: Stands for "nobody has asked yet", so that a machine where the SID cannot
#: be read is asked once rather than on every call.
_UNASKED = object()

#: The account this process runs as, asked once. A SID cannot change under a
#: running process, and the call is four Win32 hops, so asking per credential
#: per panel poll would be paying for an answer that cannot have moved.
_ACCOUNT: object = _UNASKED

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


def _bind() -> None:
    """Declare the four signatures before calling any of them.

    Not optional. ``GetCurrentProcess`` returns a pseudo-handle of all bits
    set; left at the default ``c_int`` restype it comes back as a Python int
    too wide for the untyped ``OpenProcessToken`` to accept, and the read
    fails with an OverflowError that looks nothing like a permissions problem.
    """
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL


def account_id() -> str | None:
    """The Windows SID this process runs as, or ``None`` if it cannot be read.

    Not a secret, and it never leaves this process: it is hashed before it
    reaches the store, because equality is the only thing anything here asks
    of it and a raw SID is a machine identifier written into a file this
    project copies onto other machines.

    ``None`` rather than raising. This answers "was that blob sealed here",
    and a process that cannot find out has to say it does not know rather than
    report the blob as foreign, which would tell an operator to clear a
    credential that works perfectly.
    """
    global _ACCOUNT
    if _ACCOUNT is _UNASKED:
        _ACCOUNT = _read_account_id()
    return _ACCOUNT  # type: ignore[return-value]


def _read_account_id() -> str | None:
    if not is_available():
        return None
    try:
        _bind()
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, _TOKEN_USER, buffer, size, ctypes.byref(size)
            ):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents.Sid
            text = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return None
            try:
                return text.value
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    except OSError:
        return None


def protect(value: str, description: str) -> bytes:
    """Encrypt ``value`` for the current user. Returns the opaque blob."""
    _require()
    source = _blob_in(value.encode("utf-8"))
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        ctypes.c_wchar_p(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise DpapiError("CryptProtectData", ctypes.GetLastError())
    return _take(out)


def unprotect(blob: bytes) -> str:
    """Decrypt a blob produced by :func:`protect`.

    Raises :class:`DpapiError` when the blob was protected by a different
    user or on a different machine, which is the error the phase's
    "copy it elsewhere and it does not open" criterion is evidenced by.
    """
    _require()
    source = _blob_in(blob)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise DpapiError("CryptUnprotectData", ctypes.GetLastError())
    return _take(out).decode("utf-8")


__all__ = [
    "DpapiError",
    "DpapiUnavailable",
    "account_id",
    "is_available",
    "protect",
    "unprotect",
]
