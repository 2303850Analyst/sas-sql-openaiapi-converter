"""Windows current-user DPAPI, no plaintext secrets in app configuration or SQLite."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def transform(data: bytes, protect: bool):
    if os.name != 'nt':
        raise ValueError('Windows DPAPI secret storage requires Windows')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError('Cannot access secret for the current Windows user')
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        kernel.LocalFree(result.data)


def read_secret(path, key_name):
    path = Path(path)
    if path.suffix == '.dpapi':
        value = transform(path.read_bytes(), False).decode('utf-8')
    else:
        text = path.read_text(encoding='utf-8-sig')
        values = [line.strip().split('=', 1)[1].strip().strip('\"\'') for line in text.splitlines()
                  if line.strip().startswith(key_name + '=')]
        if len(values) != 1:
            raise ValueError('Model key is missing or ambiguous')
        value = values[0]
    if not value or '\n' in value or '\r' in value:
        raise ValueError('Invalid model key')
    return value


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--from-env-file',type=Path,required=True)
    p.add_argument('--key-name',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.suffix != '.dpapi' or a.output.exists():
        raise ValueError('Choose a new .dpapi file outside the repository')
    raw=read_secret(a.from_env_file,a.key_name)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_bytes(transform(raw.encode('utf-8'),True))
    if read_secret(a.output,a.key_name)!=raw:
        raise ValueError('Secret round-trip verification failed')
    print('Secret protected for current Windows user; value not displayed')


if __name__=='__main__':
    main()
