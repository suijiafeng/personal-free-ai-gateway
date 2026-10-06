#!/usr/bin/env python3
"""Build a disposable loopback-only PostgreSQL fixture and run the complete suite.

Requires installed PostgreSQL 17 executables, pinned Python requirements and Node.
Never targets an existing database. Trust authentication is TEST ONLY.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
SDK_PROXY_TEST = "tests/integration/test_sdk_proxy_postgres.py"


def phase_report_path(destination, phase):
    destination = Path(destination)
    return destination.with_name(f"{destination.stem}-{phase}{destination.suffix or '.xml'}")


def merge_junit_reports(reports, destination):
    """Retain every testcase/failure and fail visibly if any phase lost its report."""
    combined = ET.Element("testsuites")
    valid = True
    for phase, path in reports:
        try:
            root = ET.parse(path).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
            if not suites:
                raise ValueError("No testsuite in required phase report")
            for suite in suites:
                suite.set("name", phase + ":" + suite.get("name", "pytest"))
                combined.append(suite)
        except (OSError, ET.ParseError, ValueError):
            valid = False
            suite = ET.SubElement(combined, "testsuite", name=phase + ":runner", tests="1", errors="1", failures="0", skipped="0", time="0")
            case = ET.SubElement(suite, "testcase", classname="postgres_suite.runner", name="required_phase_report")
            ET.SubElement(case, "error", message="Required phase JUnit report is missing or invalid").text = str(path)
    for attribute in ("tests", "errors", "failures", "skipped"):
        combined.set(attribute, str(sum(int(suite.get(attribute, "0")) for suite in combined)))
    combined.set("time", str(sum(float(suite.get("time", "0")) for suite in combined)))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(combined).write(destination, encoding="utf-8", xml_declaration=True)
    return valid


def run_test_phases(env, destination, *, root=ROOT, executable=sys.executable, run=None):
    """A new native Proxy lifespan belongs to a new Python process.

    Both phases run even if one fails. Main phase omits only the SDK module,
    which is executed unchanged in phase two against the same disposable DB.
    """
    run = run or subprocess.run
    destination = Path(destination)
    if not destination.is_absolute():
        destination = Path(root) / destination
    phases = (
        ("main", ["tests", "ops", "--ignore=" + SDK_PROXY_TEST]),
        ("sdk-proxy", [SDK_PROXY_TEST]),
    )
    reports = []
    result = 0
    for phase, selection in phases:
        report = phase_report_path(destination, phase)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)  # Never merge a stale report after a crash.
        reports.append((phase, report))
        print(f"Running isolated pytest phase: {phase}", flush=True)
        try:
            code = run([executable, "-m", "pytest", *selection, "-q", f"--junitxml={report}"], env=env, cwd=root).returncode
        except OSError:
            code = 1
        if code != 0:
            result = result or (code if code > 0 else 1)
    if not merge_junit_reports(reports, destination):
        result = result or 1
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin',default=os.environ.get('GATEWAY_POSTGRES_BIN'))
    parser.add_argument('--junitxml',default='evidence/full-regression-results.xml')
    parser.add_argument('--skip-backup-restore',action='store_true',help='Skip both database snapshot and native-process recovery rehearsal; neither is a pass.')
    args=parser.parse_args()
    binary_dir=Path(args.postgres_bin) if args.postgres_bin else None
    binaries={}
    for name in ('initdb','pg_ctl','createdb','pg_dump','pg_restore'):
        found=str(binary_dir/name) if binary_dir else shutil.which(name)
        if not found or not Path(found).is_file():
            parser.error('PostgreSQL executables are required; pass --postgres-bin or install them from the official OS package source.')
        binaries[name]=found
    if os.geteuid()==0:
        parser.error('Run disposable PostgreSQL as a non-root user.')
    version=subprocess.check_output([binaries['initdb'],'--version'],text=True)
    if ' 17.' not in version:
        parser.error('This baseline is verified on PostgreSQL 17; validate upgrades explicitly.')
    if not shutil.which('node'):
        parser.error('Node is required by the pinned native Prisma toolchain.')
    temp=Path(tempfile.mkdtemp(prefix='gateway-disposable-postgres-'))
    env=dict(os.environ)
    env['LITELLM_LOCAL_MODEL_COST_MAP']='True'
    env['LITELLM_TELEMETRY']='False'
    env['PRISMA_BINARY_CACHE_DIR']=env.get('PRISMA_BINARY_CACHE_DIR',str(temp/'prisma'))
    env['PRISMA_NODEENV_CACHE_DIR']=env.get('PRISMA_NODEENV_CACHE_DIR',str(temp/'prisma-nodeenv'))
    env['NPM_CONFIG_CACHE']=env.get('NPM_CONFIG_CACHE',str(temp/'npm'))
    env['PATH']=str(Path(sys.executable).parent)+os.pathsep+env['PATH']
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    dsn=f'postgresql://gateway_test@127.0.0.1:{port}/gateway_test'
    env['DATABASE_URL']=dsn
    env['GATEWAY_TEST_DATABASE_URL']=dsn
    data=temp/'data'
    started=False
    try:
        def run(command):
            subprocess.run(command,env=env,cwd=ROOT,check=True)
        run([binaries['initdb'],'-D',str(data),'-U','gateway_test','--auth=trust','--encoding=UTF8','--no-locale'])
        run([binaries['pg_ctl'],'-D',str(data),'-l',str(temp/'postgres.log'),'-o',f"-h 127.0.0.1 -p {port} -c unix_socket_directories='' -c timezone=UTC",'start'])
        started=True
        run([binaries['createdb'],'-h','127.0.0.1','-p',str(port),'-U','gateway_test','gateway_test'])
        native=Path(importlib.util.find_spec('litellm').origin).parent
        extras=Path(importlib.util.find_spec('litellm_proxy_extras').origin).parent
        run([sys.executable,'-m','prisma','generate','--schema',str(native/'proxy/schema.prisma')])
        run([sys.executable,'-m','prisma','migrate','deploy','--schema',str(extras/'schema.prisma')])
        import psycopg
        with psycopg.connect(dsn) as connection:
            connection.execute((ROOT/'deploy/postgres/20-gateway-ext.sql').read_text())
        run([sys.executable,'-m','unittest','tests.verify_backup_restore','-q'])
        result=run_test_phases(env, args.junitxml)
        if result==0 and not args.skip_backup_restore:
            sys.path.insert(0,str(ROOT))
            from tests.verify_native_recovery import verify_native_recovery
            def stop_database():
                run([binaries['pg_ctl'],'-D',str(data),'-m','fast','stop'])
            def start_database():
                run([binaries['pg_ctl'],'-D',str(data),'-l',str(temp/'postgres.log'),'-o',f"-h 127.0.0.1 -p {port} -c unix_socket_directories='' -c timezone=UTC",'start'])
            recovery,lifecycle=verify_native_recovery(dsn,Path(binaries['pg_dump']).parent,temp,
                stop_database=stop_database,start_database=start_database)
            (ROOT/'evidence/native-recovery.json').write_text(json.dumps(lifecycle,indent=2,ensure_ascii=False)+'\n')
            destination=ROOT/'evidence/database-recovery.json'
            destination.write_text(json.dumps(recovery,indent=2,ensure_ascii=False)+'\n')
            print('Same-version disposable PostgreSQL backup/restore: '+recovery['status'])
        return result
    finally:
        stopped=True
        if started:
            stopped=subprocess.run([binaries['pg_ctl'],'-D',str(data),'-m','fast','stop'],env=env,timeout=30).returncode==0
        if stopped:
            shutil.rmtree(temp)
        else:
            print('Fixture could not be stopped; its temporary data was retained for inspection.',file=sys.stderr)


if __name__=='__main__':
    raise SystemExit(main())
