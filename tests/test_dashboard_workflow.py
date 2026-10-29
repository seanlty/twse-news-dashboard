"""Exercise the actual embedded workflow scripts without calling live services."""

import ast
from datetime import datetime
from pathlib import Path
import subprocess
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/dashboard-update.yml'


def workflow_script(job):
    section = WORKFLOW.read_text(encoding='utf-8').split(f'  {job}:\n', 1)[1]
    script = section.split('        run: |\n', 1)[1]
    lines = []
    for line in script.splitlines():
        if line.strip() and not line.startswith('          '):
            break
        lines.append(line)
    return textwrap.dedent('\n'.join(lines))


@pytest.mark.parametrize('timestamp,manual,expected', [
    ('2026-10-29T06:59:59', False, False),
    ('2026-10-29T07:00:00', False, True),
    ('2026-10-29T23:00:00', False, True),
    ('2026-10-29T23:00:59', False, True),
    ('2026-10-29T23:01:00', False, False),
    ('2026-11-30T07:00:00', False, True),
    ('2026-12-01T07:00:00', False, True),
    ('2026-10-29T01:00:00', True, True),
    ('2026-12-01T01:00:00', True, True),
])
def test_schedule_boundaries(timestamp, manual, expected):
    tree = ast.parse(workflow_script('plan'))
    tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
    scope = {}
    exec(compile(tree, str(WORKFLOW), 'exec'), scope)
    assert scope['should_update'](datetime.fromisoformat(timestamp), manual) is expected


def run_update(monkeypatch, tmp_path, responses):
    calls = []
    summary = tmp_path / 'summary.md'
    monkeypatch.setenv('DASHBOARD_TARGET', 'railway')
    monkeypatch.setenv('DASHBOARD_BASE_URL', 'https://dashboard.example.test')
    monkeypatch.setenv('UPDATE_TOKEN', 'test-credential-do-not-log')
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))

    def fake_run(args, **kwargs):
        calls.append(args)
        return responses[len(calls) - 1]

    monkeypatch.setattr(subprocess, 'run', fake_run)
    error = None
    try:
        exec(compile(workflow_script('update'), str(WORKFLOW), 'exec'), {})
    except SystemExit as exc:
        error = exc
    return calls, summary.read_text(encoding='utf-8'), error


@pytest.mark.parametrize('bad_response', [
    subprocess.CompletedProcess([], 22, '{"message":"test-credential-do-not-log"}\n502', 'test-credential-do-not-log'),
    subprocess.CompletedProcess([], 0, '{"ok":false}\n200', ''),
    subprocess.CompletedProcess([], 0, 'invalid json\n200', ''),
    subprocess.CompletedProcess([], 0, '[]\n200', ''),
])
def test_failed_endpoint_does_not_skip_other_updates_or_expose_credentials(monkeypatch, tmp_path, capsys, bad_response):
    good = subprocess.CompletedProcess([], 0, '{"ok":true}\n200', '')
    calls, summary, error = run_update(monkeypatch, tmp_path, [bad_response, good, good])
    assert len(calls) == 3
    assert error is not None
    assert '| material info |' in summary and '| failed |' in summary
    assert '| monthly revenue | 200 | success |' in summary
    assert '| financial report | 200 | success |' in summary
    assert 'test-credential-do-not-log' not in summary + capsys.readouterr().out


def test_success_reports_degraded_sources_and_cooldown(monkeypatch, tmp_path, capsys):
    responses = [
        subprocess.CompletedProcess([], 0, '{"ok":true,"skipped":true}\n200', ''),
        subprocess.CompletedProcess([], 0, '{"ok":true,"degraded":true}\n200', ''),
        subprocess.CompletedProcess([], 0, '{"ok":true}\n200', ''),
    ]
    calls, summary, error = run_update(monkeypatch, tmp_path, responses)
    assert len(calls) == 3 and error is None
    assert 'cooldown' in summary and 'degraded source data' in summary
    assert '::warning::' in capsys.readouterr().out
