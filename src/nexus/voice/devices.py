"""Windows default endpoint identities; enumeration never opens an audio stream."""

import sys


def default_endpoint_ids():
    if sys.platform != "win32":
        return None
    import ctypes as c
    from uuid import UUID
    # PortAudio can initialize this thread as STA. Inspect HRESULT directly:
    # RPC_E_CHANGED_MODE means reuse its apartment, not an endpoint failure.
    ole = c.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [c.c_void_p, c.c_ulong]
    ole.CoInitializeEx.restype = c.c_long
    ole.CoCreateInstance.argtypes = [c.c_void_p, c.c_void_p, c.c_ulong, c.c_void_p, c.POINTER(c.c_void_p)]
    ole.CoCreateInstance.restype = c.c_long
    ole.CoTaskMemFree.argtypes = [c.c_void_p]
    initialized = ole.CoInitializeEx(None, 0)
    if initialized < 0 and initialized != -2147417850:
        return None
    enumerator = c.c_void_p()
    clsid = c.create_string_buffer(UUID("BCDE0395-E52F-467C-8E3D-C4579291692E").bytes_le)
    iid = c.create_string_buffer(UUID("A95664D2-9614-4F35-A746-DE8DB63617E6").bytes_le)

    def method(pointer, index, result, *args):
        table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(result, c.c_void_p, *args)(table[index])

    try:
        if ole.CoCreateInstance(clsid, None, 1, iid, c.byref(enumerator)) < 0:
            return None
        values = []
        for direction in (1, 0):  # capture, render
            device = c.c_void_p()
            result = method(enumerator, 4, c.c_long, c.c_int, c.c_int, c.POINTER(c.c_void_p))(
                enumerator, direction, 0, c.byref(device))
            if result < 0:
                values.append(None)
                continue
            identifier = c.c_void_p()
            try:
                result = method(device, 5, c.c_long, c.POINTER(c.c_void_p))(device, c.byref(identifier))
                values.append(c.wstring_at(identifier) if result >= 0 else None)
            finally:
                if identifier.value:
                    ole.CoTaskMemFree(identifier)
                method(device, 2, c.c_ulong)(device)
        return tuple(values)
    finally:
        if enumerator.value:
            method(enumerator, 2, c.c_ulong)(enumerator)
        if initialized >= 0:
            ole.CoUninitialize()
