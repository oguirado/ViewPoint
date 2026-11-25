import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from guest_command_runtime.controller import Controller
from guest_command_runtime.image import ImageError
from guest_command_runtime.schema import Invalid
from guest_command_runtime.wire import WireError


def config():
    return {'schema':'guest-command-host-config/v1','owner_id':'actor-unit','generation':1,
            'runtime_admitted':True,'runtime_root':'/dev/shm/unit','external_cgroup':'/sys/fs/cgroup/unit',
            'startup_seconds':15,'stop_seconds':5,'artifacts':{
                key:{'path':'/unit/'+key,'sha256':'a'*64} for key in ('engine','kernel','broker','busybox')}}


class FakeNative:
    def __init__(self,config,record):
        self.record=record;self.nonce='a'*64;self.lifetime=time.monotonic()+55;self.closed=False
    def open(self):return self
    def close(self,*,graceful):self.closed=True;return {'closed':True}


class FakeWire:
    def __init__(self,sock,validate):self.validate=validate;self.sent=[]
    def send(self,kind,meta,payload=b'',*,deadline):
        self.validate(kind,meta,len(payload));self.sent.append((kind,copy.deepcopy(meta),payload))
    def receive(self,*,lifetime_deadline):
        hello=self.sent[0][1]
        return 5,{'kind':'ready',**{key:hello[key] for key in ('workload_id','generation','reservation_id')}},b''


class LocalController(Controller):
    def _receive(self):pass  # Tests drive actual actor state with synthetic transport.


