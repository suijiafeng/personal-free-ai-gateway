#!/usr/bin/env python3
"""Manual read-only post-start check. CONNECT only; never send provider keys/bodies."""
from __future__ import annotations
import argparse
import json
import socket
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from reviewed_start import approved_hosts


def status(proxy_host,proxy_port,method,target,timeout=4):
    with socket.create_connection((proxy_host,proxy_port),timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(f'{method} {target} HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n'.encode('ascii'))
        raw=b''
        while b'\r\n' not in raw and len(raw)<1024:
            chunk=s.recv(1024-len(raw))
            if not chunk:break
            raw+=chunk
        first=raw.split(b'\r\n',1)[0].split()
        if len(first)<2 or first[0] not in (b'HTTP/1.1',b'HTTP/1.0'):
            raise ValueError('Invalid HTTP proxy response.')
        return int(first[1])


def run(host='egress',port=3128,hosts_file='/etc/squid/approved-hosts.txt'):
    cases=[('external_not_allowlisted','CONNECT','example.com:443',403),
           ('ip_literal','CONNECT','127.0.0.1:443',403),
           ('metadata_address','CONNECT','169.254.169.254:443',403),
           ('ipv6_loopback','CONNECT','[::1]:443',403),
           ('non_tls_port','CONNECT','api.groq.com:80',403),
           ('plain_http','GET','http://api.groq.com/',403),
           ('subdomain','CONNECT','evil.api.groq.com:443',403)]
    cases.extend(('approved_'+h,'CONNECT',h+':443',200) for h in sorted(approved_hosts(hosts_file)))
    results=[]
    for name,method,target,expected in cases:
        try: code=status(host,port,method,target); ok=code==expected
        except (OSError,ValueError):code=None;ok=False
        results.append({'name':name,'status':code,'expected':expected,'passed':ok})
    return {'passed':all(x['passed'] for x in results),'checks':results,
            'provider_inference_called':False,'credentials_used':False,
            'warning':'CONNECT establishes TCP only. This does not validate account/free eligibility, TLS handshake, model compatibility, or container bypass prevention.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',default='egress')
    p.add_argument('--port',type=int,default=3128);p.add_argument('--hosts',default='/etc/squid/approved-hosts.txt')
    a=p.parse_args();r=run(a.host,a.port,a.hosts);print(json.dumps(r,indent=2));return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
