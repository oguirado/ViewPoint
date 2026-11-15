package harvest

// All synthetic fixture counts, costs, times and limits in this file are
// [TARGET]. They are not measurements of a host runner or a model service.

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

//go:embed testdata/repro-golden.json
var pipelineGolden []byte

type pipelineClock struct { mu sync.Mutex; now time.Time }
func (c *pipelineClock) Now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.now }
func (c *pipelineClock) advance(d time.Duration) { c.mu.Lock(); defer c.mu.Unlock(); c.now = c.now.Add(d) }

type pipelineGenerator struct {
	mu sync.Mutex
	items []Candidate
	calls int
	limits []int
	fn func(GenerationRequest) (Batch,error)
	fnFrozen func(FrozenWave,GenerationRequest) (Batch,error)
}
func (g *pipelineGenerator) Generate(_ context.Context, f FrozenWave, r GenerationRequest) (Batch,error) {
	g.mu.Lock(); defer g.mu.Unlock()
	g.calls++; g.limits = append(g.limits,r.Limit)
	if g.fnFrozen != nil { return g.fnFrozen(f,r) }
	if g.fn != nil { return g.fn(r) }
	n := r.Limit; if n > len(g.items) { n = len(g.items) }
	if n < 0 { return Batch{},errors.New("negative admission credit") }
	b := Batch{Candidates:append([]Candidate(nil),g.items[:n]...), Done:n == len(g.items)}
	g.items = g.items[n:]
	return b,nil
}

type pipelineSubmitter struct {
	mu sync.Mutex
	clock *pipelineClock
	calls []Submission
	executions map[string]Execution
	fn func(Submission) (Execution,error)
}
func (s *pipelineSubmitter) Submit(_ context.Context, x Submission) (Execution,error) {
	s.mu.Lock(); defer s.mu.Unlock()
	s.calls = append(s.calls,x); s.clock.advance(time.Second)
	if s.fn != nil { return s.fn(x) }
	e,ok := s.executions[x.Attempt.AttemptID]
	if !ok { return Execution{},errors.New("execution without supplied fixture") }
	return e,nil
}

type pipelineExtractor struct {
	mu sync.Mutex
	clock *pipelineClock
	calls []Attempt
	extractions map[string]Extraction
	fn func(Attempt,Execution) (Extraction,error)
}
func (e *pipelineExtractor) Extract(_ context.Context, a Attempt, x Execution) (Extraction,error) {
	e.mu.Lock(); defer e.mu.Unlock()
	e.calls = append(e.calls,a); e.clock.advance(time.Second)
	if e.fn != nil { return e.fn(a,x) }
	v,ok := e.extractions[a.AttemptID]
	if !ok { return Extraction{},errors.New("extract without supplied fixture") }
	return v,nil
}

// The fake adapter receives canonical bytes from its fixture, just as the
// future aggregator port will. Harvest itself does not implement this hash.
type pipelineIdentity struct { mu sync.Mutex; calls int }
func (i *pipelineIdentity) Canonical(_ context.Context,r ReproInput) (CanonicalRepro,error) {
	i.mu.Lock(); defer i.mu.Unlock(); i.calls++
	return CanonicalRepro{Normalizer:ReproNormalizer,Key:digest(r.Record),Canonical:append([]byte(nil),r.Record...)},nil
}

type pipelineJournal struct { mu sync.Mutex; records []wal.Registro }
func (j *pipelineJournal) appendRecord(r wal.Registro, details any) (uint64,error) {
	j.mu.Lock(); defer j.mu.Unlock()
	b,e := json.Marshal(details); if e != nil { return 0,e }
	r.Detalles = b; r.LSN = uint64(len(j.records)+1); j.records = append(j.records,r)
	return r.LSN,nil
}
func (j *pipelineJournal) EscribirIntencion(tx,key string,details any) (uint64,error) {
	return j.appendRecord(wal.Registro{TxID:tx,Clave:key,Clase:wal.ClaseExterna,Tipo:wal.TipoIntencion},details)
}
func (j *pipelineJournal) EscribirDesenlace(tx,key string,d wal.SrfDesenlace,rc wal.SrfRCMotor,details any) (uint64,error) {
	n := int(rc)
	return j.appendRecord(wal.Registro{TxID:tx,Clave:key,Clase:wal.ClaseExterna,Tipo:wal.TipoDesenlace,Desenlace:d,RC:&n},details)
}
func (j *pipelineJournal) EscribirResolucion(key string,d wal.SrfDesenlace,details any) (uint64,error) {
	return j.appendRecord(wal.Registro{Clave:key,Clase:wal.ClaseExterna,Tipo:wal.TipoResolucion,Desenlace:d},details)
}
func (j *pipelineJournal) Records(context.Context) ([]wal.Registro,error) {
	j.mu.Lock(); defer j.mu.Unlock()
	r := append([]wal.Registro(nil),j.records...)
	for i := range r { r[i].Detalles = append([]byte(nil),r[i].Detalles...) }
	return r,nil
}
func (j *pipelineJournal) count(kind wal.TipoRegistro) int {
	j.mu.Lock(); defer j.mu.Unlock(); n:=0
	for _,r := range j.records { if r.Tipo == kind { n++ } }; return n
}
func (j *pipelineJournal) hasDoubt() bool {
	j.mu.Lock(); defer j.mu.Unlock()
	for _,r := range j.records { if r.Tipo == wal.TipoDesenlace && r.Desenlace == wal.DesenlaceEnDuda && r.RC != nil && *r.RC == 6 { return true } }
	return false
}

