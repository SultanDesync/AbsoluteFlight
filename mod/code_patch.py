"""Code swaps through the pinned bus DLL (T100): AbsoluteInputBus_PatchCode suspends every other thread,
checks no instruction pointer is inside the bytes, compares, writes, reads back, flushes and resumes, all
in native code (input_bus/src/codepatch.rs). Replaces the tools/patch_call.py subprocess, which needed
the dev venv. ctypes releases the GIL for the call, so a suspended Python thread can't block it.
"""
import ctypes as C
import hashlib
from pathlib import Path

EXPORT='AbsoluteInputBus_PatchCode'
ERRORS={-1:'bad arguments',-2:'code ownership/expected bytes mismatch',-3:'unable to quiesce patch boundary',
        -4:'Windows call failed',-5:'code readback mismatch',-6:'native panic'}


class NativePatcher:
    def __init__(self,function):self.function=function

    def __call__(self,site,expected,replacement):
        """Swap expected -> replacement at site in this process. Returns a one-line summary like patch_call's."""
        if len(expected)!=len(replacement) or not 1<=len(expected)<=16:
            raise ValueError('expected and replacement must be the same short length')
        threads=C.c_uint32()
        code=self.function(site,bytes(expected),bytes(replacement),len(expected),C.byref(threads))
        if code<0:raise RuntimeError(f'native patch {site:#x} failed: {ERRORS.get(code,code)}')
        return '{"ok": true, "already": true}' if code==1 else f'{{"ok": true, "threads": {threads.value}}}'


def load(bus_config):
    """The pinned DLL's patcher, or None when that DLL predates the export (rebuild input_bus/)."""
    dll=Path(bus_config['dll']).resolve(strict=True)
    if hashlib.sha256(dll.read_bytes()).hexdigest()!=bus_config['dll_sha256']:raise RuntimeError('Pinned bus binary changed')
    library=C.CDLL(str(dll))
    if not hasattr(library,EXPORT):return None
    function=getattr(library,EXPORT)
    function.argtypes=[C.c_size_t,C.c_char_p,C.c_char_p,C.c_uint32,C.POINTER(C.c_uint32)];function.restype=C.c_int32
    return NativePatcher(function)
