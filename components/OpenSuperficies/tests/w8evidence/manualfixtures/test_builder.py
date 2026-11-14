"""No socket, subprocess, model, Docker, sudo, network or real seat operations."""
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder  # noqa: E402


def baseline():
    t = builder.task()
    return t, builder.wave(t), builder.launch(), builder.port()


def test_positive_is_only_model_eligible():
    t, w, l, p = baseline()
    result = builder.inspect(t, w, l, p)
    assert result['state'] == 'SYNTHETIC_ELIGIBLE_ONLY'
    assert result['code'] is None and not result['authorized']


def test_registration_binds_digest_and_lease():
    t, w, l, p = baseline()
    assert (t['task_id'], t['attempt_id'], t['lease_id'], builder.digest(builder.encoded(t))) in p.registrations
    t['objective'] = 'changed after registration'
    assert builder.inspect(t, w, l, p)['code'] == 2


def test_closed_muse_wave():
    t, w, l, p = baseline()
    assert set(w) == {'task_id', 'attempt', 'objective'}
    assert builder.inspect(t, {**w, 'patch_path': 'synthetic.patch'}, l, p)['code'] is None
    assert builder.inspect(t, {**w, 'launch_spec': {}}, l, p)['code'] == 2


def test_objective_remains_passive():
    t, w, l, p = baseline()
    old_gate = copy.deepcopy(t['gate'])
    w['objective'] = '--privileged --network host --mount /home'
    assert builder.inspect(t, w, l, p)['code'] is None
    assert not builder.inspect(t, w, l, p)['authorized']
    assert old_gate == t['gate'] and l == builder.launch()


def test_gate_is_ordered_stdlib_only_target():
    t = builder.task()
    assert t['gate']['argv'][:3] == ['python3', '-I', '-c']
    assert t['gate']['cwd'] == '.'
    assert builder.digest(builder.encoded(t['gate']['argv'])) != builder.digest(builder.encoded(t['gate']['argv'][::-1]))


def test_receipt_contains_schema_identity_and_safe_simulated_cleanup():
    t = builder.task()
    r = builder.receipt(t)
    assert r['schema'] == 'os.cu.worker-receipt/v1'
    assert (r['task_id'], r['attempt_id'], r['lease_id']) == (t['task_id'], t['attempt_id'], t['lease_id'])
    assert r['task_sha256'] == builder.digest(builder.encoded(t))
    assert r['repo']['commit'] == r['runtime']['commit'] == r['output']['base_commit']
    assert r['gate']['argv'] == t['gate']['argv']
    assert r['gate']['exit_code'] == r['gate']['raw_exit_code'] == 0
    assert r['failure_reason'] is None
    assert r['cleanup']['remaining_workspace_bytes'] == 0
    assert r['cleanup']['status'] == 'verified'
    assert r['extract']['text_only'] is True and r['extract']['mode'] == '0644'
    assert r['extract']['sha256'] == hashlib.sha256(builder.TEXT.encode()).hexdigest()
    assert not r['runtime']['workspace']['cache']['read_only'] is False
    assert r['observations']['probes']['runtime_socket_visible'] is False
    assert r['host_metric']['value'] is None and r['host_metric']['provenance'] == 'unavailable'


def test_unavailable_publication_never_authorizes_os_capsule():
    f = builder.fixtures()
    pub = f['publication_unavailable']
    assert pub['driver_exit_code'] == 4 and pub['worker_exit_code'] == 0
    assert pub['bridge_result'] is None and pub['os_capsule_authorization'] == 'AUSENTE'
    assert pub['publisher_called'] is False and pub['outbox_written'] is False
    assert f['receipt_success_scenario']['host_observed'] == 'AUSENTE'


@pytest.mark.parametrize('name,code,state', [
    ('unknown_launch_key', 5, 'REFUSED'), ('bind', 5, 'REFUSED'),
    ('env_file', 5, 'REFUSED'), ('socket', 5, 'REFUSED'),
    ('unknown_wave_key', 2, 'REFUSED'), ('unregistered_id', 2, 'REFUSED'),
    ('missing_cache', 4, 'REFUSED'), ('seat_down', 6, 'EN_DUDA'),
])
def test_must_fail(name, code, state):
    case = next(v for v in builder.cases() if v['case'] == name)
    result = case['expected']
    assert (result['code'], result['state']) == (code, state)
    assert result['receipt'] is None and not result['authorized'] and not result['publisher_called']


def test_pins_are_synthetic_not_live():
    f = builder.fixtures()
    assert f['authorization'] == f['live_pins'] == 'AUSENTE'
    assert all(p['provenance'] == builder.LABEL for p in f['pins'].values())
    assert all(re.fullmatch('[0-9a-f]{64}', p['value']) for k, p in f['pins'].items() if k != 'repo_commit')
    assert re.fullmatch('[0-9a-f]{40}', f['pins']['repo_commit']['value'])


def test_seat_progression_is_passive():
    f = builder.fixtures()
    assert f['seat_progression'] == ['submit', 'wait', 'receipt', 'durable_text_collection',
                                      'acknowledgement', 'purge_verification']
    assert f['operations_invoked'] is False


def test_generator_reproducible_and_only_writes_utf8_json(tmp_path):
    assert builder.main(['--output-dir', str(tmp_path)]) == 0
    content = (tmp_path / 'fixtures.json').read_text(encoding='utf-8')
    assert content == builder.dump(builder.fixtures())
    assert list(p.name for p in tmp_path.iterdir()) == ['fixtures.json']
    assert builder.main(['--output-dir', str(tmp_path)]) == 0
    assert (tmp_path / 'fixtures.json').read_text(encoding='utf-8') == content


def test_cli_input_and_unexpected_errors(monkeypatch, tmp_path, capsys):
    assert builder.main([]) == 2
    assert builder.main(['--unknown']) == 2
    capsys.readouterr()
    f = tmp_path / 'file'
    f.write_text('SYNTHETIC', encoding='utf-8')
    assert builder.main(['--output-dir', str(f)]) == 2
    def fail(_):
        raise OSError('simulated write error')
    monkeypatch.setattr(builder, 'generate', fail)
    assert builder.main(['--output-dir', str(tmp_path)]) == 3


def test_full_exit_contract_no_code_one():
    assert set(builder.EXIT) == {0, 2, 3, 4, 5, 6, 7}
    assert 1 not in builder.EXIT and builder.EXIT[7] == 'operator'


def test_checked_in_must_fail_case_index():
    saved = json.loads(Path(__file__).with_name('cases.json').read_text(encoding='utf-8'))
    assert saved['authority'] == 'AUSENTE' and saved['synthetic'] is True
    actual = {x['case']: x['expected'] for x in builder.cases()}
    for row in saved['cases']:
        result = actual[row['name']]
        assert result['code'] == row['exit_code']
        assert result['state'] == row['state']
        assert result['receipt'] is None
