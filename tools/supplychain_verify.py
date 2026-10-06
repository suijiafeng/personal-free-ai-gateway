#!/usr/bin/env python3
"""Offline, read-only SHA-256 and lock/advisory consistency checks (no Docker)."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root=ROOT):
    root = Path(root)
    checks = []
    def check(name, ok):
        checks.append({'name':name, 'passed':bool(ok)})
    data = json.loads((root/'deploy/image-candidates.json').read_text())
    deployment_text = '\n'.join((root/f).read_text() for f in ('deploy/Dockerfile','deploy/Dockerfile.egress','deploy/compose.yaml'))
    for item in data['images']:
        ref = item['reference']
        check(ref+'_used_immutable', item.get('immutable_reference','__missing__') in deployment_text)
        check(ref+'_no_unpinned_use', not re.search(re.escape(ref)+r'(?!@sha256:)',deployment_text))
        file = item.get('manifest_file')
        check(ref+'_index_digest', bool(file) and 'sha256:'+sha(root/file) == item['digest'])
        if not file:
            continue
        index = json.loads((root/file).read_text())
        descriptors = {p['digest']:p for p in index['manifests']}
        platforms = item.get('platforms',[])
        check(ref+'_mac_platforms', {p['architecture'] for p in platforms} == {'amd64','arm64'})
        for p in platforms:
            check(ref+'_'+p['architecture']+'_content', 'sha256:'+sha(root/p['manifest_file']) == p['digest'])
            descriptor = descriptors.get(p['digest'],{})
            check(ref+'_'+p['architecture']+'_membership', descriptor.get('platform',{}).get('architecture') == p['architecture'])
    lock = (root/'requirements.lock').read_text()
    pins = re.findall(r'^([a-zA-Z0-9_.-]+)(?:\[[^]]+\])?==([^\s\\]+)',lock,re.M)
    check('every_dependency_pinned', bool(pins) and not re.search(r'^(?!#|\s|$)[^\n]*[<>~]',lock,re.M))
    chunks = re.split(r'(?=^[a-zA-Z0-9_.-]+(?:\[[^]]+\])?==)',lock,flags=re.M)[1:]
    check('every_dependency_has_sha256', len(chunks)==len(pins) and all(re.search(r'--hash=sha256:[0-9a-f]{64}',c) for c in chunks))
    report = json.loads((root/'evidence/supply-chain/dependency-advisories-current.json').read_text())
    check('advisory_lock_fingerprint', sha(root/'requirements.lock') == report['requirements_lock_sha256'])
    check('advisory_exact_set', set(pins) == {(p['name'],p['version'])for p in report['packages']})
    check('advisory_queries_complete', len(report['packages']) == len(pins) and all('error' not in p for p in report['packages']))
    check('known_python_advisories_clear', all(not p.get('vulnerabilities') for p in report['packages']))
    for name, source in report.get('source_documents',{}).items():
        check('source_'+name, 'path' in source and sha(root/source['path']) == source['sha256'])
    squid=json.loads((root/'evidence/supply-chain/squid-source.json').read_text())
    release=json.loads((root/squid['github_release_file']).read_text())
    asset=next((a for a in release['assets'] if a['name']=='squid-7.7.tar.xz'),{})
    check('squid_official_source_digest', asset.get('digest') == 'sha256:'+squid['archive_sha256'])
    check('squid_build_checksum_pinned', '--checksum=sha256:'+squid['archive_sha256'] in (root/'deploy/Dockerfile.egress').read_text())
    return {'schema_version':1,'mode':'offline_read_only','passed':all(c['passed'] for c in checks),
            'checks':checks, 'production_approved':False,
            'limitations':['Content hashes are not publisher signature verification.',
              'Saved advisory review is a dated snapshot, not a current online scan.',
              'No container build, OS/npm scan, Mac execution, or real-provider call is implied.']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT)
    args=p.parse_args()
    try:
        result=verify(args.root)
    except (OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'passed':False,'error':'Missing or invalid evidence; refusing to mark verification passed.'}))
        return 2
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':
    raise SystemExit(main())
