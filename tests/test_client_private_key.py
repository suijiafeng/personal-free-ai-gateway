"""Reference app can use the private key result without echoing/exporting it."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from examples.client import read_consumer_key


def test_private_key_file_can_be_used_without_env_or_printing(tmp_path,capsys):
    os.chmod(tmp_path,0o700)
    path=tmp_path/'app.json'
    path.write_text(json.dumps({'key':'sk-synthetic-local-file-only'}));path.chmod(0o600)
    assert read_consumer_key(path)=='sk-synthetic-local-file-only'
    assert capsys.readouterr().out==''
    path.chmod(0o644)
    with pytest.raises(ValueError):read_consumer_key(path)


def test_client_rejects_bad_key_file_without_env_fallback_or_traceback(tmp_path):
    root=Path(__file__).parents[1]
    env={**os.environ,'GATEWAY_API_KEY':'sk-existing-environment-must-not-be-used'}
    result=subprocess.run([sys.executable,str(root/'examples/client.py'),'--key-file',str(tmp_path/'missing')],
        env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode==2
    assert 'no environment fallback' in result.stderr
    assert 'Traceback' not in result.stderr
    assert env['GATEWAY_API_KEY'] not in result.stdout+result.stderr
