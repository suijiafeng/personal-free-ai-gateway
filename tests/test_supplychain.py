"""Offline static integrity and credential-boundary tests; no provider calls."""
from pathlib import Path
from types import SimpleNamespace
import copy
import json
import shutil
import pytest
from deploy.reviewed_start import approved_hosts, prepare_environment, read_secret, validate_policy
from deploy.check_reviewed import check
from tools.supplychain_verify import verify

ROOT=Path(__file__).resolve().parents[1]


def policy(provider='groq', enabled=True, reason=None):
    domain, key = {'groq':('api.groq.com','GROQ_API_KEY'),
                   'gemini':('generativelanguage.googleapis.com','GEMINI_API_KEY')}[provider]
    d=SimpleNamespace(enabled=enabled,provider=provider,credential_env=key,api_base='https://'+domain)
    return SimpleNamespace(profile='production',deployments=[d],exclusion=lambda _:reason)


def test_default_offline_gate_and_integrity():
    assert check(ROOT)['passed']
    assert check(ROOT)['enabled_providers']==[]
    assert not check(ROOT)['activation_ready']
    assert verify(ROOT)['passed']


def test_default_hosts_deny_all():
    assert approved_hosts(ROOT/'deploy/egress/approved-hosts.txt')==set()


@pytest.mark.parametrize('host',['.groq.com','api.groq.com.evil.invalid','127.0.0.1','[::1]',
                               'api.groq.com:443','api.groq.com\napi.groq.com'])
def test_unsupported_egress_host_is_rejected(tmp_path,host):
    p=tmp_path/'hosts';p.write_text(host)
    with pytest.raises(ValueError):approved_hosts(p)


def test_enabled_policy_must_exactly_match_host_list():
    with pytest.raises(ValueError):validate_policy(policy(),set())
    with pytest.raises(ValueError):validate_policy(policy(),{'api.groq.com','generativelanguage.googleapis.com'})
    assert validate_policy(policy(),{'api.groq.com'})=={'groq'}


def test_expired_review_cannot_start():
    with pytest.raises(ValueError):validate_policy(policy(reason='expired'),{'api.groq.com'})


def test_nonstandard_credential_mapping_rejected():
    p=policy();p.deployments[0].credential_env='OTHER_KEY'
    with pytest.raises(ValueError):validate_policy(p,{'api.groq.com'})


def test_inherited_provider_key_is_never_fallback(tmp_path):
    hosts=tmp_path/'hosts';hosts.write_text('api.groq.com\n')
    with pytest.raises(FileNotFoundError):
        prepare_environment(policy(),hosts,tmp_path,{'GROQ_API_KEY':'inherited-key'})


def test_file_secret_takes_precedence_and_proxy_is_closed(tmp_path):
    hosts=tmp_path/'hosts';hosts.write_text('api.groq.com\n')
    (tmp_path/'groq_api_key').write_text('synthetic-only-file-value\n')
    (tmp_path/'groq_api_key').chmod(0o600)
    env=prepare_environment(policy(),hosts,tmp_path,{'GROQ_API_KEY':'wrong','GEMINI_API_KEY':'wrong',
          'HTTPS_PROXY':'http://evil.invalid:1','ALL_PROXY':'socks5://evil.invalid:2','NO_PROXY':'*'})
    assert env['GROQ_API_KEY']=='synthetic-only-file-value'
    assert 'GEMINI_API_KEY' not in env and 'ALL_PROXY' not in env
    assert env['HTTPS_PROXY']=='http://egress:3128'
    assert env['NO_PROXY']=='localhost,127.0.0.1,postgres'


def test_disabled_provider_secret_not_loaded(tmp_path):
    hosts=tmp_path/'hosts';hosts.write_text('deny-all.invalid\n')
    env=prepare_environment(policy(enabled=False),hosts,tmp_path,{'GROQ_API_KEY':'inherited'})
    assert 'GROQ_API_KEY' not in env


@pytest.mark.parametrize('content',['','value\nsecond','value\r\n','contains space','x'*4097])
def test_invalid_secret_rejected(tmp_path,content):
    p=tmp_path/'secret';p.write_text(content);p.chmod(0o600)
    with pytest.raises(ValueError):read_secret(p)


def test_symlink_secret_rejected(tmp_path):
    secret=tmp_path/'secret';secret.write_text('synthetic')
    link=tmp_path/'link';link.symlink_to(secret)
    with pytest.raises(OSError):read_secret(link)


def test_manifest_corruption_fails_offline_gate(tmp_path):
    for folder in ['deploy','evidence']:
        shutil.copytree(ROOT/folder,tmp_path/folder)
    shutil.copy(ROOT/'requirements.lock',tmp_path/'requirements.lock')
    p=tmp_path/'evidence/supply-chain/raw/python-index.json'
    p.write_bytes(p.read_bytes()+b' ')
    assert not verify(tmp_path)['passed']


def test_lock_drift_fails_offline_gate(tmp_path):
    for folder in ['deploy','evidence']:
        shutil.copytree(ROOT/folder,tmp_path/folder)
    (tmp_path/'requirements.lock').write_text((ROOT/'requirements.lock').read_text()+'\n# drift\n')
    assert not verify(tmp_path)['passed']


def test_ingress_does_not_duplicate_request_metadata_without_ttl():
    text=(ROOT/'deploy/nginx/nginx.conf').read_text()
    assert 'access_log off;' in text
    assert 'log_format ' not in text
    assert '$request_time' not in text and '$time_iso8601' not in text


def test_squid_uses_current_official_source_and_disabled_features():
    source=json.loads((ROOT/'evidence/supply-chain/squid-source.json').read_text())
    docker=(ROOT/'deploy/Dockerfile.egress').read_text()
    assert source['version']=='7.7' and source['archive_bytes_verified']
    assert source['source_url'] in docker
    assert '--checksum=sha256:'+source['archive_sha256'] in docker
    for flag in ('--disable-auth','--disable-icap-client','--disable-external-acl-helpers',
                 '--disable-htcp','--disable-snmp','--disable-cache-digests'):
        assert flag in docker
    assert 'squid=5.' not in docker


def test_squid_no_content_logging_or_tls_interception():
    lines=[s.strip() for s in (ROOT/'deploy/egress/squid.conf').read_text().splitlines()
           if s.strip() and not s.lstrip().startswith('#')]
    assert 'access_log none' in lines and 'cache_log /dev/null' in lines
    assert 'icp_port 0' in lines
    assert not any(s.startswith(('cache_peer','ssl_bump','auth_param','icap_service')) for s in lines)


def test_squid_runtime_evidence_matches_shipped_acl():
    import hashlib
    result=json.loads((ROOT/'evidence/supply-chain/squid-runtime-probe.json').read_text())
    assert result['passed'] and len(result['checks'])==19
    assert result['acl_sha256']==hashlib.sha256((ROOT/'deploy/egress/squid.conf').read_bytes()).hexdigest()
    assert result['test_harness_sha256']==hashlib.sha256((ROOT/'tests/run_squid_egress.py').read_bytes()).hexdigest()
    assert result['provider_calls']==0 and not result['container_network_tested']
