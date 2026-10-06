from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from tests.run_postgres_suite import run_test_phases, merge_junit_reports, phase_report_path, SDK_PROXY_TEST


def report(path, *, failed=False):
    path.write_text('<testsuites><testsuite name="pytest" tests="1" failures="'+str(int(failed))+'" errors="0" skipped="0" time="0.5"><testcase classname="case" name="original">'+('<failure message="original assertion">detail</failure>' if failed else '')+'</testcase></testsuite></testsuites>')


def test_both_adapter_phases_execute_in_new_processes_and_merge_every_case(tmp_path):
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        report(Path(next(v.split('=',1)[1] for v in command if v.startswith('--junitxml='))))
        return SimpleNamespace(returncode=0)
    output=tmp_path/'results.xml'
    assert run_test_phases({},output,root=tmp_path,executable='python',run=run)==0
    assert len(calls)==2
    assert '--ignore='+SDK_PROXY_TEST in calls[0]
    assert calls[1][3:5]==[SDK_PROXY_TEST,'-q']
    root=ET.parse(output).getroot()
    assert root.attrib['tests']=='2' and root.attrib['failures']=='0'
    assert len(root.findall('.//testcase'))==2
    assert [s.get('name') for s in root]==['main:pytest','sdk-proxy:pytest']
    assert phase_report_path(output,'main').is_file() and phase_report_path(output,'sdk-proxy').is_file()


def test_first_phase_failure_is_preserved_and_second_phase_still_runs(tmp_path):
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        failed=len(calls)==1
        report(Path(next(v.split('=',1)[1] for v in command if v.startswith('--junitxml='))),failed=failed)
        return SimpleNamespace(returncode=int(failed))
    output=tmp_path/'results.xml'
    assert run_test_phases({},output,root=tmp_path,run=run)==1
    assert len(calls)==2
    root=ET.parse(output).getroot()
    assert root.get('tests')=='2' and root.get('failures')=='1'
    assert root.find('.//failure').text=='detail'


def test_crashed_phase_cannot_reuse_stale_success_report(tmp_path):
    output=tmp_path/'results.xml'
    report(phase_report_path(output,'main'))
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        if len(calls)==1:return SimpleNamespace(returncode=2)
        report(Path(next(v.split('=',1)[1] for v in command if v.startswith('--junitxml='))))
        return SimpleNamespace(returncode=0)
    assert run_test_phases({},output,root=tmp_path,run=run)==2
    root=ET.parse(output).getroot()
    assert root.get('errors')=='1'
    assert root.find('.//error').get('message').startswith('Required phase')


def test_invalid_phase_xml_is_an_explicit_error(tmp_path):
    broken=tmp_path/'broken.xml';broken.write_text('broken')
    output=tmp_path/'merged.xml'
    assert merge_junit_reports([('sdk-proxy',broken)],output) is False
    assert ET.parse(output).getroot().get('errors')=='1'