type pipelineSink struct {
	*MemorySink
	mu sync.Mutex
	collected map[string]LocalReceipt
	collectCalls int
}
func (s *pipelineSink) Collect(ctx context.Context,r LocalReceipt) error {
	if e := s.MemorySink.Collect(ctx,r); e != nil { return e }
	s.mu.Lock(); defer s.mu.Unlock(); s.collectCalls++; s.collected[r.Attempt.Key] = r
	return nil
}

type pipelinePublisher struct {
	mu sync.Mutex
	sends []Publication
	queries []Publication
	loseAck bool
	unavailable bool
}
func publicationAck(p Publication) PublicationAck {
	return PublicationAck{ID:p.ID,Accepted:true,Surface:p.Surface,ConsumerVerifierSHA256:p.Capability.ConsumerVerifierSHA256}
}
func (p *pipelinePublisher) Publish(_ context.Context,x Publication) (PublicationAck,error) {
	p.mu.Lock(); defer p.mu.Unlock(); p.sends = append(p.sends,x)
	if p.loseAck { return PublicationAck{},errors.New("external write happened; acknowledgment lost") }
	return publicationAck(x),nil
}
func (p *pipelinePublisher) Reconcile(_ context.Context,x Publication) (PublicationAck,error) {
	p.mu.Lock(); defer p.mu.Unlock(); p.queries = append(p.queries,x)
	if p.unavailable { return PublicationAck{},refusal(6,"external result remains unknown") }
	return publicationAck(x),nil
}

type pipelineHarness struct {
	t testing.TB
	f FrozenWave
	cfg Config
	ports Ports
	clock *pipelineClock
	gen *pipelineGenerator
	submit *pipelineSubmitter
	extract *pipelineExtractor
	identity *pipelineIdentity
	journal *pipelineJournal
	sink *pipelineSink
	pub *pipelinePublisher
}
func newPipelineHarness(t testing.TB, ids ...string) *pipelineHarness {
	t.Helper()
	w := Wave{WaveID:"synthetic-wave",Repo:"example/repo",BaseCommit:strings.Repeat("a",40),Objective:"synthetic host harvest",GateArgv:[]string{"go","test","./..."},InputSHA256:strings.Repeat("d",64),ResourcePolicySHA256:strings.Repeat("e",64)}
	for _,id := range ids { w.Obligations = append(w.Obligations,Obligation{ID:id,Weight:1}) }
	f,e := Freeze(w); if e != nil { t.Fatal(e) }
	h := &pipelineHarness{t:t,f:f,clock:&pipelineClock{now:time.Date(2100,time.January,1,0,0,0,0,time.UTC)}}
	h.gen=&pipelineGenerator{}
	h.submit=&pipelineSubmitter{clock:h.clock,executions:map[string]Execution{}}
	h.extract=&pipelineExtractor{clock:h.clock,extractions:map[string]Extraction{}}
	h.identity=&pipelineIdentity{}
	h.journal=&pipelineJournal{}
	h.sink=&pipelineSink{MemorySink:NewMemorySink(),collected:map[string]LocalReceipt{}}
	h.pub=&pipelinePublisher{}
	h.cfg=Config{Wave:f,QueueCap:map[Stage]int{Generate:2,AdmitExecute:2,ExtractVerify:2,Deduplicate:2,CollectPublish:2},MaxAttemptsPerObligation:DefaultAttemptLimit,WaveDeadline:h.clock.Now().Add(time.Hour)}
	h.ports=Ports{Generator:h.gen,Submitter:h.submit,Extractor:h.extract,Identity:h.identity,Sink:h.sink,Publisher:h.pub,Clock:h.clock,Log:h.journal,History:h.journal,Waves:NewWaveBarrier()}
	return h
}
func (h *pipelineHarness) fixture(obligation,suffix string,rc int) Candidate {
	h.t.Helper()
	c,e,x := safeFixture(h.t,h.f,obligation,suffix,rc)
	n := uint64(7)
	e.Usage=Cost{Microunits:&n,Unit:"microcredits"}; x.Usage=e.Usage
	x.ResolutionVerified=true; x.ResolutionEvidence=[]byte("trusted synthetic host reconciliation")
	h.submit.executions[c.Attempt.AttemptID]=e
	h.extract.extractions[c.Attempt.AttemptID]=x
	return c
}
func (h *pipelineHarness) pipeline() *Pipeline {
	h.t.Helper(); p,e:=NewPipeline(h.cfg,h.ports); if e != nil { h.t.Fatal(e) }; return p
}
func (h *pipelineHarness) enablePublication() {
	h.cfg.Capability=&Capability{ConsumerVerifier:"synthetic-consumer",ConsumerVerifierSHA256:strings.Repeat("f",64),Surface:CanonicalSurface,WriterAuthorization:"example-writer-authorization"}
}
func (h *pipelineHarness) addRepro(c Candidate) {
	x:=h.extract.extractions[c.Attempt.AttemptID]
	x.Repros=[]ReproInput{{Binding:ReproBinding{Repo:h.f.Wave().Repo,Commit:h.f.Wave().BaseCommit,InputSHA256:h.f.Wave().InputSHA256,GateArgv:append([]string(nil),h.f.Wave().GateArgv...),FrameOrder:append([]string(nil),c.Attempt.FrameOrder...)},Record:append([]byte(nil),pipelineGolden...)}}
	h.extract.extractions[c.Attempt.AttemptID]=x
}
func checkPipelineBounds(t testing.TB,a Account,c Config) {
	t.Helper()
	for _,s:=range stages {
		if a.QueuePeaks[s]>c.QueueCap[s] || a.QueueHeld[s]>c.QueueCap[s] || a.QueueHeld[s]<0 { t.Fatalf("%s queue escaped cap: peak=%d held=%d cap=%d",s,a.QueuePeaks[s],a.QueueHeld[s],c.QueueCap[s]) }
	}
}
func stateFor(t testing.TB,a Account,id string) ObligationState {
	t.Helper(); for _,o:=range a.Obligations { if o.Obligation.ID==id { return o.State } }; t.Fatalf("lost obligation %q",id); return ""
}

