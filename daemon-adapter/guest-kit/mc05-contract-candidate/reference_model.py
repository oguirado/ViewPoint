"""PROPOSED pure transition oracle; no authentication, transport or effects.

Caller must supply the authenticated actor and validated host-event origin at an
integration boundary. Comparing IDs here is not authentication.
"""
from copy import deepcopy


class Refused(ValueError):
    pass


def require(value, code):
    if not value:
        raise Refused(code)


class ReferenceModel:
    def __init__(self, validate, owner_id, backend, isa):
        require(backend in ('firecracker','qemu') and isa in ('aarch64','x86_64'),'HOST_SELECTION')
        self.validate=validate;self.owner=owner_id;self.backend=backend;self.isa=isa
        self.session=None;self.pending=None;self.started=False;self.ownership_held=False
        self.requests=set();self.events=set();self.jobs=set()

    def submit(self,message,actor_id):
        self.validate('headless_request',message)
        require(actor_id==self.owner,'OWNER_MISMATCH')
        require(message['request_id'] not in self.requests,'DUPLICATE_REQUEST')
        require(self.pending is None,'BUSY')
        op=message['op'];params=message['params']
        if op=='create':
            require(not self.started,'OUT_OF_ORDER')
        else:
            require(self.session is not None,'UNKNOWN_WORKLOAD')
            require(params['workload_id']==self.session['workload_id'],'UNKNOWN_WORKLOAD')
            require(params['generation']==self.session['generation'],'STALE_GENERATION')
            require(self.ownership_held,'OUT_OF_ORDER')
            if op=='run':
                require(params['job_id'] not in self.jobs,'DUPLICATE_JOB')
                require(self.session['phase']=='ready' and not self.jobs,'OUT_OF_ORDER')
            else:
                require(self.session['phase'] in ('ready','completed','uncertain'),'OUT_OF_ORDER')
        self.requests.add(message['request_id'])
        self.pending={'request':deepcopy(message),'accepted':False}
        if op=='create':self.started=True;self.ownership_held=True
        if op=='run':self.jobs.add(params['job_id'])

    def _identity(self,session):
        require((session['owner_id'],session['profile_id'],session['backend'],session['isa'])==(self.owner,'headless-sumsq-v1',self.backend,self.isa),'FOREIGN_SESSION')
        if self.session is not None:
            require((session['workload_id'],session['generation'])==(self.session['workload_id'],self.session['generation']),'STALE_SESSION')

    def _job(self,session,status):
        params=self.pending['request']['params'];job=session['job']
        require(job is not None and job['job_id']==params['job_id'] and job['n']==params['n'] and job['status']==status,'JOB_CORRELATION')
        if status=='completed':
            n=params['n'];require(job['value']==n*(n+1)*(2*n+1)//6,'JOB_RESULT')

    def observe(self,message):
        self.validate('headless_event',message)
        require(message['event_id'] not in self.events,'DUPLICATE_EVENT')
        require(self.pending is not None,'NO_PENDING_REQUEST')
        request=self.pending['request'];op=request['op'];kind=message['event'];data=message['data']
        require(message['request_id']==request['request_id'],'REQUEST_CORRELATION')
        if kind=='refused':
            require(not self.pending['accepted'],'REFUSAL_AFTER_ACCEPT')
            target=(None,None) if self.session is None else (self.session['workload_id'],self.session['generation'])
            require((data['workload_id'],data['generation'])==target,'REFUSAL_CORRELATION')
            self.events.add(message['event_id']);self.pending=None
            if op=='create':self.ownership_held=False
            return
        incoming=data['session'];self._identity(incoming)
        if kind=='accepted':
            require(not self.pending['accepted'] and data['op']==op,'ACCEPT_CORRELATION')
            require(incoming['phase']=={'create':'starting','run':'running','stop':'stopping'}[op] and incoming['ownership']=='held','ACCEPT_PHASE')
            if op=='create':require(incoming['job'] is None,'CREATE_JOB')
            elif op=='run':self._job(incoming,'accepted')
            else:require(incoming['job']==self.session['job'],'STOP_JOB_CHANGED')
        elif kind=='state_changed':
            require(self.pending['accepted'],'STATE_BEFORE_ACCEPT')
            if op=='create':require(incoming['phase']=='ready' and incoming['job'] is None,'CREATE_READY')
            else:require(op=='stop' and incoming['phase']=='stopped' and incoming['job']==self.session['job'],'STOP_NOT_CONFIRMED')
        elif kind=='completed':
            require(op=='run' and self.pending['accepted'] and incoming['phase']=='completed','COMPLETE_PHASE');self._job(incoming,'completed')
        else:
            require(kind=='outcome_unknown' and data['op']==op and incoming['phase']=='uncertain' and incoming['ownership']=='held','UNKNOWN_PHASE')
            if op=='run':self._job(incoming,'unknown')
            elif op=='create':require(incoming['job'] is None,'CREATE_JOB')
            else:require(incoming['job']==self.session['job'],'STOP_JOB_CHANGED')
        self.events.add(message['event_id']);self.session=deepcopy(incoming)
        self.ownership_held=incoming['ownership']=='held'
        if kind=='accepted':self.pending['accepted']=True
        else:self.pending=None
