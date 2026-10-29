"""D21 exact identity/offline gate for an explicitly enabled in-process adapter.

No game memory is read and no hook is installed. Runtime focus and qualified
admission are boolean callbacks; this gate cannot infer a safe flight fixture.
The current process identity and executable file identity become immutable on
the first successful check. Saved multiplayer settings are re-read each time.
"""
import ctypes as C
import hashlib
import math
import os
from pathlib import Path
import re
import threading
import xml.etree.ElementTree as ET

FILE_VERSION='183877'
STEAM_BUILDID='26295378'


class PreflightError(RuntimeError):
    """A required identity, offline, focus or runtime gate is unproven."""


def _absolute(value,name):
    if not isinstance(value,(str,os.PathLike)) or not Path(value).is_absolute():
        raise PreflightError(name+' must be an absolute path')
    return Path(value).resolve()


def _fingerprint(stat):
    # Python 3.14 exposes NTFS change time through ctime; a just-closed write
    # can make path stat/fstat disagree there. Birth time is the stable Windows
    # identity adjunct (3.11 already reports birth time as ctime on Windows).
    created=getattr(stat,'st_birthtime_ns',stat.st_ctime_ns)
    return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,created)


def _stable_bytes(path,maximum):
    before=_fingerprint(path.stat())
    if before[2]>maximum:raise PreflightError('preflight file exceeds size limit: '+str(path))
    with path.open('rb') as stream:
        opened=_fingerprint(os.fstat(stream.fileno()))
        data=stream.read(maximum+1)
        after=_fingerprint(os.fstat(stream.fileno()))
    if len(data)>maximum or before!=opened or opened!=after or after!=_fingerprint(path.stat()):
        raise PreflightError('preflight file changed while reading: '+str(path))
    return data


def _same_path(a,b):
    return os.path.normcase(str(Path(a).resolve()))==os.path.normcase(str(Path(b).resolve()))


