from copy import deepcopy
from pathlib import Path
import pytest
import yaml
from gateway.config import Policy, load_policy, deployment_native_model

ROOT=Path(__file__).resolve().parents[1]

def test_sdk_compiler_preserves_provider_identity_and_uses_supported_registration():
    policy=load_policy(ROOT/'config/policy.sdk.mock.yaml')
    native=policy.native_config()
    assert native['litellm_settings']['custom_provider_map']==[
        {'provider':'gateway_openai_sdk','custom_handler':'gateway.stream_bridge.bridge'}]
    for d,entry in zip(policy.deployments,native['model_list']):
        assert entry['litellm_params']['model']==deployment_native_model(d)=='gateway_openai_sdk/'+d.model
        assert entry['litellm_params']['api_base']==d.api_base
        assert entry['litellm_params']['api_key']=='os.environ/'+d.credential_env

@pytest.mark.parametrize('provider,base',[('groq','https://api.groq.com/openai/v1'),('gemini','https://generativelanguage.googleapis.com/v1beta/openai')])
def test_production_sdk_exact_endpoint_only_and_remains_unqualified(provider,base):
    raw=yaml.safe_load((ROOT/'config/policy.yaml').read_text())
    d=next(v for v in raw['deployments'] if v['provider']==provider)
    assert d['adapter']=='openai_sdk' and d['api_base']==base
    assert d['enabled'] is False and d['eligibility']['status']=='unknown'
    d['api_base']=base+'/other'
    with pytest.raises(ValueError):Policy.model_validate(raw)

def test_production_native_streaming_cannot_be_enabled_by_policy_edit():
    raw=yaml.safe_load((ROOT/'config/policy.yaml').read_text())
    raw['deployments'][0]['adapter']='native'
    raw['deployments'][0]['capability']['streaming']=True
    with pytest.raises(ValueError):Policy.model_validate(raw)

def test_native_aggregate_and_audit_storage_are_disabled():
    cfg=load_policy(ROOT/'config/policy.sdk.mock.yaml').native_config()
    assert cfg['general_settings']['disable_spend_updates'] is True
    assert cfg['litellm_settings']['store_audit_logs'] is False
