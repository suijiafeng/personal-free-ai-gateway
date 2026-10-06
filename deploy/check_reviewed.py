#!/usr/bin/env python3
"""Read-only deployment gate. No daemon calls, provider traffic, or secret output."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.reviewed_start import approved_hosts, validate_policy


def check(root=ROOT, policy_path=None, hosts_path=None):
    import yaml
    from gateway.config import load_policy
    root = Path(root)
    policy_path = Path(policy_path or root/'config/policy.yaml')
    hosts_path = Path(hosts_path or root/'deploy/egress/approved-hosts.txt')
    compose = yaml.safe_load((root/'deploy/compose.yaml').read_text())
    overlay = yaml.safe_load((root/'deploy/compose.egress.yaml').read_text())
    checks = []
    def require(name, ok):
        checks.append({'name':name, 'passed':bool(ok)})
    require('backend_internal', compose['networks']['backend'].get('internal') is True)
    for service in ('gateway', 'postgres'):
        c = compose['services'][service]
        require(service+'_internal_only', c.get('networks') == ['backend'] and not c.get('ports') and not c.get('network_mode'))
    require('overlay_does_not_connect_gateway_to_edge', 'networks' not in overlay['services']['gateway'])
    bootstrap = overlay['services']['gateway']
    require('provider_bridge_root_before_drop_only', bootstrap.get('user') == '0:0'
            and bootstrap.get('init') is False and bootstrap.get('cap_drop') == ['ALL']
            and set(bootstrap.get('cap_add',[])) == {'SETUID','SETGID'}
            and bootstrap.get('security_opt') == ['no-new-privileges:true'])
    require('provider_healthcheck_drops_first', bootstrap.get('healthcheck',{}).get('test') ==
            ['CMD','python','/app/deploy/reviewed_start.py','--healthcheck'])
    egress = overlay['services']['egress']
    require('egress_only_bridge', set(egress['networks']) == {'backend','egress_edge'} and not egress.get('ports') and not egress.get('network_mode'))
    require('egress_no_secrets', not egress.get('secrets') and not egress.get('environment'))
    for provider in ('groq','gemini'):
        p = yaml.safe_load((root/f'deploy/compose.{provider}.yaml').read_text())
        secret = provider+'_api_key'
        require(provider+'_file_only_secret', p['services']['gateway'].get('secrets') == [secret]
                and p['secrets'][secret].get('file','').startswith('${'+provider.upper()+'_API_KEY_FILE:?')
                and not p['services']['gateway'].get('environment'))
    acl = [s.strip() for s in (root/'deploy/egress/squid.conf').read_text().splitlines()
           if s.strip() and not s.lstrip().startswith('#')]
    require('squid_exact_domain_no_reverse_dns', 'acl approved_hosts dstdomain -n "/etc/squid/approved-hosts.txt"' in acl)
    rules = [s for s in acl if s.startswith('http_access ')]
    require('squid_fail_closed_rule_order', rules == ['http_access deny !CONNECT',
        'http_access deny !TLS_port','http_access deny !approved_hosts','http_access deny blocked_dst',
        'http_access allow CONNECT TLS_port approved_hosts','http_access deny all'])
    nginx=(root/'deploy/nginx/nginx.conf').read_text()
    require('nginx_no_duplicate_request_log', 'access_log off;' in nginx and 'log_format ' not in nginx)
    require('squid_port443_only', 'acl TLS_port port 443' in acl)
    require('no_tls_interception', not any(s.startswith(('ssl_bump','https_port')) for s in acl))
    try:
        policy = load_policy(policy_path)
        enabled = validate_policy(policy, approved_hosts(hosts_path))
        require('policy_hosts_consistent', True)
    except Exception:
        enabled = set()
        require('policy_hosts_consistent', False)
    return {'schema_version':1,'mode':'read_only_static','passed':all(x['passed'] for x in checks),
            'activation_ready':False, 'enabled_providers':sorted(enabled), 'checks':checks,
            'policy_sha256':hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            'target_runtime_verified':False,
            'remaining_gates':['Mac Docker build/network verification','account and free-only review',
                               'secret file preparation by owner','final image/SBOM/advisory review']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--policy', type=Path)
    p.add_argument('--hosts', type=Path)
    args = p.parse_args()
    try:
        result = check(policy_path=args.policy, hosts_path=args.hosts)
    except Exception:
        print(json.dumps({'passed':False,'error':'Deployment file validation failed; no secret values are reported.'}))
        return 2
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