def _windows_current_process():
    """Inspect only this process using kernel/token/version APIs, without pymem."""
    if os.name!='nt':raise PreflightError('in-process preflight requires Windows')
    k=C.WinDLL('kernel32',use_last_error=True)
    a=C.WinDLL('advapi32',use_last_error=True)
    v=C.WinDLL('version',use_last_error=True)
    for library,name,args,result in (
        (k,'GetCurrentProcess',[],C.c_void_p),(k,'GetCurrentThread',[],C.c_void_p),
        (k,'GetCurrentProcessId',[],C.c_uint32),(k,'CloseHandle',[C.c_void_p],C.c_int),
        (k,'LocalFree',[C.c_void_p],C.c_void_p),
        (k,'QueryFullProcessImageNameW',[C.c_void_p,C.c_uint32,C.c_wchar_p,C.POINTER(C.c_uint32)],C.c_int),
        (k,'GetProcessTimes',[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p],C.c_int),
        (a,'OpenProcessToken',[C.c_void_p,C.c_uint32,C.POINTER(C.c_void_p)],C.c_int),
        (a,'OpenThreadToken',[C.c_void_p,C.c_uint32,C.c_int,C.POINTER(C.c_void_p)],C.c_int),
        (a,'GetTokenInformation',[C.c_void_p,C.c_uint32,C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32)],C.c_int),
        (a,'ConvertSidToStringSidW',[C.c_void_p,C.POINTER(C.c_wchar_p)],C.c_int),
        (v,'GetFileVersionInfoSizeW',[C.c_wchar_p,C.POINTER(C.c_uint32)],C.c_uint32),
        (v,'GetFileVersionInfoW',[C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_void_p],C.c_int),
        (v,'VerQueryValueW',[C.c_void_p,C.c_wchar_p,C.POINTER(C.c_void_p),C.POINTER(C.c_uint32)],C.c_int),
    ):
        fn=getattr(library,name);fn.argtypes=args;fn.restype=result

    def check(value):
        if not value:raise C.WinError(C.get_last_error())
        return value

    def token_sid(token):
        size=C.c_uint32()
        a.GetTokenInformation(token,1,None,0,C.byref(size))  # TOKEN_USER
        if not 1<=size.value<=65536:raise PreflightError('invalid process token information')
        buffer=C.create_string_buffer(size.value)
        check(a.GetTokenInformation(token,1,buffer,size.value,C.byref(size)))
        text=C.c_wchar_p()
        check(a.ConvertSidToStringSidW(C.c_void_p.from_buffer(buffer).value,C.byref(text)))
        try:return text.value
        finally:k.LocalFree(C.cast(text,C.c_void_p))

    process=k.GetCurrentProcess()
    name=C.create_unicode_buffer(32768);length=C.c_uint32(len(name))
    check(k.QueryFullProcessImageNameW(process,0,name,C.byref(length)))
    birth,exit_time,kernel,user=(C.c_uint64() for _ in range(4))
    check(k.GetProcessTimes(process,C.byref(birth),C.byref(exit_time),C.byref(kernel),C.byref(user)))
    process_token=C.c_void_p();thread_token=C.c_void_p()
    check(a.OpenProcessToken(process,8,C.byref(process_token)))
    try:
        owner_sid=token_sid(process_token)
        if a.OpenThreadToken(k.GetCurrentThread(),8,1,C.byref(thread_token)):
            try:current_user_sid=token_sid(thread_token)
            finally:k.CloseHandle(thread_token)
        elif C.get_last_error()==1008:  # ERROR_NO_TOKEN: no impersonation
            current_user_sid=owner_sid
        else:raise C.WinError(C.get_last_error())
    finally:k.CloseHandle(process_token)
    ignored=C.c_uint32()
    version_size=v.GetFileVersionInfoSizeW(name.value,C.byref(ignored))
    if not 1<=version_size<=16*1024*1024:raise PreflightError('missing executable version resource')
    version=C.create_string_buffer(version_size)
    check(v.GetFileVersionInfoW(name.value,0,version_size,version))
    pointer=C.c_void_p();size=C.c_uint32()
    check(v.VerQueryValueW(version,r'\VarFileInfo\Translation',C.byref(pointer),C.byref(size)))
    if not pointer.value or not 4<=size.value<=65536 or size.value%4:
        raise PreflightError('invalid executable version translations')
    translations=(C.c_uint16*(size.value//2)).from_address(pointer.value)
    versions=[]
    for index in range(0,len(translations),2):
        key=f'\\StringFileInfo\\{translations[index]:04x}{translations[index+1]:04x}\\FileVersion'
        value=C.c_void_p();characters=C.c_uint32()
        check(v.VerQueryValueW(version,key,C.byref(value),C.byref(characters)))
        if not value.value or not 1<=characters.value<=256:
            raise PreflightError('invalid executable FileVersion string')
        versions.append(C.wstring_at(value.value,characters.value).rstrip('\0'))
    if len(set(versions))!=1:raise PreflightError('ambiguous executable FileVersion')
    return {'pid':int(k.GetCurrentProcessId()),'creation_time':str(birth.value),
            'exe':name.value,'owner_sid':owner_sid,'current_user_sid':current_user_sid,
            'file_version':versions[0]}


def _windows_focused():
    if os.name!='nt':raise PreflightError('foreground identity requires Windows')
    user=C.WinDLL('user32',use_last_error=True)
    user.GetForegroundWindow.argtypes=[];user.GetForegroundWindow.restype=C.c_void_p
    user.GetWindowThreadProcessId.argtypes=[C.c_void_p,C.POINTER(C.c_uint32)]
    user.GetWindowThreadProcessId.restype=C.c_uint32
    window=user.GetForegroundWindow();pid=C.c_uint32()
    return bool(window and user.GetWindowThreadProcessId(window,C.byref(pid)) and pid.value==os.getpid())


class Preflight:
    """`Preflight(expected, inspect=..., focused=..., admitted=...).check()`.

    Expected keys: absolute ``exe``, ``file_version``, ``steam_buildid`` and
    trusted ``sha256``; optional absolute ``manifest`` and ``settings`` paths.
    ``inspect()`` supplies pid/creation_time/exe/owner_sid/current_user_sid/
    file_version for pure tests. ``admitted()`` must positively return True;
    it belongs to the qualified runtime, never to a pipe request.
    """
    def __init__(self,expected,*,inspect=None,focused=None,admitted=None):
        allowed={'exe','file_version','steam_buildid','sha256','manifest','settings'}
        if not isinstance(expected,dict) or set(expected)-allowed:
            raise PreflightError('invalid trusted preflight configuration')
        self.exe=_absolute(expected.get('exe'),'exe')
        if self.exe.name.casefold()!='nms.exe':raise PreflightError('expected executable must be NMS.exe')
        if expected.get('file_version')!=FILE_VERSION or expected.get('steam_buildid')!=STEAM_BUILDID:
            raise PreflightError('unsupported trusted game build')
        sha=expected.get('sha256')
        if not isinstance(sha,str) or re.fullmatch(r'[0-9a-fA-F]{64}',sha) is None:
            raise PreflightError('trusted full executable SHA256 required')
        self.sha256=sha.lower()
        self.manifest=_absolute(expected.get('manifest',self.exe.parent.parent.parent.parent/'appmanifest_275850.acf'),'manifest')
        self.settings=_absolute(expected.get('settings',self.exe.parent/'SETTINGS'/'GCUSERSETTINGSDATA.MXML'),'settings')
        self.inspect=inspect or _windows_current_process
        self.focused=focused or _windows_focused
        self.admitted=admitted
        self._identity=None;self._exe_file=None;self._lock=threading.RLock()
        self.latest={'ok':False,'error':'preflight not checked'}

    def _process(self):
        data=self.inspect()
        required={'pid','creation_time','exe','owner_sid','current_user_sid','file_version'}
        if not isinstance(data,dict) or not required<=set(data):raise PreflightError('incomplete process identity')
        if type(data['pid']) is not int or data['pid']!=os.getpid():raise PreflightError('not the current process')
        birth=data['creation_time']
        if (isinstance(birth,bool) or not isinstance(birth,(str,int,float)) or
                (isinstance(birth,str) and (not birth.isdecimal() or int(birth)<=0)) or
                (isinstance(birth,(int,float)) and (not math.isfinite(birth) or birth<=0))):
            raise PreflightError('invalid process creation time')
        actual=_absolute(data['exe'],'current process exe')
        if not _same_path(actual,self.exe):raise PreflightError('current executable path differs from trusted path')
        if data['file_version']!=FILE_VERSION:raise PreflightError('executable FileVersion mismatch')
        for key in ('owner_sid','current_user_sid'):
            if not isinstance(data[key],str) or re.fullmatch(r'S-\d+(?:-\d+)+',data[key]) is None:
                raise PreflightError('invalid process owner SID')
        if data['owner_sid']!=data['current_user_sid']:raise PreflightError('process owner differs from current user')
        identity=(data['pid'],birth,os.path.normcase(str(actual)),data['owner_sid'],data['current_user_sid'])
        if self._identity is not None and identity!=self._identity:raise PreflightError('process identity changed')
        return data,identity

    def _hash(self):
        before=_fingerprint(self.exe.stat())
        if self._exe_file is not None:
            if before!=self._exe_file:raise PreflightError('executable file identity or metadata changed')
            return before
        digest=hashlib.sha256()
        with self.exe.open('rb') as stream:
            opened=_fingerprint(os.fstat(stream.fileno()))
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            after=_fingerprint(os.fstat(stream.fileno()))
        if before!=opened or opened!=after or after!=_fingerprint(self.exe.stat()):
            raise PreflightError('executable changed while hashing')
        if digest.hexdigest()!=self.sha256:raise PreflightError('executable SHA256 mismatch')
        return before

    def check(self):
        """Recheck before every session/write. Any exception fails closed."""
        with self._lock:
            try:
                process,identity=self._process()
                exe_file=self._hash()
                manifest=_stable_bytes(self.manifest,1024*1024).decode('utf-8-sig')
                buildids=re.findall(r'"buildid"\s+"([^"]*)"',manifest)
                if buildids!=[STEAM_BUILDID]:raise PreflightError('Steam buildid missing, ambiguous or mismatched')
                xml=_stable_bytes(self.settings,8*1024*1024)
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                    raise PreflightError('unsupported settings XML declarations')
                root=ET.fromstring(xml)
                multiplayer=[element for element in root.iter('Property') if element.get('name')=='Multiplayer']
                if len(multiplayer)!=1 or multiplayer[0].get('value')!='false':
                    raise PreflightError('exactly one saved Multiplayer=false property required')
                if self.focused() is not True:raise PreflightError('NMS is not the current foreground process')
                if self.admitted is None or self.admitted() is not True:
                    raise PreflightError('qualified runtime admission required')
                # Recheck metadata after callbacks: neither a replaced file nor a
                # delayed callback may silently establish a stale hash cache.
                if exe_file!=_fingerprint(self.exe.stat()):raise PreflightError('executable changed during preflight')
                self._identity=identity;self._exe_file=exe_file
                self.latest={'ok':True,'pid':process['pid'],'creation_time':process['creation_time'],
                             'exe':str(self.exe),'owner_sid':process['owner_sid'],
                             'current_user_sid':process['current_user_sid'],'file_version':FILE_VERSION,
                             'steam_buildid':STEAM_BUILDID,'sha256':self.sha256,
                             'multiplayer':False,'focused':True,'admitted':True,
                             'exe_size':exe_file[2],'exe_mtime_ns':exe_file[3],'exe_file_id':str(exe_file[1])}
                return dict(self.latest)
            except Exception as error:
                self.latest={'ok':False,'error':str(error)}
                if isinstance(error,PreflightError):raise
                raise PreflightError(str(error)) from error

    def status(self):
        return dict(self.latest)