func TestFreezeExactInventoryAndDefensiveCopies(t *testing.T) {
	h:=newPipelineHarness(t,"first","second"); f:=h.f
	w:=f.Wave(); hashes:=f.FieldSHA256(); seal:=f.SHA256()
	w.GateArgv[0]="changed"; w.Obligations[0].ID="lost"; hashes["GateArgv"]="changed"
	if f.Wave().GateArgv[0]!="go" || f.Wave().Obligations[0].ID!="first" || f.FieldSHA256()["GateArgv"]=="changed" || f.SHA256()!=seal { t.Fatal("frozen storage was mutable") }
	fields:=map[string]func(*Wave){"WaveID":func(w *Wave){w.WaveID+="-changed"},"Repo":func(w *Wave){w.Repo+="-changed"},"BaseCommit":func(w *Wave){w.BaseCommit=strings.Repeat("b",40)},"Objective":func(w *Wave){w.Objective+=" changed"},"GateArgv":func(w *Wave){w.GateArgv[1]="changed"},"InputSHA256":func(w *Wave){w.InputSHA256=strings.Repeat("b",64)},"ResourcePolicySHA256":func(w *Wave){w.ResourcePolicySHA256=strings.Repeat("b",64)},"Obligations":func(w *Wave){w.Obligations[0].Weight++}}
	for field,mutate:=range fields { t.Run(field,func(t *testing.T){w:=f.Wave();mutate(&w);g,e:=Freeze(w);if e!=nil {t.Fatal(e)};if g.FieldSHA256()[field]==f.FieldSHA256()[field] || g.SHA256()==seal {t.Fatal("changed frozen field retained its hash")}}) }
	for name,obs:=range map[string][]Obligation{"empty":nil,"zero-weight":{{ID:"o",Weight:0}},"duplicate":{{ID:"o",Weight:1},{ID:"o",Weight:1}},"overflow":{{ID:"o",Weight:math.MaxUint64},{ID:"p",Weight:1}}} {
		t.Run(name,func(t *testing.T){w:=f.Wave();w.Obligations=obs;if _,e:=Freeze(w);ExitCode(e)!=2 {t.Fatalf("inventory accepted: %v",e)}})
	}
}

func TestPipelineCompleteReceiptsRestoreEveryBoundary(t *testing.T) {
	h:=newPipelineHarness(t,"a","b","c","d")
	for _,id:=range []string{"a","b","c","d"} {h.gen.items=append(h.gen.items,h.fixture(id,id,0))}
	p:=h.pipeline(); a,e:=p.Run(context.Background())
	if e!=nil {t.Fatal(e)}
	if !a.Successful || !a.Accounted || !a.Closed || a.ExecutedAttempts!=4 || a.SubmittedAttempts!=4 || len(a.Attempts)!=4 || len(h.sink.collected)!=4 {t.Fatalf("incomplete accounting: %+v",a)}
	if a.CapacityHeld!=0 || a.Pending!=0 {t.Fatalf("completed work retains admission: %+v",a)}
	for _,s:=range stages {if a.QueueHeld[s]!=0 {t.Fatalf("%s did not hand off its credit",s)}}
	for _,r:=range a.Attempts {if !r.Collected || r.CapacityHeld || r.State!=Succeeded {t.Fatalf("receipt not independently collected: %+v",r)}}
	if a.FirstAdmitted==nil || a.CollectedAndCleaned==nil || a.UsefulHarvest.WallNanoseconds==nil || *a.UsefulHarvest.WallNanoseconds<=0 {t.Fatal("collection interval unavailable after verified cleanup")}
	if a.TotalCost.Microunits==nil || *a.TotalCost.Microunits!=28 {t.Fatalf("cost omits an attempt: %+v",a.TotalCost)}
	checkPipelineBounds(t,a,h.cfg)
}

func TestPipelineUnsafeCandidatesNeverReachSubmitter(t *testing.T) {
	cases:=map[string]func(*Candidate){"guest-inference":func(c *Candidate){c.GuestInference=true},"bind-mount":func(c *Candidate){c.BindMounts=[]string{"/host"}},"env-file":func(c *Candidate){c.EnvFile="env.txt"},"runtime-socket":func(c *Candidate){c.RuntimeSocket=true},"runner-route":func(c *Candidate){c.Route="direct-runner"},"missing-input":func(c *Candidate){c.Attempt.InputSHA256=""},"kvm-on-process":func(c *Candidate){c.Attempt.RequiresKVM=true;c.Attempt.Backend=ProcessBackend}}
	for name,mutate:=range cases {t.Run(name,func(t *testing.T){
		h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);mutate(&c);h.gen.items=[]Candidate{c}
		a,e:=h.pipeline().Run(context.Background())
		if e==nil || len(h.submit.calls)!=0 || a.Successful {t.Fatalf("unsafe candidate reached execution: err=%v account=%+v",e,a)}
		if len(a.Dispositions)==0 || a.Dispositions[0].Reason=="" {t.Fatal("candidate refusal has no explicit disposition")}
		checkPipelineBounds(t,a,h.cfg)
	})}
}