class DiagnosticsTests(unittest.TestCase):
    def test_startup_exception_details_are_allowlisted_and_private(self):
        class UntrustedError(RuntimeError):
            def __str__(self):
                raise AssertionError('must never stringify an unknown exception')

        withheld = 'exception details withheld'
        native_message = 'init must have no PT_DYNAMIC or PT_INTERP'
        image_message = 'static ARM64 ET_EXEC required'
        synthetic_secret = 'synthetic private path'
        for error, expected_class, expected_message in (
            (RuntimeError(native_message), 'RuntimeError', native_message),
            (ImageError(image_message), 'ImageError', image_message),
            (RuntimeError(synthetic_secret), 'RuntimeError', withheld),
            (UntrustedError(native_message), withheld, withheld),
        ):
            with self.subTest(exception=type(error)), tempfile.TemporaryDirectory() as state:
                events = []
                controller = LocalController(config(), 'session-unit', state, events.append, FakeNative, FakeWire)
                try:
                    controller.native.open = Mock(side_effect=error)
                    controller.request({'schema': 'guest_command_request/v0.2', 'request_id': 'request-create',
                                        'op': 'create', 'params': {'profile_id': 'guest-command-arm64-v1'}})
                    self.assertEqual([event['event'] for event in events], ['accepted', 'outcome_unknown'])
                    emitted = events[-1]
                    self.assertEqual(emitted['data'], {'op': 'create', 'code': 'TRANSPORT'})
                    controller.contracts.validate('guest_command_event.schema.json', emitted)
                    raw = (Path(state) / 'journal.jsonl').read_text(encoding='utf-8')
                    self.assertNotIn(synthetic_secret, raw)
                    journal = json.loads(raw.splitlines()[-1])['body']
                    self.assertEqual(journal['event'], emitted)
                    self.assertEqual(journal['exception_class'], expected_class)
                    self.assertEqual(journal['exception_message'], expected_message)
                    self.assertTrue(controller.native.closed)
                finally:
                    controller.store.close()

    def test_async_exception_details_are_allowlisted_and_private(self):
        for code, error_class, message in (
            ('TRANSPORT', 'WireError', 'frame read timed out'),
            ('PROTOCOL', 'Invalid', 'guest binding or direction changed'),
        ):
            class PostStartupWire(FakeWire):
                def __init__(self, sock, validate):
                    super().__init__(sock, validate)
                    self.receive = Mock(side_effect=self.frames())

                def frames(self):
                    kind, meta, payload = super().receive(lifetime_deadline=None)
                    yield kind, meta, payload
                    if code == 'PROTOCOL':
                        yield kind, dict(meta, generation=meta['generation'] + 1), payload
                    raise WireError('frame read timed out')

            with self.subTest(code=code), tempfile.TemporaryDirectory() as state:
                events = []
                controller = Controller(config(), 'session-unit', state, events.append, FakeNative, PostStartupWire)
                try:
                    controller.request({'schema': 'guest_command_request/v0.2', 'request_id': 'request-create',
                                        'op': 'create', 'params': {'profile_id': 'guest-command-arm64-v1'}})
                    controller.reader.join(timeout=2)
                    self.assertFalse(controller.reader.is_alive())
                    controller.finish()
                    controller.run()
                    self.assertEqual([event['event'] for event in events], ['accepted', 'ready', 'outcome_unknown'])
                    emitted = events[-1]
                    self.assertEqual(emitted['data'], {'op': 'create', 'code': code})
                    controller.contracts.validate('guest_command_event.schema.json', emitted)
                    raw = (Path(state) / 'journal.jsonl').read_text(encoding='utf-8')
                    journal = json.loads(raw.splitlines()[-1])['body']
                    self.assertEqual(journal['event'], emitted)
                    self.assertEqual(journal['exception_class'], error_class)
                    self.assertEqual(journal['exception_message'], message)
                finally:
                    controller.store.close()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.events=[]
        self.c=LocalController(config(),'session-unit',self.tmp.name,self.events.append,FakeNative,FakeWire)
        self.c.request({'schema':'guest_command_request/v0.2','request_id':'request-create','op':'create',
                        'params':{'profile_id':'guest-command-arm64-v1'}})
    def tearDown(self):self.c.store.close();self.tmp.cleanup()
    def request(self,op,rid,**params):
        return {'schema':'guest_command_request/v0.2','request_id':rid,'op':op,
                'params':{key:self.c.state[key] for key in ('workload_id','generation')}|params}
    def event(self,kind,**fields):
        self.c.guest_event(5,{'kind':kind,**self.c._binding(),**fields},b'')
    def start(self,rid='request-run',job='job-one'):
        self.c.request(self.request('run',rid,job_id=job,command_utf8='printf ok',cwd='',stdin_b64='',timeout_ms=1000))
        self.event('accepted',request_id=rid,op='exec',job_id=job)
    def terminal(self,rid='request-run',job='job-one',cause='exit',cancel=None,stop=None,count=0,complete=True):
        self.event('job_finished',request_id=rid,job_id=job,cancel_request_id=cancel,stop_request_id=stop,
                   cause=cause,exit_code=0 if cause=='exit' else None,signal=None if cause=='exit' else 9,
                   output_complete=complete,job_empty=True,stdout_sent_bytes=count,stderr_sent_bytes=0)

    def test_multiple_commands_and_exact_output(self):
        self.start()
        self.c.guest_event(5,{'kind':'output',**self.c._binding(),'request_id':'request-run',
                              'job_id':'job-one','stream':'stdout','chunk_index':0},b'ok')
        self.terminal(count=2)
        result=self.events[-1]
        self.assertEqual(result['data']['stdout_sha256'],hashlib.sha256(b'ok').hexdigest())
        self.assertEqual(self.c.state['phase'],'ready')
        self.start('request-next','job-two');self.terminal('request-next','job-two')
        self.assertEqual(self.c.state['jobs_started'],2)
        self.assertEqual(self.c.wire.sent[1][2],b'printf ok')

    def test_cancel_is_separate_and_requires_ack(self):
        self.start();self.c.request(self.request('cancel','request-cancel',job_id='job-one'))
        self.event('accepted',request_id='request-cancel',op='cancel',job_id='job-one')
        self.terminal(cause='cancel',cancel='request-cancel')
        self.assertEqual(self.events[-1]['request_id'],'request-run')
        self.assertEqual(self.c.state['phase'],'ready')
        self.assertEqual(self.c.state['job']['state'],'cancelled')

    def test_exit_before_cancel_ack_fences_new_job(self):
        self.start();self.c.request(self.request('cancel','request-cancel',job_id='job-one'))
        self.terminal()
        self.assertEqual(self.c.state['phase'],'awaiting_control')
        self.event('error',request_id='request-cancel',code='INVALID')
        self.assertEqual(self.c.state['phase'],'ready')
        self.assertEqual(self.c.state['job']['state'],'exited')

    def test_stop_needs_job_drain_and_native_close(self):
        self.start();self.c.request(self.request('stop','request-stop'))
        self.event('accepted',request_id='request-stop',op='stop',job_id=None)
        with self.assertRaises(Invalid):self.event('stopping',request_id='request-stop')
        self.assertFalse(self.c.native.closed)
        self.terminal(cause='stop',stop='request-stop')
        self.event('stopping',request_id='request-stop')
        self.assertTrue(self.c.native.closed)
        self.assertEqual(self.c.state['ownership'],'released')

    def test_unknown_keeps_reservation_and_known_finished_job(self):
        self.start();self.c.request(self.request('cancel','request-cancel',job_id='job-one'))
        self.terminal();self.c._unknown('request-cancel','TRANSPORT')
        self.assertTrue(self.c.native.closed)
        self.assertEqual(self.c.state['ownership'],'held')
        self.assertEqual(self.c.state['job']['state'],'exited')
        self.c.request(self.request('stop','request-stop'))
        self.assertEqual(self.c.state['ownership'],'released')
        self.assertEqual(self.c.state['job']['state'],'exited')

    def test_output_loss_and_forged_control_rejected(self):
        self.start()
        with self.assertRaises(Invalid):self.terminal(count=1)
        with self.assertRaises(Invalid):self.terminal(cause='cancel',cancel='request-forged')
        self.assertNotEqual(self.c.state['phase'],'ready')

    def test_idle_lifetime_failure_emits_original_create_resource_update(self):
        self.start();self.terminal()
        self.c._unknown('request-run','DEADLINE')
        event=self.events[-1]
        self.assertEqual(event['event'],'outcome_unknown')
        self.assertEqual(event['request_id'],'request-create')
        self.assertEqual(event['data']['op'],'create')
        self.assertEqual(event['session']['job']['state'],'exited')
        self.assertEqual(event['session']['ownership'],'held')
        self.assertTrue(self.c.native.closed)

    def test_one_global_unknown_fences_run_and_control(self):
        self.start();self.c.request(self.request('cancel','request-cancel',job_id='job-one'))
        count=len(self.events)
        self.c._unknown('request-cancel','TRANSPORT')
        self.assertEqual(len(self.events),count+1)
        self.assertEqual(self.events[-1]['request_id'],'request-run')
        self.assertTrue(self.c.requests['request-run']['done'])
        self.assertTrue(self.c.requests['request-cancel']['done'])

    def test_queued_stop_survives_run_acceptance(self):
        self.c.request(self.request('run','request-run',job_id='job-one',command_utf8='sleep 1',cwd='',stdin_b64='',timeout_ms=1000))
        self.c.request(self.request('stop','request-stop'))
        self.event('accepted',request_id='request-run',op='exec',job_id='job-one')
        self.assertEqual(self.events[-1]['session']['phase'],'stopping')
        self.assertEqual(self.events[-1]['session']['controls']['stop'],{'request_id':'request-stop','ack':'pending'})
        self.event('accepted',request_id='request-stop',op='stop',job_id=None)
        self.terminal(cause='stop',stop='request-stop')
        self.event('stopping',request_id='request-stop')
        self.assertTrue(self.c.native.closed)

    def test_byte_semantics_fail_before_dispatch(self):
        previous=len(self.c.wire.sent)
        for command,cwd,stdin in [('echo\0bad','',''),('echo','../escape',''),('echo','','Zh==')]:
            self.c.request(self.request('run','request-'+str(previous)+str(len(self.events)),job_id='job-bad',
                                        command_utf8=command,cwd=cwd,stdin_b64=stdin,timeout_ms=1))
            self.assertEqual(self.events[-1]['event'],'refused')
        self.assertEqual(len(self.c.wire.sent),previous)


if __name__=='__main__':unittest.main()
