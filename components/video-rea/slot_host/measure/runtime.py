# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture bindings and independent observation of the normal REA runner."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slot_host.projections.schema import digest, jcs_bytes
from slot_host.runtime import binding, runner, tools
from slot_host.runtime.live_park import _FixtureLiveAuthority
from slot_host.runtime.process_lifecycle import proc_starttime

from .seal import FixtureError, sha

INSTALLATION = 'installation:fixture-slot@medicion'
GRANT = 'grant.exec.external_process'


class Runtime:
    def __init__(self, host, root):
        self.host = host
        self.store = host.store
        self.manifest = host.seal.inputs['slot']
        self.path = root / 'fixture-adapter'
        self.path.write_bytes(host.seal.executable)
        self.path.chmod(0o500)
        self.inventory = {INSTALLATION: (self.manifest, self.path)}
        self.processes = []
        self.stdout = None
        self.producer = {}

    @staticmethod
    def policy(manifest, operation):
        role = next(item['role'] for item in manifest['operations'] if item['id'] == operation)
        contract = next(item for item in manifest['contract_refs'] if item['role'] == role)
        provider = {k: v for k, v in manifest['provider_ref'].items() if k != 'schema'}
        adapter = manifest['capabilities']['adapter_identity']
        ref = manifest['evidence']['shared_schema_ref']
        return {
            'grants': [GRANT], 'spawn_granted': True, 'manifest_digest': digest(manifest),
            'slot_id': manifest['slot_id'], 'registry': contract | provider | {
                'adapter_id': adapter['adapter_id'], 'adapter_sha256': adapter['adapter_sha256']},
            'licence_disposition': 'external_execution', 'evidence_ref': ref,
            'evidence_sha256': binding._EVIDENCE_PINS[ref][1], 'perception_ceiling': 'none',
        }

    def admits(self, bound):
        return (self.store.root.is_dir() and bound.executable_path == str(self.path)
                and bound.installation_ref == INSTALLATION
                and bound.slot_id == self.manifest['slot_id']
                and bound.manifest_digest == digest(self.manifest)
                and bound.implementation_sha256 == sha(self.host.seal.executable)
                and bound.operation_id in ('record', 'replay'))

    def started(self, receipt):
        attempt = receipt['attempt']
        if (attempt['request_id'], attempt['attempt_id']) != (self.store.request_id, self.store.attempt_id):
            raise FixtureError('Runtime receipt identifies a different fixture attempt.')
        key = 'terminal-profile' if 'open-profile' in self.store.index else 'open-profile'
        self.store.put_json(key, receipt)
        return True

    def spawn(self, argv, **kwargs):
        fd, = kwargs['pass_fds']
        if (self.processes or 'open-profile' not in self.store.index
                or kwargs['executable'] != f'/proc/self/fd/{fd}'
                or tuple(argv[1:]) != self.expected_argv or kwargs['env'] != {}
                or sha(os.pread(fd, len(self.host.seal.executable) + 1, 0)) != sha(self.host.seal.executable)):
            raise FixtureError('Independent launch observation differs from the sealed fixture.')
        saved = self.store.read('open-profile', request_id=self.store.request_id, attempt_id=self.store.attempt_id)
        if not saved:
            raise FixtureError('The durable opened profile did not survive readback.')
        process = subprocess.Popen(argv, **kwargs)
        self.processes.append((process, proc_starttime(process.pid), Path(kwargs['cwd']), Path(argv[0])))
        return process

    def capture(self, process, deadline, cap):
        out, err, timeout, overflow = runner.process_lifecycle.collect_output(process, deadline, cap)
        child, starttime, _, _ = self.processes[0]
        if child is not process or type(starttime) is not int:
            raise FixtureError('Captured stdout does not bind the observed fixture child.')
        self.stdout = out
        self.producer.update(process_id=child.pid, starttime=starttime, host_result_sha256=sha(out))
        return out, err, timeout, overflow

    def tape_record(self, parent, checksum):
        tape = self.host.asset(self.host.assignment['refs'][0], 'tape')
        if (parent, checksum) != (tape['identity'], tape['sha256']):
            return None
        return {'parent_episode_id': parent, 'input_tape_sha256': checksum}

    def dispatch(self, face, request, payload):
        policy = Runtime.policy(self.manifest, payload.get('operation_id', 'record'))
        if face == 'slot.inspect':
            with patch.object(binding, '_retention_available', self.admits):
                return tools.dispatch(face, {'request_id': request, 'payload': payload},
                                      host=self.inventory, host_policy=policy)['result']

        tape = self.host.asset(self.host.assignment['refs'][0], 'tape')
        if payload['tape_sha256'] != tape['sha256']:
            raise FixtureError('Runtime tape digest differs from the sealed input.')
        tape_arg = jcs_bytes(tape['data']).decode()
        runtime_payload = dict(payload, argv=[request, tape_arg])
        self.expected_argv = (payload['operation_id'], request, tape_arg)
        self.implementation = policy['registry']['implementation_sha256']
        with patch.object(binding, '_retention_available', self.admits):
            authority = None
            if payload['installation_ref'] == INSTALLATION:
                verified = binding.verify_binding(
                    self.manifest, self.path, operation_id=payload['operation_id'],
                    requested_profile='external_process', capsule_perception_ceiling='none',
                    installation_ref=INSTALLATION, **tools._auth_kwargs_from_policy(policy))
                if not self.admits(verified):
                    raise FixtureError('Runtime fixture binding is stale.')
                authority = _FixtureLiveAuthority(verified, request_id=request, attempt_id=self.store.attempt_id)
            # Explicit witness binds the captured bytes and independent spawn identity.
            # This sealed child has no imports or operations that start descendants.
            with (patch.object(runner, 'subprocess', SimpleNamespace(Popen=self.spawn, PIPE=subprocess.PIPE,
                                                                     DEVNULL=subprocess.DEVNULL)),
                  patch.object(runner, '_collect_output', self.capture)):
                result = tools.dispatch(
                    face, {'request_id': request, 'payload': runtime_payload}, host=self.inventory,
                    host_policy=policy, live_authority=authority, producer_witness=self.producer,
                    tape_resolver=self.tape_record, attempt_store=self.started)['result']
        if (result['evidence_accepted'] is not True or self.stdout != result['stdout'].encode()
                or result['shared_records'] is None):
            raise FixtureError('Runtime result or evidence projection failed fixture custody.')
        self.store.put('stdout', self.stdout)
        for key, document in result['shared_records'].items():
            self.store.put_json(key, document)
        return result

    def settled(self):
        return all(process.returncode == 0 and proc_starttime(process.pid) != start
                   and not scratch.exists() and not staged.parent.exists()
                   for process, start, scratch, staged in self.processes)