func TestPipelineSeatRefusalIsDoubtAndNeverResubmits(t *testing.T) {
	h:=newPipelineHarness(t,"a");h.gen.items=[]Candidate{h.fixture("a","a",0)}
	h.submit.fn=func(Submission)(Execution,error){return Execution{},refusal(4,"seat unavailable")}
	h.extract.fn=func(Attempt,Execution)(Extraction,error){return Extraction{},refusal(4,"receipt unavailable")}
	p:=h.pipeline()
	for i:=0;i<2;i++ {
		a,e:=p.Run(context.Background())
		if ExitCode(e)!=6 || a.Successful || a.CapacityHeld!=1 || stateFor(t,a,"a")!=Unresolved {t.Fatalf("seat uncertainty was released or relabeled: %v %+v",e,a)}
		if len(h.submit.calls)!=1 || len(a.Attempts)!=1 || h.journal.count(wal.TipoIntencion)!=1 {t.Fatal("uncertain attempt was sent or journalled twice")}
		checkPipelineBounds(t,a,h.cfg)
	}
	if !h.journal.hasDoubt() {t.Fatal("seat refusal lacks EN_DUDA rc6 outcome")}
}

func TestPipelineUnknownPurgeHoldsCreditUntilMatchingResolution(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c}
	x:=h.extract.extractions[c.Attempt.AttemptID];x.Cleanup.Known=false;x.Cleanup.Verified=false;h.extract.extractions[c.Attempt.AttemptID]=x
	p:=h.pipeline();a,e:=p.Run(context.Background())
	if ExitCode(e)!=6 || a.CapacityHeld!=1 || a.QueueHeld[ExtractVerify]!=1 || a.Successful {t.Fatalf("unknown purge accepted: %v %+v",e,a)}
	x.Cleanup.Known=true;x.Cleanup.Verified=true;h.extract.extractions[c.Attempt.AttemptID]=x
	a,e=p.Run(context.Background())
	if e!=nil || !a.Successful || a.CapacityHeld!=0 || len(h.submit.calls)!=1 {t.Fatalf("matching cleanup resolution failed or resubmitted: %v %+v",e,a)}
	if h.journal.count(wal.TipoResolucion)==0 {t.Fatal("trusted resolution was not recorded in the existing WAL")}
	checkPipelineBounds(t,a,h.cfg)
}

func TestPipelineOverproductionCannotOverflowOrExecute(t *testing.T) {
	h:=newPipelineHarness(t,"a","b");a:=h.fixture("a","a",0);b:=h.fixture("b","b",0)
	h.cfg.QueueCap[Generate]=1
	h.gen.fn=func(GenerationRequest)(Batch,error){return Batch{Candidates:[]Candidate{a,b},Done:true},nil}
	account,e:=h.pipeline().Run(context.Background())
	if ExitCode(e)!=5 || len(h.submit.calls)!=0 || len(account.Dispositions)<2 {t.Fatalf("overproduction lacks complete refusal: %v %+v",e,account)}
	checkPipelineBounds(t,account,h.cfg)
}

func TestPipelineNoInputLeavesInventoryVisible(t *testing.T) {
	h:=newPipelineHarness(t,"unproduced");a,e:=h.pipeline().Run(context.Background())
	if e==nil || a.Successful || len(h.submit.calls)!=0 || a.GeneratedCandidates!=0 || stateFor(t,a,"unproduced")!=MissingEvidence {t.Fatalf("execution or success before input: %v %+v",e,a)}
	if a.FirstAdmitted!=nil || a.UsefulHarvest.WallNanoseconds!=nil {t.Fatal("unadmitted inventory has an invented execution interval")}
}

func TestPipelineDuplicateCandidateDoesNotInflateAttempts(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c,c}
	a,_:=h.pipeline().Run(context.Background())
	if len(h.submit.calls)!=1 || a.ExecutedAttempts!=1 || len(a.Attempts)!=1 || len(h.sink.collected)!=1 {t.Fatalf("duplicate delivery inflated lifecycle: %+v",a)}
	if a.Duplicates==0 {t.Fatal("duplicate delivery not reported")}
	checkPipelineBounds(t,a,h.cfg)
}

func TestPipelineRetriesStayLinkedAndWithinBudget(t *testing.T) {
	h:=newPipelineHarness(t,"a");h.cfg.MaxCandidates=5
	cs:=[]Candidate{h.fixture("a","first",5),h.fixture("a","second",5),h.fixture("a","third",5),h.fixture("a","fourth",0)}
	for i:=1;i<len(cs);i++ {cs[i].Attempt.ParentAttemptID=cs[i-1].Attempt.AttemptID}
	next:=0
	h.gen.fn=func(r GenerationRequest)(Batch,error){
		if next==len(cs) {return Batch{Done:true},nil}
		if next>0 {ready:=false;for _,a:=range r.Attempts {if a.AttemptID==cs[next-1].Attempt.AttemptID && a.State==ExplainedFailure && !a.CapacityHeld {ready=true}};if !ready {return Batch{},nil}}
		c:=cs[next];next++;return Batch{Candidates:[]Candidate{c},Done:next==len(cs)},nil
	}
	p:=h.pipeline();var a Account;var e error
	for i:=0;i<8;i++ {a,e=p.Run(context.Background());if a.Closed {break}}
	if len(h.submit.calls)!=3 || len(a.Attempts)!=3 || a.Successful || stateFor(t,a,"a")!=ExplainedFailure {t.Fatalf("retry budget or obligation identity drifted: %v %+v",e,a)}
	for i,r:=range a.Attempts {if i>0 && r.ParentAttemptID=="" {t.Fatal("retry became an unlinked obligation")}}
	if a.TotalCost.Microunits==nil || *a.TotalCost.Microunits!=21 {t.Fatalf("retries omitted from cost: %+v",a.TotalCost)}
	checkPipelineBounds(t,a,h.cfg)
}

