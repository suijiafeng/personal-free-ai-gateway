import pytest
from gateway import bootstrap


def test_second_app_is_rejected_without_touching_native_globals(monkeypatch):
    monkeypatch.setattr(bootstrap,'_APP_CREATED',False)
    calls=[]
    expected=object()
    def construct(*args):
        calls.append(args)
        return expected
    monkeypatch.setattr(bootstrap,'_build_app',construct)
    assert bootstrap.build_app('first.yaml') is expected
    with pytest.raises(RuntimeError,match='fresh process'):
        bootstrap.build_app('second.yaml')
    assert calls==[('first.yaml',None)]


def test_unsuccessful_construction_does_not_claim_created_instance(monkeypatch):
    monkeypatch.setattr(bootstrap,'_APP_CREATED',False)
    def failed(*args):raise RuntimeError('synthetic invalid configuration')
    monkeypatch.setattr(bootstrap,'_build_app',failed)
    with pytest.raises(RuntimeError,match='synthetic'):
        bootstrap.build_app('invalid.yaml')
    assert bootstrap._APP_CREATED is False
