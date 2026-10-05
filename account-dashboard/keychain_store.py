"""通过 macOS 钥匙串或 Windows 凭据管理器保存本机账户。"""
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys

SERVICE = ('com.hengliang.okx.' + hashlib.sha256(str(Path(__file__).resolve().parent).encode()).hexdigest()[:16]).encode()
ACCOUNT = b'saved-account'
NOT_FOUND = -25300


def framework():
    if sys.platform != 'darwin': raise RuntimeError('保存账户支持 macOS 钥匙串或 Windows 凭据管理器')
    lib = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
    lib.SecKeychainFindGenericPassword.argtypes = [C.c_void_p,C.c_uint32,C.c_char_p,C.c_uint32,C.c_char_p,C.POINTER(C.c_uint32),C.POINTER(C.c_void_p),C.POINTER(C.c_void_p)]
    lib.SecKeychainAddGenericPassword.argtypes = [C.c_void_p,C.c_uint32,C.c_char_p,C.c_uint32,C.c_char_p,C.c_uint32,C.c_void_p,C.POINTER(C.c_void_p)]
    lib.SecKeychainItemModifyAttributesAndData.argtypes = [C.c_void_p,C.c_void_p,C.c_uint32,C.c_void_p]
    lib.SecKeychainItemDelete.argtypes = [C.c_void_p]
    lib.SecKeychainItemFreeContent.argtypes = [C.c_void_p,C.c_void_p]
    return lib


def release(item):
    if item:
        cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cf.CFRelease.argtypes = [C.c_void_p];cf.CFRelease(item)


def check(status):
    if status: raise RuntimeError('钥匙串操作未完成，请允许本机后台访问，或解锁登录钥匙串后重试')


def find(with_data=False):
    lib=framework();item=C.c_void_p();size=C.c_uint32();data=C.c_void_p()
    code=lib.SecKeychainFindGenericPassword(None,len(SERVICE),SERVICE,len(ACCOUNT),ACCOUNT,
        C.byref(size) if with_data else None,C.byref(data) if with_data else None,C.byref(item))
    if code == NOT_FOUND:return lib,None,None
    check(code)
    try: value=C.string_at(data,size.value) if with_data else None
    finally:
        if data:lib.SecKeychainItemFreeContent(None,data)
    return lib,item,value


def mac_exists():
    _,item,_=find();found=bool(item);release(item);return found


def mac_save(credentials):
    payload=json.dumps(credentials,ensure_ascii=False).encode();lib,item,_=find()
    try:
        if item:check(lib.SecKeychainItemModifyAttributesAndData(item,None,len(payload),payload))
        else:
            new=C.c_void_p()
            check(lib.SecKeychainAddGenericPassword(None,len(SERVICE),SERVICE,len(ACCOUNT),ACCOUNT,len(payload),payload,C.byref(new)))
            release(new)
    finally:release(item)


def mac_load():
    _,item,value=find(True)
    if not item:raise RuntimeError('尚未保存账户，请先填写 API 并勾选记住此账户')
    try:
        result=json.loads(value)
        if not isinstance(result,dict) or any(not isinstance(result.get(k),str) or not result[k] for k in ['key','secret','passphrase','site']):
            raise RuntimeError('保存的账户信息无效，请忘记账户后重新保存')
        return result
    finally:release(item)


def mac_delete():
    lib,item,_=find()
    try:
        if item:check(lib.SecKeychainItemDelete(item))
    finally:release(item)


def storage_info():
    if sys.platform=='darwin':return {'name':'macOS 钥匙串','supported':True}
    if sys.platform=='win32':return {'name':'Windows 凭据管理器','supported':True}
    return {'name':'系统凭据存储','supported':False}

def target():return SERVICE.decode('ascii')+'.'+ACCOUNT.decode('ascii')

def validate(value):
    try:result=json.loads(value)
    except (ValueError,UnicodeError,TypeError):raise RuntimeError('保存的账户信息无效，请忘记账户后重新保存') from None
    if not isinstance(result,dict) or any(not isinstance(result.get(k),str) or not result[k] for k in ['key','secret','passphrase','site']):
        raise RuntimeError('保存的账户信息无效，请忘记账户后重新保存')
    return result

def exists():
    if sys.platform=='win32':
        import windows_credentials as win
        return win.read(target()) is not None
    return mac_exists()

def save(credentials):
    if sys.platform=='win32':
        import windows_credentials as win
        payload=json.dumps(credentials,ensure_ascii=False,separators=(',',':')).encode('utf-8');validate(payload)
        return win.write(target(),payload)
    return mac_save(credentials)

def load():
    if sys.platform=='win32':
        import windows_credentials as win
        value=win.read(target())
        if value is None:raise RuntimeError('尚未保存账户，请先填写 API 并勾选记住此账户')
        return validate(value)
    return mac_load()

def delete():
    if sys.platform=='win32':
        import windows_credentials as win
        return win.delete(target())
    return mac_delete()