func TestPipelineDedupStillCollectsEveryReceiptAndCost(t *testing.T) {
	h:=newPipelineHarness(t,"a","b");h.enablePublication()
	c1,c2:=h.fixture("a","a",0),h.fixture("b","b",0)
	h.gen.items=[]Candidate{c1,c2};h.addRepro(c1);h.addRepro(c2)
	a,e:=h.pipeline().Run(context.Background())
	if e!=nil || !a.Successful {t.Fatalf("safe dedup control failed: %v %+v",e,a)}
	if a.ExecutedAttempts!=2 || len(h.sink.collected)!=2 || a.Duplicates!=1 || a.UsefulHarvest.VerifiedDistinctRepros!=1 || len(h.pub.sends)!=1 {t.Fatalf("finding dedup swallowed attempt accounting: %+v",a)}
	if a.TotalCost.Microunits==nil || *a.TotalCost.Microunits!=14 {t.Fatalf("duplicate attempt cost disappeared: %+v",a.TotalCost)}
	if h.pub.sends[0].Surface!=CanonicalSurface || h.pub.sends[0].Provenance!=HostMuse {t.Fatal("host Muse surface or provenance relabeled")}
}

func TestPipelinePublicationHoldsWithoutCompleteCapability(t *testing.T) {
	cases:=map[string]func(*pipelineHarness){"absent":func(h *pipelineHarness){h.cfg.Capability=nil},"missing-verifier":func(h *pipelineHarness){h.enablePublication();h.cfg.Capability.ConsumerVerifier=""},"missing-pin":func(h *pipelineHarness){h.enablePublication();h.cfg.Capability.ConsumerVerifierSHA256=""},"missing-writer":func(h *pipelineHarness){h.enablePublication();h.cfg.Capability.WriterAuthorization=""},"relabeled-surface":func(h *pipelineHarness){h.enablePublication();h.cfg.Capability.Surface="jules"}}
	for name,change:=range cases {t.Run(name,func(t *testing.T){
		h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c};h.addRepro(c);change(h)
		a,e:=h.pipeline().Run(context.Background())
		if e==nil || len(h.pub.sends)!=0 || a.Closed || a.Pending==0 || len(h.sink.collected)!=1 {t.Fatalf("publication bypassed prerequisite or lost local receipt: %v %+v",e,a)}
		if name!="relabeled-surface" && ExitCode(e)!=4 {t.Fatalf("missing publication prerequisite rc=%d",ExitCode(e))}
		checkPipelineBounds(t,a,h.cfg)
	})}
}

func TestPipelineLostExternalAcknowledgmentOnlyReconciles(t *testing.T) {
	h:=newPipelineHarness(t,"a");h.enablePublication();c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c};h.addRepro(c)
	h.pub.loseAck=true;h.pub.unavailable=true
	p:=h.pipeline();a,e:=p.Run(context.Background())
	if ExitCode(e)!=6 || len(h.pub.sends)!=1 || a.Closed {t.Fatalf("lost external acknowledgment not quarantined: %v %+v",e,a)}
	id:=h.pub.sends[0].ID;h.pub.unavailable=false
	a,e=p.Run(context.Background())
	if e!=nil || !a.Successful || len(h.pub.sends)!=1 || len(h.pub.queries)==0 || h.pub.queries[len(h.pub.queries)-1].ID!=id {t.Fatalf("uncertain publication replayed or failed same-identity reconciliation: %v %+v",e,a)}
	if len(h.submit.calls)!=1 || len(a.Attempts)!=1 {t.Fatal("publication reconciliation repeated execution")}
}

func TestPipelinePersistedIntentionIsQueryOnly(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c}
	_,e:=h.journal.EscribirIntencion(h.f.Wave().WaveID,c.Attempt.Key,map[string]any{"wave_sha256":h.f.SHA256(),"attempt":c.Attempt})
	if e!=nil {t.Fatal(e)}
	a,e:=h.pipeline().Run(context.Background())
	if len(h.submit.calls)!=0 || h.journal.count(wal.TipoIntencion)!=1 {t.Fatal("recovered intention was sent again")}
	if e!=nil || !a.Successful || h.journal.count(wal.TipoResolucion)!=1 {t.Fatalf("trusted recovered receipt did not resolve existing lifecycle: %v %+v",e,a)}
}

func TestPipelineAccountNeverConfusesAccountedWithSuccessful(t *testing.T) {
	for name,codes:=range map[string][]int{"all-failed":{5,5},"mixed":{0,5},"complete":{0,0}} {t.Run(name,func(t *testing.T){
		h:=newPipelineHarness(t,"a","b");for i,id:=range []string{"a","b"} {h.gen.items=append(h.gen.items,h.fixture(id,id,codes[i]))}
		a,e:=h.pipeline().Run(context.Background())
		if !a.Accounted || !a.Closed || len(a.Obligations)!=2 {t.Fatalf("terminal suite lost accounting: %v %+v",e,a)}
		want:=name=="complete";if a.Successful!=want || (want && e!=nil) || (!want && e==nil) {t.Fatalf("suite success misreported: %v %+v",e,a)}
	})}
	h:=newPipelineHarness(t,"a","missing");h.gen.items=[]Candidate{h.fixture("a","a",0)}
	a,e:=h.pipeline().Run(context.Background())
	if e==nil || a.Successful || stateFor(t,a,"missing")!=MissingEvidence || len(a.Obligations)!=2 {t.Fatalf("unproduced obligation disappeared: %v %+v",e,a)}
}

