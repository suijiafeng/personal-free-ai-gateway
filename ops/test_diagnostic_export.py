from pathlib import Path
import json
import os
import sys
from urllib.parse import parse_qs,urlsplit
import pytest
sys.path.insert(0,str(Path(__file__).parent))
import diagnostic_export as export
from gateway_ops import OpsError


def page(data,cursor=None):
    return {'schema_version':1,'contains_content':False,'order':'event_id_desc',
        'config_revision':'a'*64,'data':data,'next_cursor':cursor}


def test_export_walks_pagination_without_request_or_secret_material(monkeypatch):
    calls=[]
    def api(url,key,path):
        calls.append(path)
        query=parse_qs(urlsplit(path).query)
        if 'before_id' not in query:
            return page([{'event_id':4,'event':'request_finished','usage_source':'unknown'},
                         {'event_id':3,'event':'attempt_finished','usage_source':'unknown'}],3)
        return page([{'event_id':2,'event':'attempt_started','usage_source':'unknown'}])
    monkeypatch.setattr(export,'private_admin',api)
    result=export.collect('http://127.0.0.1:4101','private-admin',{'limit':2,'fallback':True})
    assert result['event_count']==3
    assert result['contains_content'] is False
    assert 'fallback=true' in calls[0]
    assert 'before_id=3' in calls[1]
    assert 'private-admin' not in json.dumps(result)


@pytest.mark.parametrize('invalid',[
    page([{'event_id':1,'prompt':'BODY'}]),
    page([{'event_id':1,'status':'private-admin'}]),
    page([{'event_id':2},{'event_id':2}]),
    page([{'event_id':2}],1),
    page([],2),
    {**page([]),'contains_content':True},
])
def test_export_rejects_unknown_fields_secret_echo_and_broken_cursor(monkeypatch,invalid):
    monkeypatch.setattr(export,'private_admin',lambda *args:invalid)
    with pytest.raises(OpsError):
        export.collect('http://127.0.0.1:4101','private-admin',{'limit':2})


def test_export_bound_is_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(export,'private_admin',lambda *args:page([{'event_id':2},{'event_id':1}]))
    with pytest.raises(OpsError):
        export.collect('http://127.0.0.1:4101','admin',{'limit':2},1)


def test_file_is_private_and_never_overwrites_or_follows_symlink(tmp_path):
    os.chmod(tmp_path,0o700)
    output=tmp_path/'metadata.json'
    export.write_export(output,{'data':[]})
    assert output.stat().st_mode&0o777==0o600
    assert json.loads(output.read_text())=={'data':[]}
    with pytest.raises(OpsError):export.write_export(output,{'replacement':True})
    link=tmp_path/'link.json';link.symlink_to(output)
    with pytest.raises(OpsError):export.write_export(link,{'replacement':True})
    assert json.loads(output.read_text())=={'data':[]}


def test_dry_run_does_not_read_credentials_or_write(monkeypatch,tmp_path,capsys):
    def forbidden(*args):raise AssertionError('Unexpected I/O')
    monkeypatch.setattr(export,'selected_target',forbidden)
    monkeypatch.setattr(export,'private_admin',forbidden)
    monkeypatch.setattr(export,'write_export',forbidden)
    assert export.main(['--env-file',str(tmp_path/'missing'), '--output',str(tmp_path/'export')])==0
    assert json.loads(capsys.readouterr().out)['executed'] is False
