#!/usr/bin/env python3
"""Export private, paginated gateway metadata; never export content or credentials."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gateway.trace_query import parse_trace_query
from gateway.state import EVENT_FIELDS
from key_admin import private_admin, selected_target, private_parent
from gateway_ops import OpsError


def collect(url, administrator, query, max_events=10000):
    """Bounded keyset traversal; new inserts do not duplicate already read rows."""
    collected=[]
    revision=None
    previous=None
    query=dict(query)
    while True:
        params={k: str(v).lower() if type(v) is bool else v for k,v in query.items()}
        page=private_admin(url,administrator,'/gateway/traces?'+urlencode(params))
        if (page.get('schema_version')!=1 or page.get('contains_content') is not False
                or page.get('order')!='event_id_desc' or not isinstance(page.get('data'),list)):
            raise OpsError('Unexpected diagnostic export schema; no file written.')
        current=page.get('config_revision')
        if not isinstance(current,str) or len(current)!=64 or any(c not in '0123456789abcdef' for c in current):
            raise OpsError('Diagnostic export did not identify its configuration.')
        if revision is not None and current!=revision:
            raise OpsError('Configuration changed during export; retry against a stable revision.')
        revision=current
        data=page['data']
        if len(data)>query['limit']:
            raise OpsError('Diagnostic export exceeded the page bound.')
        for event in data:
            if not isinstance(event,dict) or set(event)-(EVENT_FIELDS|{'event_id'}):
                raise OpsError('Unexpected diagnostic fields; no file written.')
            event_id=event.get('event_id')
            if type(event_id) is not int or event_id<=0 or (previous is not None and event_id>=previous):
                raise OpsError('Diagnostic cursor changed or repeated; no file written.')
            # The authenticated admin secret may never be included even if a
            # broken/misconfigured local server returned it in an allowed field.
            serialized=json.dumps(event,ensure_ascii=False)
            if administrator in serialized or len(serialized)>8192:
                raise OpsError('Unsafe or oversized diagnostic event; no file written.')
            collected.append(event)
            previous=event_id
            if len(collected)>max_events:
                raise OpsError('Export exceeds --max-events; narrow the filters, no file written.')
        cursor=page.get('next_cursor')
        if cursor is None:
            break
        if not data or type(cursor) is not int or cursor!=previous:
            raise OpsError('Invalid diagnostic continuation cursor; no file written.')
        query['before_id']=cursor
    return {'schema_version':1,'exported_at':datetime.now(timezone.utc).isoformat(),
        'config_revision':revision,'filters':{k:v for k,v in query.items() if k!='before_id'},
        'event_count':len(collected),'order':'event_id_desc','contains_content':False,
        'notes':'Read-only metadata; request/attempt counts have separate denominators. Missing usage remains unknown.',
        'data':collected}


def write_export(path,result):
    content=(json.dumps(result,indent=2,ensure_ascii=False)+'\n').encode()
    with private_parent(path,output=True) as (directory,name):
        descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        try:
            with os.fdopen(descriptor,'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            os.unlink(name,dir_fd=directory)
            raise
        os.fsync(directory)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--admin-url')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--request-id')
    parser.add_argument('--key-id')
    parser.add_argument('--since')
    parser.add_argument('--until')
    parser.add_argument('--final-status')
    parser.add_argument('--error-code')
    parser.add_argument('--fallback',choices=['true','false'])
    parser.add_argument('--max-events',type=int,default=10000)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    try:
        from gateway.errors import GatewayError
        values={k.replace('_','-'):v for k,v in vars(args).items()}
        filters={name:getattr(args,name) for name in ('request_id','key_id','since','until','final_status','error_code','fallback') if getattr(args,name) is not None}
        query=parse_trace_query(urlencode({'limit':100,**filters}).encode())
        if not 1<=args.max_events<=100000:
            raise OpsError('--max-events must be 1 to 100000.')
        if not args.execute:
            print(json.dumps({'mode':'DRY_RUN','executed':False,'filters':filters,
                'output':str(args.output),'note':'No credentials read, API called or file written.'},ensure_ascii=False))
            return 0
        url,administrator=selected_target(args.env_file,args.admin_url)
        result=collect(url,administrator,query,args.max_events)
        write_export(args.output,result)
        print(json.dumps({'exported':True,'event_count':result['event_count'],'output':str(args.output),'contains_content':False},ensure_ascii=False))
        return 0
    except (OpsError,GatewayError,OSError):
        print('Diagnostic export failed safely; no successful export was confirmed. Check private file permissions, target and filters.',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