func TestPipelineMissingUsageIsNullWithAUSENTE(t *testing.T) {
	h:=newPipelineHarness(t,"a","b");c1,c2:=h.fixture("a","a",0),h.fixture("b","b",0);h.gen.items=[]Candidate{c1,c2}
	x:=h.extract.extractions[c2.Attempt.AttemptID];x.Usage=MissingCost();h.extract.extractions[c2.Attempt.AttemptID]=x
	ex:=h.submit.executions[c2.Attempt.AttemptID];ex.Usage=MissingCost();h.submit.executions[c2.Attempt.AttemptID]=ex
	a,e:=h.pipeline().Run(context.Background())
	if e!=nil {t.Fatal(e)}
	if a.TotalCost.Microunits!=nil || a.TotalCost.Reason!="AUSENTE" {t.Fatalf("unknown usage coerced into zero/partial total: %+v",a.TotalCost)}
	unknown:=0;for _,r:=range a.Attempts {if r.Usage.Microunits==nil {unknown++;if r.Usage.Reason!="AUSENTE" {t.Fatal("missing attempt usage has no reason")}}};if unknown!=1 {t.Fatalf("unknown usage records=%d",unknown)}
}

func TestPipelineDeadlineAndCancellationNeverAdmitWork(t *testing.T) {
	t.Run("deadline",func(t *testing.T){
		h:=newPipelineHarness(t,"a");h.gen.items=[]Candidate{h.fixture("a","a",0)};h.cfg.WaveDeadline=h.clock.Now().Add(-time.Nanosecond)
		p,e:=NewPipeline(h.cfg,h.ports);if e==nil {_,e=p.Run(context.Background())}
		if e==nil || len(h.submit.calls)!=0 {t.Fatalf("expired wave admitted work: %v",e)}
	})
	t.Run("cancelled",func(t *testing.T){
		h:=newPipelineHarness(t,"a");h.gen.items=[]Candidate{h.fixture("a","a",0)};p:=h.pipeline();ctx,cancel:=context.WithCancel(context.Background());cancel()
		_,e:=p.Run(ctx);if e==nil || len(h.submit.calls)!=0 {t.Fatalf("cancelled wave admitted work: %v",e)}
	})
}

func TestPipelineRejectsInvalidConfiguration(t *testing.T) {
	cases:=map[string]func(*pipelineHarness){"queue-zero":func(h *pipelineHarness){h.cfg.QueueCap[Generate]=0},"missing-stage":func(h *pipelineHarness){delete(h.cfg.QueueCap,ExtractVerify)},"negative-attempts":func(h *pipelineHarness){h.cfg.MaxAttemptsPerObligation=-1},"attempts-over-cap":func(h *pipelineHarness){h.cfg.MaxAttemptsPerObligation=4},"zero-deadline":func(h *pipelineHarness){h.cfg.WaveDeadline=time.Time{}},"missing-journal":func(h *pipelineHarness){h.ports.Log=nil},"missing-history":func(h *pipelineHarness){h.ports.History=nil},"missing-barrier":func(h *pipelineHarness){h.ports.Waves=nil}}
	for name,change:=range cases {t.Run(name,func(t *testing.T){h:=newPipelineHarness(t,"a");change(h);if _,e:=NewPipeline(h.cfg,h.ports);e==nil {t.Fatal("invalid configuration accepted")}})}
}

func TestExitCodeNeverUsesLegacyOne(t *testing.T) {
	for _,e:=range []error{nil,errors.New("setup"),context.Canceled,context.DeadlineExceeded,&Error{Code:1},&Error{Code:9},&Error{Code:4}} {n:=ExitCode(e);if n!=0 && (n<2 || n>7) {t.Fatalf("forbidden exit code %d",n)}}
}

func TestPipelineReturnedAccountCannotMutateLifecycle(t *testing.T) {
	h:=newPipelineHarness(t,"a");h.gen.items=[]Candidate{h.fixture("a","a",0)};p:=h.pipeline();a,e:=p.Run(context.Background());if e!=nil {t.Fatal(e)}
	baseline:=a
	a.Obligations[0].Obligation.ID="forged";a.Attempts[0].Receipt[0]='x';a.QueueHeld[Generate]=99
	b,e:=p.Run(context.Background());if e!=nil {t.Fatal(e)}
	if stateFor(t,b,"a")!=Succeeded || b.QueueHeld[Generate]!=0 || b.Attempts[0].Receipt[0]=='x' {t.Fatal("returned account mutated authoritative state")}
	if !reflect.DeepEqual(b.FirstAdmitted,baseline.FirstAdmitted) || len(h.submit.calls)!=1 {t.Fatal("completed wave was admitted again")}
}

func TestPipelineConcurrentRunHasOneDispatch(t *testing.T) {
	h:=newPipelineHarness(t,"a");h.gen.items=[]Candidate{h.fixture("a","a",0)};p:=h.pipeline()
	type answer struct {account Account;err error};done:=make(chan answer,2)
	for i:=0;i<2;i++ {go func(){a,e:=p.Run(context.Background());done<-answer{a,e}}()}
	for i:=0;i<2;i++ {r:=<-done;if r.err!=nil || !r.account.Successful {t.Fatalf("serialized Run failed: %v %+v",r.err,r.account)}}
	if len(h.submit.calls)!=1 || h.journal.count(wal.TipoIntencion)!=1 {t.Fatal("concurrent Run duplicated lifecycle")}
}

