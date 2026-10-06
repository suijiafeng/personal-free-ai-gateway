#!/usr/bin/env python3
"""Validate official Squid with real TCP, synthetic requests, no Internet targets.

Supply an already-reviewed installed/build binary. This tool does not download,
install, contact providers, read real keys, or change the host network.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
from probe_egress import status


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


def run(binary:Path):
    version=subprocess.run([str(binary),'-v'],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=5)
    if version.returncode or 'Version 7.7' not in version.stdout:
        raise RuntimeError('Requires the reviewed Squid7.7 binary.')
    results=[]
    with tempfile.TemporaryDirectory(prefix='gateway-squid-acl-') as directory:
        d=Path(directory);(d/'hosts').write_text('127.0.0.1 api.groq.com\n')
        template=(ROOT/'deploy/egress/squid.conf').read_text()
        for phase,allow in [('default','deny-all.invalid'),('reviewed_private_dns','api.groq.com')]:
            port=free_port();(d/'allowlist').write_text(allow+'\n')
            conf=template.replace('http_port 3128',f'http_port 127.0.0.1:{port}')
            conf=conf.replace('/etc/squid/approved-hosts.txt',str(d/'allowlist'))
            conf=conf.replace('/tmp/squid.pid',str(d/'squid.pid'))
            conf+=f'\nhosts_file {d / "hosts"}\n'
            config=d/'squid.conf';config.write_text(conf)
            parsed=subprocess.run([str(binary),'-k','parse','-f',str(config)],stdin=subprocess.DEVNULL,
                                  capture_output=True,text=True,timeout=10)
            results.append({'name':phase+'_squid_config_parse','passed':parsed.returncode==0})
            if parsed.returncode:
                return {'passed':False,'checks':results,'parse_output':parsed.stderr[:4000]}
            with open(d/'process.log','w') as log:
                process=subprocess.Popen([str(binary),'-N','-f',str(config)],stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,env={'PATH':os.defpath,'HOME':str(d),'LANG':'C'})
                try:
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        if process.poll() is not None:raise RuntimeError('Squid failed before TCP readiness.')
                        try:
                            with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                        except OSError:time.sleep(.05)
                    cases=[('unapproved_hostname','CONNECT','example.com:443'),
                           ('ipv4_loopback','CONNECT','127.0.0.1:443'),
                           ('ipv6_loopback','CONNECT','[::1]:443'),
                           ('metadata','CONNECT','169.254.169.254:443'),
                           ('wrong_port','CONNECT','api.groq.com:80'),
                           ('plain_http','GET','http://api.groq.com/'),
                           ('subdomain','CONNECT','evil.api.groq.com:443'),
                           ('ftp','GET','ftp://api.groq.com/file')]
                    if phase=='reviewed_private_dns':
                        cases.append(('approved_name_private_resolution','CONNECT','api.groq.com:443'))
                    for name,method,target in cases:
                        actual=status('127.0.0.1',port,method,target)
                        results.append({'name':phase+'_'+name,'status':actual,'expected':403,'passed':actual==403})
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    return {'passed':all(x['passed'] for x in results),'checks':results,
            'scope':'Real loopback TCP against upstream Squid7.7; original ACL. Only bind port and fixture paths changed.',
            'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
            'acl_sha256':hashlib.sha256((ROOT/'deploy/egress/squid.conf').read_bytes()).hexdigest(),
            'test_harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'squid_version':version.stdout.splitlines()[0],
            'provider_calls':0,'real_credentials_used':False,'container_network_tested':False,
            'positive_public_connect_tested':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--squid',type=Path,required=True)
    args=parser.parse_args();result=run(args.squid.resolve());print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)
