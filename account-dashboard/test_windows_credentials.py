"""跨平台模拟 Win32 凭据 API；Windows 原生验证需在 Windows 再运行本文件。"""
import ctypes as C
import secrets
import sys
import unittest
from unittest.mock import patch
import windows_credentials as win
import keychain_store as store

class FakeNative:
    def __init__(self):self.values={};self.error=0;self.freed=0;self.fail=False
    def CredWriteW(self,ptr,flags):
        if self.fail:self.error=5;return 0
        entry=C.cast(ptr,win.PCREDENTIAL).contents
        assert entry.Type==1 and entry.Persist==2 and flags==0
        self.values[entry.TargetName]=C.string_at(entry.CredentialBlob,entry.CredentialBlobSize);return 1
    def CredReadW(self,target,kind,flags,out):
        if self.fail:self.error=5;return 0
        if target not in self.values:self.error=win.NOT_FOUND;return 0
        data=self.values[target];self.buffer=(C.c_ubyte*len(data)).from_buffer_copy(data)
        self.entry=win.CREDENTIALW();self.entry.CredentialBlobSize=len(data);self.entry.CredentialBlob=self.buffer
        self.ptr=C.pointer(self.entry);C.cast(out,C.POINTER(win.PCREDENTIAL))[0]=self.ptr;return 1
    def CredDeleteW(self,target,kind,flags):
        if self.fail:self.error=5;return 0
        if target not in self.values:self.error=win.NOT_FOUND;return 0
        del self.values[target];return 1
    def CredFree(self,ptr):self.freed+=1

class WindowsCredentialTest(unittest.TestCase):
    def setUp(self):
        self.fake=FakeNative();self.patches=[patch.object(store.sys,'platform','win32'),patch.object(win,'native',return_value=self.fake),patch.object(C,'get_last_error',side_effect=lambda:self.fake.error,create=True)]
        for p in self.patches:p.start()
        self.value={'key':'fixture','secret':'fixture-secret','passphrase':'测试口令','site':'global'}
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
    def test_save_replace_load_forget(self):
        self.assertEqual(store.storage_info()['name'],'Windows 凭据管理器');self.assertFalse(store.exists())
        store.save(self.value);self.assertTrue(store.exists());self.assertEqual(store.load(),self.value)
        changed=dict(self.value,passphrase='更新后的口令');store.save(changed);self.assertEqual(store.load(),changed)
        self.assertGreater(self.fake.freed,0);store.delete();store.delete();self.assertFalse(store.exists())
        with self.assertRaises(RuntimeError):store.load()
    def test_denial_is_not_treated_as_missing(self):
        self.fake.fail=True
        for action in [store.exists,store.load,store.delete,lambda:store.save(self.value)]:
            with self.assertRaisesRegex(RuntimeError,'操作失败'):action()
    def test_capacity_and_invalid_data(self):
        with self.assertRaisesRegex(RuntimeError,'容量'):store.save(dict(self.value,passphrase='中'*900))
        self.assertFalse(self.fake.values)
        self.fake.values[store.target()]=b'broken'
        with self.assertRaisesRegex(RuntimeError,'信息无效'):store.load()
        self.assertEqual(self.fake.freed,1)
    def test_windows_structure_abi(self):
        self.assertEqual(C.sizeof(win.DWORD),4);self.assertEqual(C.sizeof(win.FILETIME),8)
        self.assertEqual(C.sizeof(win.CREDENTIALW),80 if C.sizeof(C.c_void_p)==8 else 52)

@unittest.skipUnless(sys.platform=='win32','需要 Windows 原生系统')
class WindowsNativeTest(unittest.TestCase):
    def test_real_round_trip_in_isolated_entry(self):
        target='com.hengliang.okx.test.'+secrets.token_hex(16)
        try:
            self.assertIsNone(win.read(target));win.write(target,b'fixture-only');self.assertEqual(win.read(target),b'fixture-only')
            win.write(target,'测试'.encode());self.assertEqual(win.read(target),'测试'.encode());win.delete(target);self.assertIsNone(win.read(target))
        finally:win.delete(target)

if __name__=='__main__':unittest.main()