func TestPipelineNextWaveWaitsForActiveCleanup(t *testing.T) {
	first:=newPipelineHarness(t,"first");c:=first.fixture("first","first",0);first.gen.items=[]Candidate{c}
	x:=first.extract.extractions[c.Attempt.AttemptID];x.Cleanup.Verified=false;x.Cleanup.Known=false;first.extract.extractions[c.Attempt.AttemptID]=x
	p1:=first.pipeline();a,e:=p1.Run(context.Background());if ExitCode(e)!=6 || a.Closed {t.Fatalf("setup did not retain active wave: %v %+v",e,a)}
	next:=newPipelineHarness(t,"next");w:=next.f.Wave();w.WaveID="next-wave";f,e:=Freeze(w);if e!=nil {t.Fatal(e)};next.f=f;next.cfg.Wave=f
	next.ports.Waves=first.ports.Waves;next.ports.Log=first.journal;next.ports.History=first.journal
	next.gen.items=[]Candidate{next.fixture("next","next",0)};p2:=next.pipeline()
	ctx,cancel:=context.WithCancel(context.Background());entered:=make(chan struct{});finished:=make(chan error,1)
	go func(){close(entered);_,e:=p2.Run(ctx);finished<-e}();<-entered;cancel()
	if e:=<-finished;e==nil {t.Fatal("next wave crossed unresolved active-wave barrier")}
	if next.gen.calls!=0 || len(next.submit.calls)!=0 {t.Fatal("cross-wave generation occurred before verified closure")}
	x.Cleanup.Known=true;x.Cleanup.Verified=true;first.extract.extractions[c.Attempt.AttemptID]=x
	if a,e:=p1.Run(context.Background());e!=nil || !a.Closed {t.Fatalf("active wave did not close: %v %+v",e,a)}
	p3:=next.pipeline();a,e=p3.Run(context.Background());if e!=nil || !a.Successful || len(next.submit.calls)!=1 {t.Fatalf("closed wave did not restore generation barrier: %v %+v",e,a)}
}

func TestPipelineInjectedCapabilityReleasesPublicationHold(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c};h.addRepro(c)
	p:=h.pipeline();before,e:=p.Run(context.Background())
	if ExitCode(e)!=4 || before.Closed || len(h.sink.collected)!=1 || len(h.pub.sends)!=0 {t.Fatalf("missing capability did not preserve local HOLD: %v %+v",e,before)}
	cap:=Capability{ConsumerVerifier:"synthetic-consumer",ConsumerVerifierSHA256:strings.Repeat("f",64),Surface:CanonicalSurface,WriterAuthorization:"example-writer-authorization"}
	if e:=p.SetCapability(cap);e!=nil {t.Fatal(e)}
	a,e:=p.Run(context.Background())
	if e!=nil || !a.Successful || !a.Closed || len(h.pub.sends)!=1 || len(h.submit.calls)!=1 {t.Fatalf("capability did not release only publication: %v %+v",e,a)}
	if !reflect.DeepEqual(a.FirstAdmitted,before.FirstAdmitted) || !reflect.DeepEqual(a.CollectedAndCleaned,before.CollectedAndCleaned) {t.Fatal("publication changed verified collection interval")}
	cap.ConsumerVerifierSHA256=strings.Repeat("a",64)
	if e:=p.SetCapability(cap);e==nil {t.Fatal("pinned publication capability changed after reservation")}
}

func TestPipelineHistoricBindingMismatchStaysQuarantined(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c}
	_,e:=h.journal.EscribirIntencion(h.f.Wave().WaveID,c.Attempt.Key,map[string]any{"wave_sha256":strings.Repeat("0",64),"attempt":c.Attempt})
	if e!=nil {t.Fatal(e)}
	p:=h.pipeline()
	for i:=0;i<2;i++ {
		a,e:=p.Run(context.Background())
		if ExitCode(e)!=6 || a.Successful || a.Closed || a.CapacityHeld!=1 || stateFor(t,a,"a")!=Unresolved {t.Fatalf("historic binding drift escaped quarantine on invocation %d: %v %+v",i,e,a)}
		if len(h.submit.calls)!=0 || len(h.extract.calls)!=0 || h.journal.count(wal.TipoIntencion)!=1 {t.Fatal("historic mismatched identity was submitted, queried as current work, or reserved twice")}
	}
}

func TestPipelineCancellationDuringExtractionNeverDispatchesFollowingWork(t *testing.T) {
	h:=newPipelineHarness(t,"a","b");c1,c2:=h.fixture("a","a",0),h.fixture("b","b",0);h.gen.items=[]Candidate{c1,c2}
	ctx,cancel:=context.WithCancel(context.Background())
	h.extract.fn=func(Attempt,Execution)(Extraction,error){cancel();return Extraction{},context.Canceled}
	p:=h.pipeline();a,e:=p.Run(ctx)
	if ExitCode(e)!=6 || a.CapacityHeld!=1 || len(h.submit.calls)!=1 {t.Fatalf("cancelled extraction caused a new dispatch: %v %+v",e,a)}
	if h.submit.calls[0].Attempt.Key!=c1.Attempt.Key {t.Fatal("unexpected first dispatch")}
	h.extract.fn=nil
	a,e=p.Run(context.Background())
	if e!=nil || !a.Successful || len(h.submit.calls)!=2 {t.Fatalf("resumed collection did not complete original inventory: %v %+v",e,a)}
	firstSends:=0;for _,s:=range h.submit.calls {if s.Attempt.Key==c1.Attempt.Key {firstSends++}}
	if firstSends!=1 {t.Fatal("cancelled attempt was resubmitted during resolution")}
}

