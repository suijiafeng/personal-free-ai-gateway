"""Root transition contract tests use mocks; they are not actual Linux setuid proof."""
import dataclasses
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import pytest
from deploy import privilege_drop as drop
from deploy import reviewed_start as bridge


def private(tmp_path):
    p=tmp_path/'key';p.write_text('synthetic-only\n');p.chmod(0o600);return p


def status(uid=10001,gid=10001,caps='0000000000000000',nnp='1',groups=''):
    return {'Uid':' '.join([str(uid)]*4),'Gid':' '.join([str(gid)]*4),'Groups':groups,
            'CapEff':caps,'CapPrm':caps,'CapInh':'0','CapAmb':'0','CapBnd':'c0','NoNewPrivs':nnp}


def test_read_only_as_exact_owner_then_restore_root(tmp_path,monkeypatch):
    p=private(tmp_path);owner=p.stat().st_uid;calls=[]
    monkeypatch.setattr(bridge.os,'geteuid',lambda:0)
    monkeypatch.setattr(bridge.os,'seteuid',lambda uid:calls.append(uid))
    assert bridge.read_secret_as_owner(p)=='synthetic-only'
    assert calls==[owner,0]


def test_read_failure_still_restores_root(tmp_path,monkeypatch):
    p=private(tmp_path);calls=[]
    monkeypatch.setattr(bridge.os,'geteuid',lambda:0)
    monkeypatch.setattr(bridge.os,'seteuid',lambda uid:calls.append(uid))
    def fail(*a,**kw):raise OSError('synthetic read failure')
    monkeypatch.setattr(bridge,'read_secret',fail)
    with pytest.raises(OSError):bridge.read_secret_as_owner(p)
    assert calls==[p.stat().st_uid,0]


def test_failure_to_restore_root_is_fatal(tmp_path,monkeypatch):
    p=private(tmp_path)
    monkeypatch.setattr(bridge.os,'geteuid',lambda:0)
    def seteuid(uid):
        if uid==0:raise PermissionError('restore failed')
    monkeypatch.setattr(bridge.os,'seteuid',seteuid)
    with pytest.raises(PermissionError):bridge.read_secret_as_owner(p)


@pytest.mark.parametrize('mode',[0o644,0o640,0o660,0o000])
def test_insecure_or_unreadable_host_mode_rejected_before_seteuid(tmp_path,monkeypatch,mode):
    p=private(tmp_path);p.chmod(mode);calls=[]
    monkeypatch.setattr(bridge.os,'geteuid',lambda:0)
    monkeypatch.setattr(bridge.os,'seteuid',lambda uid:calls.append(uid))
    with pytest.raises(ValueError):bridge.read_secret_as_owner(p)
    assert calls==[]


def test_secret_identity_drift_rejected(tmp_path):
    p=private(tmp_path);expected=bridge.secret_identity(p.stat());p.chmod(0o400)
    with pytest.raises(ValueError):bridge.read_secret(p,expected)


def test_owner_drift_rejected_before_read(tmp_path,monkeypatch):
    p=private(tmp_path);expected=list(bridge.secret_identity(p.stat()));expected[2]+=1
    with pytest.raises(ValueError):bridge.read_secret(p,tuple(expected))


def test_ctime_drift_during_read_rejected(tmp_path,monkeypatch):
    p=private(tmp_path);original=os.fstat;count=0
    def fstat(fd):
        nonlocal count
        value=original(fd);count+=1
        if count==1:return value
        fields={n:getattr(value,n) for n in ['st_mode','st_dev','st_ino','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns']}
        fields['st_ctime_ns']+=1;return SimpleNamespace(**fields)
    monkeypatch.setattr(bridge.os,'fstat',fstat)
    with pytest.raises(ValueError):bridge.read_secret(p)


def test_drop_order_clears_groups_ids_caps_and_verifies(monkeypatch):
    calls=[]
    monkeypatch.setattr(drop,'check_bootstrap_capabilities',lambda:calls.append('check'))
    monkeypatch.setattr(drop,'_prctl',lambda *args:calls.append(('prctl',)+args))
    monkeypatch.setattr(drop.os,'setgroups',lambda groups:calls.append(('groups',groups)))
    monkeypatch.setattr(drop.os,'setresgid',lambda *ids:calls.append(('gid',)+ids))
    monkeypatch.setattr(drop.os,'setresuid',lambda *ids:calls.append(('uid',)+ids))
    monkeypatch.setattr(drop,'clear_capabilities',lambda:calls.append('clear'))
    monkeypatch.setattr(drop,'verify_application_identity',lambda:calls.append('verify'))
    drop.drop_to_application()
    assert calls==['check',('prctl',38,1),('prctl',8,0),('prctl',47,4),
                   ('groups',[]),('gid',10001,10001,10001),('uid',10001,10001,10001),'clear','verify']


