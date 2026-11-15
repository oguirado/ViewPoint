package proto1harvest

// All synthetic fixture costs, timestamps and capacity bounds are TARGET, not
// host observations. No guest, network transport, display or frame is started.

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
	"opensuperficies.local/srf/src/w8wave/aggregator"
	"opensuperficies.local/srf/src/w8wave/harvest"
)

func testHash(data []byte) string { sum:=sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func testJSON(t *testing.T, v any) []byte { t.Helper(); b,e:=json.Marshal(v);if e!=nil {t.Fatal(e)};return b }
func testObject(t *testing.T,b []byte) map[string]any { t.Helper();var v map[string]any;if e:=json.Unmarshal(b,&v);e!=nil {t.Fatal(e)};return v }
func testMap(t *testing.T,v any) map[string]any {t.Helper();m,ok:=v.(map[string]any);if !ok {t.Fatalf("expected fixture object, got %T",v)};return m}

// The existing independent codec fixture supplies the A4 isolation facts. The
// task/seat/receipt identities, canonical hashes and tar payload are rebuilt
// with standard-library arithmetic, not from the module under test.
func makeFixture(t *testing.T, wave harvest.Wave, obligation,suffix string,rc int) (harvest.Candidate,harvest.Execution,harvest.Extraction) {
	t.Helper()
	wire,e:=os.ReadFile("../../../testdata/workerbridge/codec.json")
	if e!=nil { t.Fatalf("AUSENTE: independent producer fixture: %v",e) }
	codec:=testObject(t,wire)
	grant:=testMap(t,codec["grant"])
	receipt:=testMap(t,codec["receipt"])
	taskID,attemptID,leaseID:="task-"+obligation,"attempt-"+suffix,"lease-"+suffix
	nonce:=testHash([]byte("nonce:"+obligation+":"+suffix))
	tuple:=testJSON(t,[]string{taskID,attemptID,leaseID})
	key:=testHash(tuple)
	repo:=map[string]any{"url":"https://github.com/"+wave.Repo,"commit":wave.BaseCommit}
	worker:=map[string]any{
		"schema":"os.cu.worker-task/v1","task_id":taskID,"attempt_id":attemptID,"lease_id":leaseID,
		"repo":repo,"objective":wave.Objective,
		"sources":[]any{map[string]any{"path":"input.txt","sha256":wave.InputSHA256}},
		"write_paths":[]any{"report.txt"},
		"gate":map[string]any{"argv":wave.GateArgv,"cwd":".","timeout_ms":10000},
		"evidence":map[string]any{"requests":[]any{},"max_bytes":102400},
		"consumer":map[string]any{"name":"auto_ingest"},
		"host_metric":map[string]any{"name":"elapsed_ms","unit":"ms","required":false,"target_max":nil},
	}
	workerWire:=testJSON(t,worker)
	a:=harvest.Attempt{Key:key,ObligationID:obligation,TaskID:taskID,AttemptID:attemptID,LeaseID:leaseID,Nonce:nonce,
		WorkerTask:workerWire,InputSHA256:wave.InputSHA256,ResourcePolicySHA256:wave.ResourcePolicySHA256,Backend:harvest.ProcessBackend}
	for _,doc:=range []map[string]any{grant,receipt} {
		doc["task_id"]=taskID;doc["attempt_id"]=attemptID;doc["lease_id"]=leaseID;doc["lease_nonce"]=nonce
		doc["task_sha256"]=testHash(workerWire)
	}
	grant["repo_commit"]=wave.BaseCommit
	runtime:=testMap(t,grant["runtime"]); runtime["commit"]=wave.BaseCommit
	receipt["runtime"]=runtime;receipt["repo"]=repo
	receipt["seat_grant_sha256"]=testHash(testJSON(t,grant))
	gate:=testMap(t,receipt["gate"]);gate["argv"]=wave.GateArgv
	receipt["sources_observed"]=[]any{map[string]any{"path":"input.txt","sha256":wave.InputSHA256}}
	receipt["evidence"]=map[string]any{"answers":[]any{},"notes":"Independent in-memory host fixture; no host launch."}
	body:=[]byte("verified text never persisted as a frame\n")
	var buffer bytes.Buffer
	tarWriter:=tar.NewWriter(&buffer)
	if e:=tarWriter.WriteHeader(&tar.Header{Name:"report.txt",Mode:0644,Size:int64(len(body)),Typeflag:tar.TypeReg});e!=nil {t.Fatal(e)}
	if _,e:=tarWriter.Write(body);e!=nil {t.Fatal(e)}
	if e:=tarWriter.Close();e!=nil {t.Fatal(e)}
	payload:=buffer.Bytes()
	extract:=testMap(t,receipt["extract"]);extract["entries"]=1;extract["bytes"]=len(payload);extract["sha256"]=testHash(payload)
	receipt["output"]=map[string]any{"kind":"branch","repo_url":repo["url"],"branch":"harvest/fixture","base_commit":wave.BaseCommit,
		"head_commit":strings.Repeat("b",40),"files":[]any{map[string]any{"path":"report.txt","operation":"add","mode":"100644","bytes":len(body),"sha256":testHash(body)}}}
	receipt["exit_code"]=rc
	if rc!=0 {receipt["failure_reason"]="gate";receipt["output"]=nil;gate["raw_exit_code"]=1;gate["exit_code"]=5}
	grantWire:=testJSON(t,grant)
	c:=harvest.Candidate{ID:"candidate-"+obligation+"-"+suffix,Attempt:a,Route:harvest.MuseSeatRoute}
	ex:=harvest.Execution{Accepted:true,Handle:key,Grant:grantWire,Usage:harvest.MissingCost()}
	x:=harvest.Extraction{AttemptKey:key,Nonce:nonce,HostValidated:true,A4Validated:true,Receipt:testJSON(t,receipt),Grant:append([]byte(nil),grantWire...),
		Payload:append([]byte(nil),payload...),Files:map[string][]byte{"report.txt":body},
		Cleanup:harvest.CleanupProof{AttemptKey:key,LeaseID:leaseID,Nonce:nonce,Known:true,Verified:true},Usage:harvest.MissingCost()}
	return c,ex,x
}

type fixtureGenerator struct{ items []harvest.Candidate; calls int }
func (g *fixtureGenerator) Generate(ctx context.Context,_ harvest.FrozenWave,r harvest.GenerationRequest)(harvest.Batch,error) {
	if e:=ctx.Err();e!=nil{return harvest.Batch{},e}
	g.calls++
	start:=int(r.Cursor);if start>len(g.items) {return harvest.Batch{},errors.New("generator cursor drift")}
	end:=start+r.Limit;if end>len(g.items){end=len(g.items)}
	return harvest.Batch{Candidates:append([]harvest.Candidate(nil),g.items[start:end]...),Done:end==len(g.items)},nil
}
type fixtureSubmitter struct{ sends []harvest.Submission; byAttempt map[string]harvest.Execution; loseSeat bool }
func (s *fixtureSubmitter) Submit(ctx context.Context,v harvest.Submission)(harvest.Execution,error){
	if e:=ctx.Err();e!=nil{return harvest.Execution{},e}
	s.sends=append(s.sends,v)
	if s.loseSeat{return harvest.Execution{},errors.New("seat lost after irreversible submit")}
	ex,ok:=s.byAttempt[v.Attempt.AttemptID];if !ok{return harvest.Execution{},errors.New("AUSENTE: independent seat execution")};return ex,nil
}
type fixtureExtractor struct{ byAttempt map[string]harvest.Extraction; unavailable bool; calls int }
func (e *fixtureExtractor) Extract(ctx context.Context,a harvest.Attempt,_ harvest.Execution)(harvest.Extraction,error){
	if err:=ctx.Err();err!=nil{return harvest.Extraction{},err}
	e.calls++;if e.unavailable{return harvest.Extraction{},errors.New("AUSENTE: trusted extraction")}
	v,ok:=e.byAttempt[a.AttemptID];if !ok{return harvest.Extraction{},errors.New("AUSENTE: trusted attempt receipt")};return v,nil
}
type fixtureClock struct { value time.Time }
func (c fixtureClock) Now()time.Time{return c.value}
type actualWALHistory struct{path string}
func (h actualWALHistory) Records(ctx context.Context)([]wal.Registro,error){if e:=ctx.Err();e!=nil{return nil,e};return wal.LeerRegistros(h.path)}
// Spy wraps, rather than replaces, the real in-memory harvest Sink. It has
// no file path and never writes extracted payload/frame bytes to disk.
type sinkSpy struct{ *harvest.MemorySink; collections int }
func (s *sinkSpy) Collect(ctx context.Context, r harvest.LocalReceipt)error{if e:=s.MemorySink.Collect(ctx,r);e!=nil{return e};s.collections++;return nil}
type publisherSpy struct{ sends []harvest.Publication; queries []harvest.Publication; loseAck bool }
func (p *publisherSpy) Publish(ctx context.Context, b harvest.Publication)(harvest.PublicationAck,error){
	if e:=ctx.Err();e!=nil{return harvest.PublicationAck{},e}
	p.sends=append(p.sends,b);if p.loseAck{return harvest.PublicationAck{},errors.New("ack lost")}
	return harvest.PublicationAck{ID:b.ID,Accepted:true,Surface:b.Surface,ConsumerVerifierSHA256:b.Capability.ConsumerVerifierSHA256},nil
}
func (p *publisherSpy) Reconcile(ctx context.Context,b harvest.Publication)(harvest.PublicationAck,error){
	if e:=ctx.Err();e!=nil{return harvest.PublicationAck{},e}
	p.queries=append(p.queries,b)
	return harvest.PublicationAck{ID:b.ID,Accepted:true,Surface:b.Surface,ConsumerVerifierSHA256:b.Capability.ConsumerVerifierSHA256},nil
}

type harness struct {
	wave harvest.Wave
	frozen harvest.FrozenWave
	cfg Config
	generator *fixtureGenerator
	submitter *fixtureSubmitter
	extractor *fixtureExtractor
	sink *sinkSpy
	publisher *publisherSpy
	aggregator *aggregator.Aggregator
	journal *wal.LibroConsumo
	walPath string
	golden []byte
}
func newHarness(t *testing.T, obligations ...string)*harness {
	t.Helper()
	h:=&harness{wave:harvest.Wave{WaveID:"proto1-wave",Repo:"example/repo",BaseCommit:strings.Repeat("a",40),Objective:"bounded offline receipt",GateArgv:[]string{"go","test","./..."},InputSHA256:strings.Repeat("d",64),ResourcePolicySHA256:strings.Repeat("e",64)}}
	for _,o:=range obligations{h.wave.Obligations=append(h.wave.Obligations,harvest.Obligation{ID:o,Weight:1})}
	var e error;h.frozen,e=harvest.Freeze(h.wave);if e!=nil{t.Fatal(e)}
	h.golden,e=os.ReadFile("../harvest/testdata/repro-golden.json");if e!=nil{t.Fatalf("AUSENTE: producer repro fixture: %v",e)}
	h.generator=&fixtureGenerator{}
	h.submitter=&fixtureSubmitter{byAttempt:map[string]harvest.Execution{}}
	h.extractor=&fixtureExtractor{byAttempt:map[string]harvest.Extraction{}}
	h.sink=&sinkSpy{MemorySink:harvest.NewMemorySink()}
	h.publisher=&publisherSpy{}
	h.walPath=filepath.Join(t.TempDir(),"consumption.wal")
	h.journal,e=wal.AbrirLibro(h.walPath);if e!=nil {t.Fatal(e)}
	t.Cleanup(func(){if e:=h.journal.Cerrar();e!=nil {t.Error(e)}})
	bindings:=[]aggregator.Binding{}
	for _,o:=range obligations {bindings=append(bindings,aggregator.Binding{TaskID:"task-"+o,AttemptID:"attempt-"+o,LeaseID:"lease-"+o})}
	h.aggregator,e=aggregator.NewAggregator(aggregator.Config{WaveID:h.wave.WaveID,BaseSeq:0,AllowedBindings:bindings});if e!=nil{t.Fatal(e)}
	h.cfg=Config{Harvest:harvest.Config{Wave:h.frozen,QueueCap:map[harvest.Stage]int{
		harvest.Generate:2,harvest.AdmitExecute:2,harvest.ExtractVerify:2,harvest.Deduplicate:2,harvest.CollectPublish:2,
	},MaxAttemptsPerObligation:harvest.DefaultAttemptLimit,WaveDeadline:time.Date(2100,1,1,1,0,0,0,time.UTC),MaxCandidates:len(obligations)},
		Ports:harvest.Ports{Generator:h.generator,Submitter:h.submitter,Extractor:h.extractor,Sink:h.sink,Publisher:h.publisher,Clock:fixtureClock{value:time.Date(2100,1,1,0,0,0,0,time.UTC)},Log:h.journal,History:actualWALHistory{path:h.walPath},Waves:harvest.NewWaveBarrier()},
		Aggregator:h.aggregator,MaxObservations:16}
	h.cfg.Harvest.Capability=&harvest.Capability{ConsumerVerifier:"trusted-consumer",ConsumerVerifierSHA256:strings.Repeat("f",64),Surface:harvest.CanonicalSurface,WriterAuthorization:"example-separately-injected-publisher-grant"}
	return h
}
func (h *harness) candidate(t *testing.T, obligation string,rc int,repro bool) harvest.Candidate {
	t.Helper()
	c,ex,x:=makeFixture(t,h.wave,obligation,obligation,rc)
	if repro {
		x.Repros=[]harvest.ReproInput{{Binding:harvest.ReproBinding{Repo:h.wave.Repo,Commit:h.wave.BaseCommit,InputSHA256:h.wave.InputSHA256,GateArgv:append([]string(nil),h.wave.GateArgv...)},Record:append([]byte(nil),h.golden...)}}
	}
	h.submitter.byAttempt[c.Attempt.AttemptID]=ex
	h.extractor.byAttempt[c.Attempt.AttemptID]=x
	return c
}
func (h *harness) flow(t *testing.T)*Flow {t.Helper();p,e:=New(h.cfg);if e!=nil {t.Fatal(e)};return p}
func testBounds(t *testing.T,account harvest.Account,cfg Config){t.Helper();for s,limit:=range cfg.Harvest.QueueCap{if account.QueuePeaks[s]>limit || account.QueueHeld[s]>limit {t.Fatalf("stage %s exceeded credit cap: peak=%d held=%d limit=%d",s,account.QueuePeaks[s],account.QueueHeld[s],limit)}}}
func testRefusal(t *testing.T,err error,rc int,reason string){t.Helper();if err==nil||harvest.ExitCode(err)!=rc||err.Error()!=reason{t.Fatalf("refusal mismatch: got %v (rc %d), expected %q (rc %d)",err,harvest.ExitCode(err),reason,rc)}}

func TestProto1Harvestflow(t *testing.T) {
	t.Run("positive-real-pipeline-aggregator-and-independent-witnesses",func(t *testing.T){
		h:=newHarness(t,"a","b")
		for _,o:=range []string{"a","b"}{h.generator.items=append(h.generator.items,h.candidate(t,o,0,true))}
		flow:=h.flow(t);r,e:=flow.Run(context.Background());if e!=nil{t.Fatalf("independent green control: %v",e)}
		if !r.Completion.Accounted||!r.Completion.Closed||!r.Completion.DoneSuccess||r.Completion.ExitCode!=0{t.Fatalf("A-completion not successful: %+v",r.Completion)}
		if r.Harvest.SubmittedAttempts!=2||len(h.submitter.sends)!=2||len(r.Aggregate.AttemptReceipts)!=2||len(r.Attempts)!=2{t.Fatalf("every attempt must have a real receipt and one send: %+v",r)}
		if len(h.sink.Findings())!=1||r.Aggregate.UniqueManifestationCount!=1||len(r.Aggregate.Manifestations[0].Observations)!=2||len(h.publisher.sends)!=1||len(h.sink.Publications())!=1{t.Fatalf("equal canonical bytes did not union authenticated witnesses: %+v",r.Aggregate)}
		if r.Aggregate.TotalCostMicroUSD!=nil||r.Aggregate.CostReason!=aggregator.ReasonAusente||r.Harvest.TotalCost.Microunits!=nil{t.Fatalf("unknown cost became zero: %+v %+v",r.Aggregate,r.Harvest.TotalCost)}
		if h.sink.collections<2 {t.Fatalf("sink spy lost receipt collection: %d",h.sink.collections)}
		for _,item:=range r.Attempts {if item.NativeGateRawExit==nil || *item.NativeGateRawExit!=0 || item.NormalizedExit!=0 || item.ReceiptSHA256=="" {t.Fatalf("native gate and normalized rc not independent: %+v",item)}}
		for _,o:=range []string{"a","b"} {x:=h.extractor.byAttempt["attempt-"+o];want:=testHash(x.Receipt);seen:=false;for _,ref:=range r.Aggregate.AttemptReceipts {seen=seen||ref.ReceiptSHA256==want};if !seen {t.Fatalf("missing independently computed SHA256 for %s",o)}}
		id:=testHash(h.golden);if r.Aggregate.Manifestations[0].ReproSHA256!=id {t.Fatalf("C3 identity differs from independent SHA256: got %s expected %s",r.Aggregate.Manifestations[0].ReproSHA256,id)}
		for _,path:=range []string{h.walPath,h.walPath+".flock"} {if _,e:=os.Stat(path);e!=nil {t.Fatalf("actual shared WAL missing: %s: %v",path,e)}}
		before:=h.aggregator.Summary();again,e:=flow.Run(context.Background());if e!=nil||!again.Completion.DoneSuccess||len(h.submitter.sends)!=2||len(h.publisher.sends)!=1||!reflect.DeepEqual(before,again.Aggregate){t.Fatalf("idempotent Run redelivered or changed summary: %v %+v",e,again.Completion)}
		testBounds(t,r.Harvest,h.cfg)
		t.Logf("GREEN positive: sends=2 receipts=2 unique_findings=1 witnesses=2 external_publishes=1 native_gate=0 cost=AUSENTE")
	})

	// Every negative starts with an independent valid task/grant/receipt and
	// mutates only one boundary. No prior green failure is counted as a kill.
	mutants:=[]struct{name string; mutate func(*testing.T,*harness); wantCode int; wantError string; held uint64; sends int; publication int}{
		{"receipt-grant-mismatch",func(t *testing.T,h *harness){x:=h.extractor.byAttempt["attempt-a"];doc:=testObject(t,x.Grant);doc["seat_id"]="forged-seat";x.Grant=testJSON(t,doc);h.extractor.byAttempt["attempt-a"]=x},5,"harvest: rc 5: collection changed the original seat grant",0,1,0},
		{"nonce-swap",func(t *testing.T,h *harness){x:=h.extractor.byAttempt["attempt-a"];x.Nonce=strings.Repeat("0",64);h.extractor.byAttempt["attempt-a"]=x},6,"harvest: rc 6: unresolved work or publication; reconciliation required",1,1,0},
		{"false-host-validated",func(t *testing.T,h *harness){x:=h.extractor.byAttempt["attempt-a"];x.HostValidated=false;h.extractor.byAttempt["attempt-a"]=x},6,"harvest: rc 6: unresolved work or publication; reconciliation required",1,1,0},
		{"false-a4-validated",func(t *testing.T,h *harness){x:=h.extractor.byAttempt["attempt-a"];x.A4Validated=false;h.extractor.byAttempt["attempt-a"]=x},5,"harvest: rc 5: extraction lacks matching trusted host provenance",0,1,0},
		{"guest-only-cleanup",func(t *testing.T,h *harness){x:=h.extractor.byAttempt["attempt-a"];x.Cleanup.Known=false;x.Cleanup.Verified=false;h.extractor.byAttempt["attempt-a"]=x},6,"harvest: rc 6: unresolved work or publication; reconciliation required",1,1,0},
		{"resolution-flag-without-evidence",func(t *testing.T,h *harness){h.submitter.loseSeat=true;x:=h.extractor.byAttempt["attempt-a"];x.ResolutionVerified=true;x.ResolutionEvidence=nil;h.extractor.byAttempt["attempt-a"]=x},6,"harvest: rc 6: unresolved work or publication; reconciliation required",1,1,0},
		{"seat-loss-after-submit",func(t *testing.T,h *harness){h.submitter.loseSeat=true;h.extractor.unavailable=true},6,"harvest: rc 6: unresolved work or publication; reconciliation required",1,1,0},
	}
	killed:=0
	for _,tc:=range mutants {t.Run("must-FAIL/"+tc.name,func(t *testing.T){
		h:=newHarness(t,"a");h.generator.items=[]harvest.Candidate{h.candidate(t,"a",0,true)}
		tc.mutate(t,h);flow:=h.flow(t)
		r,e:=flow.Run(context.Background())
		if tc.name=="resolution-flag-without-evidence" {r,e=flow.Run(context.Background())}
		testRefusal(t,e,tc.wantCode,tc.wantError)
		if r.Completion.DoneSuccess||r.Harvest.CapacityHeld!=tc.held||len(h.submitter.sends)!=tc.sends||len(h.publisher.sends)!=tc.publication||len(h.aggregator.Summary().Manifestations)!=0{t.Fatalf("MUST-FAIL changed publication, hold, sends or aggregator state: %+v",r)}
		if tc.held>0 {again,second:=flow.Run(context.Background());if harvest.ExitCode(second)!=6||len(h.submitter.sends)!=1||again.Harvest.CapacityHeld!=tc.held {t.Fatalf("uncertain attempt was resubmitted or released: %v %+v",second,again.Completion)}}
		testBounds(t,r.Harvest,h.cfg);killed++
		t.Logf("KILL %s: code=%d submits=%d held=%d publication=0",tc.name,tc.wantCode,tc.sends,tc.held)
	})}
	t.Run("must-FAIL/missing-stage",func(t *testing.T){h:=newHarness(t,"a");delete(h.cfg.Harvest.QueueCap,harvest.ExtractVerify);_,e:=New(h.cfg);testRefusal(t,e,2,"harvest: rc 2: queue inventory must name every stage exactly");if len(h.submitter.sends)!=0||h.sink.collections!=0||len(h.publisher.sends)!=0{t.Fatal("configuration refusal had external side effects")};killed++;t.Log("KILL missing-stage: rc=2 sends=0")})
	t.Run("must-FAIL/changed-publication-bytes",func(t *testing.T){
		h:=newHarness(t,"a");h.generator.items=[]harvest.Candidate{h.candidate(t,"a",0,true)};r,e:=h.flow(t).Run(context.Background());if e!=nil||!r.Completion.DoneSuccess{t.Fatalf("invalid green prerequisite: %v",e)}
		before:=h.sink.Publications();if len(before)!=1 {t.Fatal("missing valid reservation")}
		pub:=before[0].Publication;pub.Capability.WriterAuthorization="example-forged-publisher-pin"
		_,e=h.sink.Reserve(context.Background(),pub)
		testRefusal(t,e,6,"harvest: rc 6: publication identity changed")
		if !reflect.DeepEqual(before,h.sink.Publications())||len(h.publisher.sends)!=1||len(h.submitter.sends)!=1{t.Fatal("changed publication bytes modified reservation or resent")}
		killed++;t.Log("KILL changed-publication-bytes: immutable reserved identity")
	})
	t.Run("failed-shard-accounted-not-success",func(t *testing.T){h:=newHarness(t,"a");h.generator.items=[]harvest.Candidate{h.candidate(t,"a",5,false)};r,e:=h.flow(t).Run(context.Background());if e==nil||r.Completion.DoneSuccess||!r.Completion.Accounted||!r.Completion.Closed||len(r.Aggregate.AttemptReceipts)!=1||len(h.submitter.sends)!=1||r.Attempts[0].NormalizedExit!=5||r.Attempts[0].NativeGateRawExit==nil||*r.Attempts[0].NativeGateRawExit!=1{t.Fatalf("failed shard was promoted or forgotten: %v %+v",e,r)};t.Log("GREEN failed shard: accounted=true done_success=false raw_gate=1 normalized=5")})
	t.Run("forced-collision-exact-bytes-buckets",func(t *testing.T){
		h:=newHarness(t,"a","b");constant:=strings.Repeat("f",64);collision:=func([]byte)string{return constant}
		h.cfg.HashFn=collision;var e error;h.aggregator,e=aggregator.NewAggregator(aggregator.Config{WaveID:h.wave.WaveID,BaseSeq:0,HashFn:collision,AllowedBindings:[]aggregator.Binding{{TaskID:"task-a",AttemptID:"attempt-a",LeaseID:"lease-a"},{TaskID:"task-b",AttemptID:"attempt-b",LeaseID:"lease-b"}}});if e!=nil{t.Fatal(e)}
		h.cfg.Aggregator=h.aggregator
		ca:=h.candidate(t,"a",0,true);cb:=h.candidate(t,"b",0,true)
		x:=h.extractor.byAttempt[cb.Attempt.AttemptID]
		repro:=testObject(t,x.Repros[0].Record);repro["assertion"]="collision but unequal bytes"
		x.Repros[0].Record=testJSON(t,repro);h.extractor.byAttempt[cb.Attempt.AttemptID]=x
		h.generator.items=[]harvest.Candidate{ca,cb}
		r,e:=h.flow(t).Run(context.Background());if e!=nil||!r.Completion.DoneSuccess||r.Aggregate.UniqueManifestationCount!=2||len(h.aggregator.Store().Bucket(constant))!=2||len(h.sink.Findings())!=2||len(h.publisher.sends)!=2||len(r.Aggregate.AttemptReceipts)!=2{t.Fatalf("digest collision aliased independent canonical bytes: %v %+v",e,r.Aggregate)}
		t.Log("GREEN collision: same digest, 2 exact-byte buckets, 2 receipts, 2 publications")
	})
	t.Run("barrier-concurrent-run-single-send",func(t *testing.T){
		h:=newHarness(t,"a");h.generator.items=[]harvest.Candidate{h.candidate(t,"a",0,true)};f:=h.flow(t)
		const workers=4;start:=make(chan struct{});var ready sync.WaitGroup;ready.Add(workers);var finish sync.WaitGroup;finish.Add(workers)
		results:=make(chan error,workers)
		for i:=0;i<workers;i++{go func(){defer finish.Done();ready.Done();<-start;r,e:=f.Run(context.Background());if e==nil&&!r.Completion.DoneSuccess{e=fmt.Errorf("concurrent result incomplete")};results<-e}()}
		ready.Wait();close(start);finish.Wait();close(results)
		for e:=range results {if e!=nil{t.Fatalf("concurrent Run: %v",e)}}
		if len(h.submitter.sends)!=1||len(h.publisher.sends)!=1||len(h.aggregator.Summary().AttemptReceipts)!=1{t.Fatal("barrier exposed a duplicate irreversible send")}
		t.Log("GREEN concurrency: workers=4 exact_submits=1 publisher_sends=1 receipt=1")
	})
	t.Logf("MUST-FAIL fixture kills=%d/%d survivors=0 equivalents=0 invalid_controls=0; limits=TARGET; runtime/host=AUSENTE",killed,len(mutants)+2)
	if killed!=len(mutants)+2{t.Fatalf("MUST-FAIL coverage incomplete %d/%d",killed,len(mutants)+2)}
}