func TestPipelineGeneratorMutationCannotChangeFrozenDispatch(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);expectedSeal:=h.f.SHA256()
	h.gen.fnFrozen=func(f FrozenWave,r GenerationRequest)(Batch,error){
		view:=f.Wave();view.GateArgv[0]="forged-accessor";view.Obligations[0].ID="forged-inventory"
		// Even an adapter in this package receives independent backing storage.
		f.wave.GateArgv[0]="forged-private-copy";f.wave.Obligations[0].ID="forged-copy"
		f.fields["Objective"]="forged-hash"
		return Batch{Candidates:[]Candidate{c},Done:true},nil
	}
	a,e:=h.pipeline().Run(context.Background())
	if e!=nil || !a.Successful || stateFor(t,a,"a")!=Succeeded {t.Fatalf("generator mutated authoritative wave: %v %+v",e,a)}
	if len(h.submit.calls)!=1 || h.submit.calls[0].Wave.SHA256()!=expectedSeal || h.submit.calls[0].Wave.Wave().GateArgv[0]!="go" {t.Fatal("mutated objective or argv entered submit")}
}

func TestPipelineRecoveredFailureCannotBeErasedByLaterDoneReceipt(t *testing.T) {
	h:=newPipelineHarness(t,"a");failed:=h.fixture("a","a",5);h.gen.items=[]Candidate{failed}
	original:=append([]byte(nil),h.extract.extractions[failed.Attempt.AttemptID].Receipt...)
	a,e:=h.pipeline().Run(context.Background())
	if e==nil || !a.Closed || stateFor(t,a,"a")!=ExplainedFailure {t.Fatalf("failed receipt setup did not close: %v %+v",e,a)}
	changed:=h.fixture("a","a",0);h.gen.items=[]Candidate{changed}
	p:=h.pipeline()
	for i:=0;i<2;i++ {
		a,e=p.Run(context.Background())
		if ExitCode(e)!=6 || a.Successful || len(h.submit.calls)!=1 {t.Fatalf("later receipt erased persisted failure: %v %+v",e,a)}
	}
	retained:=false;for _,r:=range h.sink.MemorySink.Receipts(failed.Attempt.Key) {if bytes.Equal(r.Receipt,original) {retained=true}}
	if !retained {t.Fatal("contradictory delivery erased original failed receipt")}
}

func TestPipelineUnknownAttemptCannotResolveFromForgedReceiptAndCleanupAlone(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",0);h.gen.items=[]Candidate{c}
	x:=h.extract.extractions[c.Attempt.AttemptID];x.Receipt=[]byte(`{"done":true}`);x.ResolutionVerified=false;x.ResolutionEvidence=nil;h.extract.extractions[c.Attempt.AttemptID]=x
	_,e:=h.journal.EscribirIntencion(h.f.Wave().WaveID,c.Attempt.Key,map[string]any{"wave_sha256":h.f.SHA256(),"attempt":c.Attempt})
	if e!=nil {t.Fatal(e)}
	a,e:=h.pipeline().Run(context.Background())
	if ExitCode(e)!=6 || a.Successful || a.CapacityHeld!=1 || h.journal.count(wal.TipoResolucion)!=0 {t.Fatalf("cleanup plus forged receipt resolved unknown execution: %v %+v",e,a)}
	if len(h.submit.calls)!=0 {t.Fatal("unknown execution was replayed")}
}

func TestPipelineUnresolvedFailureReceiptRemainsVisibleAfterLateSuccess(t *testing.T) {
	h:=newPipelineHarness(t,"a");c:=h.fixture("a","a",5);h.gen.items=[]Candidate{c}
	x:=h.extract.extractions[c.Attempt.AttemptID];original:=append([]byte(nil),x.Receipt...);x.Cleanup.Known=false;x.Cleanup.Verified=false;h.extract.extractions[c.Attempt.AttemptID]=x
	p:=h.pipeline();a,e:=p.Run(context.Background())
	if ExitCode(e)!=6 || a.CapacityHeld!=1 {t.Fatalf("unresolved failure setup did not hold: %v %+v",e,a)}
	retained:=false;for _,r:=range h.sink.MemorySink.Receipts(c.Attempt.Key) {if bytes.Equal(r.Receipt,original) {retained=true}}
	if !retained {t.Fatal("unknown purge suppressed the original attempt receipt")}
	h.fixture("a","a",0)
	later:=append([]byte(nil),h.extract.extractions[c.Attempt.AttemptID].Receipt...)
	a,e=p.Run(context.Background())
	if e==nil || a.Successful || len(h.submit.calls)!=1 {t.Fatalf("late success erased contradictory same-attempt failure: %v %+v",e,a)}
	if len(a.Attempts)!=1 {t.Fatalf("contradictory receipt changed attempt count: %d",len(a.Attempts))}
	sawOriginal,sawLater:=false,false
	for _,raw:=range a.Attempts[0].ReceiptEvidence {sawOriginal=sawOriginal || bytes.Equal(raw,original);sawLater=sawLater || bytes.Equal(raw,later)}
	if !a.Attempts[0].FailureObserved || a.Failures!=1 || len(a.Attempts[0].ReceiptEvidence)!=2 || !sawOriginal || !sawLater {t.Fatalf("failure accounting or exact raw evidence lost after late success: %+v",a)}
	retained=false;for _,r:=range h.sink.MemorySink.Receipts(c.Attempt.Key) {if bytes.Equal(r.Receipt,original) {retained=true}}
	if !retained {t.Fatal("late result replaced retained failure evidence")}
}
