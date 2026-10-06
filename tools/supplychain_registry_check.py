#!/usr/bin/env python3
"""Optional public-registry read-only recheck; no pull, install, image run, or writes."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
ACCEPT=', '.join(['application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json'])
ALLOWED={'python','node','postgres','nginx'}


def check(root=ROOT):
    records=[]
    for item in json.loads((root/'deploy/image-candidates.json').read_text())['images']:
        name=item['reference'].split(':',1)[0]
        if name not in ALLOWED:raise ValueError('Only reviewed Docker Official Images are supported.')
        repository='library/'+name
        url='https://auth.docker.io/token?service=registry.docker.io&scope=repository:'+repository+':pull'
        with urllib.request.urlopen(url,timeout=20) as r:token=json.load(r)['token']
        # Token is short-lived read-only public-registry scope, never printed/saved.
        for ref,file in [(item['digest'],item['manifest_file'])]+[(p['digest'],p['manifest_file']) for p in item['platforms']]:
            request=urllib.request.Request('https://registry-1.docker.io/v2/'+repository+'/manifests/'+ref,
                headers={'Accept':ACCEPT,'Authorization':'Bearer '+token})
            with urllib.request.urlopen(request,timeout=20) as r:
                body=r.read(1024*1024+1);header=r.headers.get('Docker-Content-Digest')
            digest='sha256:'+hashlib.sha256(body).hexdigest()
            records.append({'reference':repository+'@'+ref,'content_hash_matches':digest==ref,
                'header_matches':header==ref,'saved_bytes_match':body==(root/file).read_bytes()})
    return {'mode':'online_public_registry_read_only','checks':records,
        'passed':all(r['content_hash_matches'] and r['header_matches'] and r['saved_bytes_match'] for r in records),
        'signature_verified':False,'image_executed':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--online',action='store_true',required=True);p.parse_args()
    try:result=check()
    except Exception:result={'passed':False,'error':'Registry check failed; no image or credential changes made.'}
    print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)
