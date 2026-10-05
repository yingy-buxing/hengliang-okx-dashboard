"""用当前 Windows 用户的凭据管理器保存通用凭据，不写明文文件。"""
import ctypes as C

DWORD=C.c_uint32
class FILETIME(C.Structure):
    _fields_=[('low',DWORD),('high',DWORD)]
class CREDENTIALW(C.Structure):
    _fields_=[('Flags',DWORD),('Type',DWORD),('TargetName',C.c_wchar_p),
              ('Comment',C.c_wchar_p),('LastWritten',FILETIME),
              ('CredentialBlobSize',DWORD),('CredentialBlob',C.POINTER(C.c_ubyte)),
              ('Persist',DWORD),('AttributeCount',DWORD),('Attributes',C.c_void_p),
              ('TargetAlias',C.c_wchar_p),('UserName',C.c_wchar_p)]
PCREDENTIAL=C.POINTER(CREDENTIALW)
NOT_FOUND=1168
MAX_BLOB=2560

def native():
    try:lib=C.WinDLL('Advapi32.dll',use_last_error=True)
    except (AttributeError,OSError):raise RuntimeError('Windows 凭据管理器不可用，请在普通 Windows 用户会话运行') from None
    lib.CredReadW.argtypes=[C.c_wchar_p,DWORD,DWORD,C.POINTER(PCREDENTIAL)];lib.CredReadW.restype=C.c_int32
    lib.CredWriteW.argtypes=[PCREDENTIAL,DWORD];lib.CredWriteW.restype=C.c_int32
    lib.CredDeleteW.argtypes=[C.c_wchar_p,DWORD,DWORD];lib.CredDeleteW.restype=C.c_int32
    lib.CredFree.argtypes=[C.c_void_p];lib.CredFree.restype=None
    return lib

def check(ok):
    if not ok:raise RuntimeError('Windows 凭据管理器操作失败，请在已登录的本机用户会话重试')

def read(target):
    lib=native();ptr=PCREDENTIAL()
    if not lib.CredReadW(target,1,0,C.byref(ptr)):
        if C.get_last_error()==NOT_FOUND:return None
        check(False)
    try:
        value=ptr.contents
        if value.CredentialBlobSize>MAX_BLOB or (value.CredentialBlobSize and not value.CredentialBlob):
            raise RuntimeError('保存的 Windows 账户数据无效，请忘记账户后重新保存')
        return C.string_at(value.CredentialBlob,value.CredentialBlobSize)
    finally:lib.CredFree(ptr)

def write(target,payload):
    if len(payload)>MAX_BLOB:raise RuntimeError('API 凭据超过 Windows 安全保存容量，请缩短口令或仅在当前会话连接')
    buffer=(C.c_ubyte*len(payload)).from_buffer_copy(payload)
    entry=CREDENTIALW();entry.Type=1;entry.TargetName=target;entry.Comment='衡量 · OKX 只读账户'
    entry.CredentialBlobSize=len(payload);entry.CredentialBlob=C.cast(buffer,C.POINTER(C.c_ubyte))
    entry.Persist=2  # CRED_PERSIST_LOCAL_MACHINE：同一用户后续登录仍可读取，不跨设备漫游。
    entry.UserName='saved-account'
    check(native().CredWriteW(C.byref(entry),0))

def delete(target):
    if not native().CredDeleteW(target,1,0):
        if C.get_last_error()!=NOT_FOUND:check(False)