@pytest.mark.parametrize('field,value',[('Uid','10001 0 10001 10001'),('Gid','0 0 0 0'),
    ('Groups','0'),('CapEff','80'),('CapPrm','80'),('CapInh','80'),('CapAmb','80'),
    ('CapBnd','1ff'),('NoNewPrivs','0')])
def test_identity_or_capability_leak_refuses_exec(field,value):
    s=status();s[field]=value
    with pytest.raises(RuntimeError):drop.verify_application_identity(s)


def test_verified_application_identity_is_unprivileged():
    assert drop.verify_application_identity(status())=={'uid':10001,'gid':10001,
        'supplementary_groups':[],'effective_permitted_inheritable_ambient_caps':0,'no_new_privileges':True}


def test_bootstrap_rejects_unexpected_dac_override(monkeypatch):
    monkeypatch.setattr(drop.os,'geteuid',lambda:0)
    s=status(uid=0,gid=0,caps='c2');s['CapBnd']='c2'
    with pytest.raises(RuntimeError):drop.check_bootstrap_capabilities(s)


def test_main_never_execs_after_drop_failure(monkeypatch,capsys):
    from gateway import config
    monkeypatch.setattr(bridge.sys,'argv',['reviewed_start.py'])
    monkeypatch.setattr(drop,'check_bootstrap_capabilities',lambda:None)
    monkeypatch.setattr(config,'load_policy',lambda _:None)
    monkeypatch.setattr(bridge,'prepare_environment',lambda *a,**kw:{'GROQ_API_KEY':'SENSITIVE_SENTINEL'})
    def fail():raise PermissionError('SENSITIVE_SENTINEL')
    monkeypatch.setattr(drop,'drop_to_application',fail)
    monkeypatch.setattr(bridge.os,'execve',lambda *a:pytest.fail('exec must not run'))
    assert bridge.main()==2
    assert 'SENSITIVE_SENTINEL' not in capsys.readouterr().err


def test_readability_probe_drops_then_exits_without_exec(monkeypatch,capsys):
    from gateway import config
    monkeypatch.setattr(bridge.sys,'argv',['reviewed_start.py','--check-bootstrap'])
    monkeypatch.setattr(drop,'check_bootstrap_capabilities',lambda:None)
    monkeypatch.setattr(config,'load_policy',lambda _:None)
    monkeypatch.setattr(bridge,'prepare_environment',lambda *a,**kw:{'GROQ_API_KEY':'SENSITIVE_SENTINEL'})
    monkeypatch.setattr(drop,'drop_to_application',lambda:drop.verify_application_identity(status()))
    monkeypatch.setattr(bridge.os,'execve',lambda *a:pytest.fail('probe must not start app'))
    assert bridge.main()==0
    output=capsys.readouterr().out
    assert '10001' in output and 'SENSITIVE_SENTINEL' not in output and 'bootstrap_readable' in output


def test_healthcheck_drops_before_network_and_does_not_read_secrets(monkeypatch):
    import urllib.request
    calls=[]
    monkeypatch.setattr(bridge.sys,'argv',['reviewed_start.py','--healthcheck'])
    monkeypatch.setattr(drop,'check_bootstrap_capabilities',lambda:calls.append('check'))
    monkeypatch.setattr(bridge,'prepare_environment',lambda *a,**kw:pytest.fail('healthcheck must not read keys'))
    def dropped():
        calls.append('drop');return drop.verify_application_identity(status())
    monkeypatch.setattr(drop,'drop_to_application',dropped)
    class Opener:
        def open(self,url,timeout):
            assert calls==['check','drop'];calls.append('network')
            assert url=='http://127.0.0.1:4000/health/liveliness'
            return SimpleNamespace(read=lambda:b'ok')
    monkeypatch.setattr(urllib.request,'build_opener',lambda *a:Opener())
    assert bridge.main()==0
    assert calls==['check','drop','network']


def test_secret_bootstrap_failure_never_drops_or_execs_app(monkeypatch,capsys):
    from gateway import config
    monkeypatch.setattr(bridge.sys,'argv',['reviewed_start.py'])
    monkeypatch.setattr(drop,'check_bootstrap_capabilities',lambda:None)
    monkeypatch.setattr(config,'load_policy',lambda _:None)
    def fail(*a,**kw):raise PermissionError('SENSITIVE_SENTINEL: restore root failed')
    monkeypatch.setattr(bridge,'prepare_environment',fail)
    monkeypatch.setattr(drop,'drop_to_application',lambda:pytest.fail('must stop on restoration failure'))
    monkeypatch.setattr(bridge.os,'execve',lambda *a:pytest.fail('exec must not run'))
    assert bridge.main()==2
    assert 'SENSITIVE_SENTINEL' not in capsys.readouterr().err
