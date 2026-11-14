package harvest

import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"

	"opensuperficies.local/srf/src/workerbridge"
)

// C3 source: contracts/worker/tests/test_w8d_c3_switchboard.py:1015-1023,
// ref bridge/os-w8d-c3-switchboard-protocol/a1, observed commit
// 3f1e1319f3bc977968a32f1cf4223583ca9a3a06. The literal canonical vector is
// 761 bytes CITED, without a newline or length prefix. All other counts and
// timings in these synthetic tests are TARGET, not host measurements.
//go:embed testdata/repro-golden.json
var c3Golden []byte

const c3GoldenDigest = "4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934"

func sinkFixtureObservation() Observation {
	return Observation{
		Binding:ReproBinding{Repo:"example/repo",Commit:strings.Repeat("a",40),InputSHA256:strings.Repeat("1",64),GateArgv:[]string{"go","test","./..."},FrameOrder:[]string{"frame-a"}},
		Repro:CanonicalRepro{Normalizer:ReproNormalizer,Key:c3GoldenDigest,Canonical:sinkCloneBytes(c3Golden)},
		Witness:Witness{TaskID:"task-a",AttemptID:"attempt-a",LeaseID:"lease-a",Worker:"worker-a",Outcome:"failure",ExitCode:5,ReceiptSHA256:strings.Repeat("b",64),Evidence:[]byte("assertion evidence")},
	}
}

func sinkFixtureCapability() Capability {
	return Capability{ConsumerVerifier:"fixture:auto_ingest.check",ConsumerVerifierSHA256:strings.Repeat("e",64),Surface:CanonicalSurface,WriterAuthorization:"fixture:collector-writer"}
}

func sinkFixturePublication(f Finding) Publication {
	return Publication{ID:"publication-a",Surface:CanonicalSurface,Provenance:HostMuse,Finding:f,Capability:sinkFixtureCapability()}
}

func sinkFixtureReceipt() LocalReceipt {
	return LocalReceipt{
		Attempt:Attempt{Key:"attempt-key",TaskID:"task-a",AttemptID:"attempt-a",LeaseID:"lease-a",Nonce:strings.Repeat("d",64),WorkerTask:[]byte(`{"fixture":"bound"}`),InputSHA256:strings.Repeat("1",64),FrameOrder:[]string{"frame-a"}},
		Receipt:[]byte(`{"fixture":"failed"}`),Usage:MissingCost(),
		Cleanup:CleanupProof{AttemptKey:"attempt-key",LeaseID:"lease-a",Nonce:strings.Repeat("d",64),Known:true,Verified:true},
	}
}

func sinkChangeCanonical(t *testing.T, o Observation, change func(workerbridge.Document)) Observation {
	t.Helper()
	record,err := workerbridge.Parse(o.Repro.Canonical); if err != nil { t.Fatal(err) }
	change(record)
	o.Repro.Canonical,err = workerbridge.Canonical(record); if err != nil { t.Fatal(err) }
	return o
}

func TestC3GoldenIdentityPortBytes(t *testing.T) {
	if len(c3Golden) != 761 { t.Fatalf("C3 vector size: got %d",len(c3Golden)) }
	if got := fmt.Sprintf("%x",sha256.Sum256(c3Golden)); got != c3GoldenDigest { t.Fatalf("C3 vector digest: %s",got) }
	o := sinkFixtureObservation()
	if err := validateIdentity(ReproInput{Binding:o.Binding,Record:sinkCloneBytes(c3Golden)},o.Repro); err != nil { t.Fatal(err) }
	parsed,err := workerbridge.Parse(c3Golden); if err != nil { t.Fatal(err) }
	canonical,err := workerbridge.Canonical(parsed); if err != nil { t.Fatal(err) }
	if !bytes.Equal(canonical,c3Golden) { t.Fatal("C3 canonical byte parity failed") }
	if bytes.HasSuffix(c3Golden,[]byte("\n")) || c3Golden[0] != '{' { t.Fatal("newline or prefix added") }
}

func TestIdentityRefusesMissingInputAndCanonicalDrift(t *testing.T) {
	cases := []struct{name string; code int; change func(*Observation)}{
		{"missing-input",2,func(o *Observation){o.Binding.InputSHA256=""}},
		{"malformed-input",2,func(o *Observation){o.Binding.InputSHA256=strings.Repeat("X",64)}},
		{"normalizer",5,func(o *Observation){o.Repro.Normalizer="w8-repro-v1"}},
		{"key",5,func(o *Observation){o.Repro.Key="short"}},
		{"missing-canonical",5,func(o *Observation){o.Repro.Canonical=nil}},
		{"whitespace",5,func(o *Observation){o.Repro.Canonical=append(o.Repro.Canonical,'\n')}},
		{"legacy-prefix",5,func(o *Observation){o.Repro.Canonical=append(make([]byte,8),o.Repro.Canonical...)}},
		{"commit-binding",5,func(o *Observation){o.Binding.Commit=strings.Repeat("d",40)}},
		{"repo-binding",5,func(o *Observation){o.Binding.Repo="different/repo"}},
		{"argv-binding",5,func(o *Observation){o.Binding.GateArgv=[]string{"test","go","./..."}}},
		{"invalid-unicode",5,func(o *Observation){o.Repro.Canonical=[]byte{'{','"',0xff,'"',':','0','}'}}},
		{"duplicate-key",5,func(o *Observation){o.Repro.Canonical=[]byte(`{"normalizer":"w8-repro/1","normalizer":"w8-repro/1"}`)}},
		{"float",5,func(o *Observation){o.Repro.Canonical=[]byte(`{"number":0.5}`)}},
	}
	for _, tc := range cases { t.Run(tc.name,func(t *testing.T){
		s := NewMemorySink(); o := sinkFixtureObservation(); tc.change(&o)
		if _,err := s.Merge(context.Background(),o); ExitCode(err) != tc.code { t.Fatalf("got %v",err) }
		if len(s.Findings()) != 0 { t.Fatal("rejected input was indexed") }
	}) }
}

func TestExactWitnessUnionPreservesContradictionsAndProvenance(t *testing.T) {
	s := NewMemorySink(); a := sinkFixtureObservation()
	first,err := s.Merge(context.Background(),a); if err != nil { t.Fatal(err) }
	if first.Duplicate { t.Fatal("first record marked duplicate") }
	second,err := s.Merge(context.Background(),a); if err != nil { t.Fatal(err) }
	if !second.Duplicate || len(second.Witnesses) != 1 { t.Fatal("redelivery inflated witness set") }
	b := sinkFixtureObservation(); b.Witness.Worker="worker-b"; b.Witness.AttemptID="attempt-b"; b.Witness.LeaseID="lease-b"
	third,err := s.Merge(context.Background(),b); if err != nil { t.Fatal(err) }
	if !third.Duplicate || third.ID != first.ID || len(third.Witnesses) != 2 { t.Fatal("provenance split or lost") }
	late := sinkFixtureObservation(); late.Witness.Outcome="done"; late.Witness.ExitCode=0; late.Witness.Evidence=[]byte("later status")
	fourth,err := s.Merge(context.Background(),late); if err != nil { t.Fatal(err) }
	if len(fourth.Witnesses) != 3 { t.Fatal("contradictory later status erased evidence") }
	foundFailure := false
	for _, witness := range fourth.Witnesses { if witness.Outcome=="failure" && witness.ExitCode==5 { foundFailure=true } }
	if !foundFailure || len(s.Findings()) != 1 { t.Fatal("failed witness or exact bucket lost") }
}

func TestWitnessUnionIndependentOfDeliveryOrder(t *testing.T) {
	a := sinkFixtureObservation(); b := sinkFixtureObservation(); c := sinkFixtureObservation()
	b.Witness.Worker="worker-b"; b.Witness.AttemptID="attempt-b"
	c.Witness.Outcome="done"; c.Witness.ExitCode=0
	left,right := NewMemorySink(),NewMemorySink()
	for _, o := range []Observation{a,b,c,a,b} { if _,err:=left.Merge(context.Background(),o);err!=nil{t.Fatal(err)} }
	for _, o := range []Observation{c,b,a,c,a} { if _,err:=right.Merge(context.Background(),o);err!=nil{t.Fatal(err)} }
	if !reflect.DeepEqual(left.Findings(),right.Findings()) { t.Fatal("arrival order changed witness union") }
}

func TestForcedIdentityCollisionUsesSeparateExactByteBuckets(t *testing.T) {
	s := NewMemorySink(); original:=sinkFixtureObservation()
	a,err:=s.Merge(context.Background(),original);if err!=nil{t.Fatal(err)}
	other:=sinkChangeCanonical(t,sinkFixtureObservation(),func(d workerbridge.Document){d["assertion"]="different assertion 7"})
	// The fake external Identity deliberately returns the same key for different
	// canonical bytes; harvest must preserve both, without defining another hash.
	b,err:=s.Merge(context.Background(),other);if err!=nil{t.Fatal(err)}
	if b.Duplicate || b.ID==a.ID || len(s.Findings())!=2 {t.Fatal("collision merged distinct exact bytes")}
	if again,err:=s.Merge(context.Background(),other);err!=nil||!again.Duplicate||again.ID!=b.ID{t.Fatalf("collision bucket unstable: %v",err)}
}

func TestChangedFrozenInputsNeverMerge(t *testing.T) {
	cases:=[]struct{name string; change func(*testing.T,Observation) Observation}{
		{"commit",func(t *testing.T,o Observation)Observation{o.Binding.Commit=strings.Repeat("d",40);return sinkChangeCanonical(t,o,func(d workerbridge.Document){d["commit"]=o.Binding.Commit})}},
		{"input",func(t *testing.T,o Observation)Observation{o.Binding.InputSHA256=strings.Repeat("d",64);return o}},
		{"argv-order",func(t *testing.T,o Observation)Observation{o.Binding.GateArgv=[]string{"test","go","./..."};return sinkChangeCanonical(t,o,func(d workerbridge.Document){d["gate_argv"]=o.Binding.GateArgv})}},
		{"frame-binding",func(t *testing.T,o Observation)Observation{o.Binding.FrameOrder=[]string{"frame-b","frame-a"};return o}},
		{"frame-record",func(t *testing.T,o Observation)Observation{return sinkChangeCanonical(t,o,func(d workerbridge.Document){d["frames"]=[]any{map[string]any{"line":8,"path":"src/b.go","symbol":"TestB"},map[string]any{"line":7,"path":"src/a.go","symbol":"TestA"}}})}},
	}
	for _,tc:=range cases{t.Run(tc.name,func(t *testing.T){
		s:=NewMemorySink();a,err:=s.Merge(context.Background(),sinkFixtureObservation());if err!=nil{t.Fatal(err)}
		b,err:=s.Merge(context.Background(),tc.change(t,sinkFixtureObservation()));if err!=nil{t.Fatal(err)}
		if b.Duplicate||a.ID==b.ID||len(s.Findings())!=2{t.Fatal("changed frozen data merged")}
	})}
}

func TestOrderedCanonicalFramesStayDistinct(t *testing.T) {
	frames:=[]any{map[string]any{"line":7,"path":"src/a.go","symbol":"TestA"},map[string]any{"line":8,"path":"src/b.go","symbol":"TestB"}}
	a:=sinkChangeCanonical(t,sinkFixtureObservation(),func(d workerbridge.Document){d["frames"]=frames})
	b:=sinkChangeCanonical(t,sinkFixtureObservation(),func(d workerbridge.Document){d["frames"]=[]any{frames[1],frames[0]}})
	s:=NewMemorySink()
	if _,err:=s.Merge(context.Background(),a);err!=nil{t.Fatal(err)}
	if f,err:=s.Merge(context.Background(),b);err!=nil||f.Duplicate{t.Fatalf("reordered frames merged: %v",err)}
}

func TestMemorySinkDefensiveCopies(t *testing.T) {
	s:=NewMemorySink();input:=sinkFixtureObservation()
	f,err:=s.Merge(context.Background(),input);if err!=nil{t.Fatal(err)}
	before:=s.Findings()
	input.Repro.Canonical[0]='x';input.Binding.GateArgv[0]="changed";input.Witness.Evidence[0]='x'
	f.Repro.Canonical[0]='y';f.Binding.FrameOrder[0]="changed";f.Witnesses[0].Evidence[0]='y'
	view:=s.Findings();view[0].Witnesses[0].Evidence[0]='z'
	if !reflect.DeepEqual(before,s.Findings()){t.Fatal("caller mutated stored finding")}
	withEmpty:=sinkFixtureObservation();withEmpty.Binding.FrameOrder=[]string{}
	f,err=s.Merge(context.Background(),withEmpty);if err!=nil{t.Fatal(err)}
	p:=sinkFixturePublication(f);p.ID="publication-empty-frames"
	if _,err=s.Reserve(context.Background(),p);err!=nil{t.Fatalf("empty slice shape changed during copy: %v",err)}
}

func TestReceiptUnionKeepsFailuresCleanupAndUnknownCost(t *testing.T) {
	s:=NewMemorySink();r:=sinkFixtureReceipt();r.Cleanup.Known=false;r.Cleanup.Verified=false
	if err:=s.Collect(context.Background(),r);err!=nil{t.Fatal(err)}
	if err:=s.Collect(context.Background(),r);err!=nil{t.Fatal(err)}
	rows:=s.Receipts(r.Attempt.Key)
	if len(rows)!=1||rows[0].Usage.Microunits!=nil||rows[0].Usage.Reason!="AUSENTE"||rows[0].Cleanup.Verified{t.Fatal("receipt redelivery, missing cost or cleanup was coerced")}
	later:=sinkFixtureReceipt();later.Receipt=[]byte(`{"fixture":"later-done"}`)
	if err:=s.Collect(context.Background(),later);err!=nil{t.Fatal(err)}
	if len(s.Receipts(r.Attempt.Key))!=2{t.Fatal("late receipt overwrote failure evidence")}
	before:=s.Receipts(r.Attempt.Key)
	r.Receipt[0]='x';r.Attempt.WorkerTask[0]='x';rows[0].Receipt[0]='x'
	if !reflect.DeepEqual(before,s.Receipts("attempt-key")){t.Fatal("receipt observation was not copied")}
}

func TestReceiptCostsAndAttemptBindingsAreNotFindingDedup(t *testing.T) {
	s:=NewMemorySink();a:=sinkFixtureReceipt();value:=uint64(7);a.Usage=Cost{Microunits:&value,Unit:"fixture-credit"}
	if err:=s.Collect(context.Background(),a);err!=nil{t.Fatal(err)}
	value=99
	if got:=*s.Receipts(a.Attempt.Key)[0].Usage.Microunits;got!=7{t.Fatalf("usage pointer retained: %d",got)}
	b:=sinkFixtureReceipt();b.Attempt.Key="retry-key";b.Attempt.AttemptID="attempt-b";b.Attempt.ParentAttemptID="attempt-a";b.Attempt.LeaseID="lease-b"
	if err:=s.Collect(context.Background(),b);err!=nil{t.Fatal(err)}
	if len(s.Receipts("attempt-key"))!=1||len(s.Receipts("retry-key"))!=1{t.Fatal("retry receipt disappeared")}
	forged:=sinkFixtureReceipt();forged.Attempt.LeaseID="changed-lease"
	if err:=s.Collect(context.Background(),forged);ExitCode(err)!=6{t.Fatalf("attempt equivocation accepted: %v",err)}
	if len(s.Receipts("attempt-key"))!=1{t.Fatal("binding conflict changed receipt store")}
}

func TestPublicationCapabilityHoldsUntilAllPrerequisites(t *testing.T) {
	if err:=validateCapability(nil);ExitCode(err)!=4{t.Fatal(err)}
	cases:=[]struct{name string;code int;change func(*Capability)}{
		{"verifier-name",4,func(c *Capability){c.ConsumerVerifier=""}},
		{"verifier-pin",4,func(c *Capability){c.ConsumerVerifierSHA256=""}},
		{"writer",4,func(c *Capability){c.WriterAuthorization=""}},
		{"missing-surface",4,func(c *Capability){c.Surface=""}},
		{"relabeled-surface",5,func(c *Capability){c.Surface="jules"}},
	}
	for _,tc:=range cases{t.Run(tc.name,func(t *testing.T){c:=sinkFixtureCapability();tc.change(&c);if err:=validateCapability(&c);ExitCode(err)!=tc.code{t.Fatal(err)}})}
	if err:=validateCapability(func()*Capability{c:=sinkFixtureCapability();return &c}());err!=nil{t.Fatal(err)}
}

func TestPublicationReservationCrashIsQueryOnly(t *testing.T) {
	s:=NewMemorySink();f,err:=s.Merge(context.Background(),sinkFixtureObservation());if err!=nil{t.Fatal(err)}
	p:=sinkFixturePublication(f)
	first,err:=s.Reserve(context.Background(),p);if err!=nil{t.Fatal(err)}
	if !first.Fresh||first.Status!=PublicationPending{t.Fatal("first publication was not reserved")}
	// Simulate the consumer write succeeding while the acknowledgement is lost.
	// Sharing the same sink after reconstruction preserves the query-only intent.
	externalWrites:=1
	second,err:=s.Reserve(context.Background(),p);if err!=nil{t.Fatal(err)}
	if second.Fresh{externalWrites++}
	if externalWrites!=1||second.Status!=PublicationPending{t.Fatal("uncertain publication was reissued")}
	extra:=sinkFixtureObservation();extra.Witness.Worker="worker-b"
	p.Finding,err=s.Merge(context.Background(),extra);if err!=nil{t.Fatal(err)}
	third,err:=s.Reserve(context.Background(),p);if err!=nil{t.Fatal(err)}
	if third.Fresh||len(third.Publication.Finding.Witnesses)!=1{t.Fatal("reserved publication bytes mutated")}
	ack:=PublicationAck{ID:p.ID,Accepted:true,Surface:CanonicalSurface,ConsumerVerifierSHA256:p.Capability.ConsumerVerifierSHA256}
	if err=s.Confirm(context.Background(),ack);err!=nil{t.Fatal(err)}
	if err=s.Confirm(context.Background(),ack);err!=nil{t.Fatal(err)}
	last,err:=s.Reserve(context.Background(),p);if err!=nil{t.Fatal(err)}
	if last.Fresh||last.Status!=PublicationAccepted||last.Ack!=ack{t.Fatal("confirmed publication was not idempotent")}
	first.Publication.Finding.Repro.Canonical[0]='x'
	view:=s.Publications();view[0].Publication.Finding.Witnesses[0].Evidence[0]='x'
	if s.Publications()[0].Publication.Finding.Repro.Canonical[0]!='{'{t.Fatal("reservation alias escaped")}
}

func TestPublicationSnapshotMayPrecedeLaterWitnessUnion(t *testing.T) {
	s:=NewMemorySink();first,err:=s.Merge(context.Background(),sinkFixtureObservation());if err!=nil{t.Fatal(err)}
	later:=sinkFixtureObservation();later.Witness.Worker="worker-b"
	if _,err=s.Merge(context.Background(),later);err!=nil{t.Fatal(err)}
	// The bounded collection queue still owns the earlier immutable snapshot.
	reserved,err:=s.Reserve(context.Background(),sinkFixturePublication(first));if err!=nil{t.Fatal(err)}
	if !reserved.Fresh||len(reserved.Publication.Finding.Witnesses)!=1||len(s.Findings()[0].Witnesses)!=2{t.Fatal("valid queued snapshot lost its reservation")}
}

func TestPublicationRefusesForgedIdentityAndAcknowledgements(t *testing.T) {
	s:=NewMemorySink();f,err:=s.Merge(context.Background(),sinkFixtureObservation());if err!=nil{t.Fatal(err)}
	p:=sinkFixturePublication(f)
	bad:=p;bad.Provenance="jules"
	if _,err=s.Reserve(context.Background(),bad);ExitCode(err)!=5{t.Fatal(err)}
	bad=sinkClonePublication(p);bad.Finding.Witnesses[0].Evidence=[]byte("uncollected")
	if _,err=s.Reserve(context.Background(),bad);ExitCode(err)!=5{t.Fatal(err)}
	if _,err=s.Reserve(context.Background(),p);err!=nil{t.Fatal(err)}
	bad=p;bad.ID="another-publication"
	if _,err=s.Reserve(context.Background(),bad);ExitCode(err)!=6{t.Fatal(err)}
	bad=p;bad.Capability.WriterAuthorization="changed-writer"
	if _,err=s.Reserve(context.Background(),bad);ExitCode(err)!=6{t.Fatal(err)}
	good:=PublicationAck{ID:p.ID,Accepted:true,Surface:CanonicalSurface,ConsumerVerifierSHA256:p.Capability.ConsumerVerifierSHA256}
	acks:=[]PublicationAck{good,good,good,good}
	acks[0].ID="unknown";acks[1].Accepted=false;acks[2].Surface="jules";acks[3].ConsumerVerifierSHA256=strings.Repeat("f",64)
	for _,ack:=range acks{if err=s.Confirm(context.Background(),ack);ExitCode(err)!=5{t.Fatalf("forged ack accepted: %v",err)}}
	if s.Publications()[0].Status!=PublicationPending{t.Fatal("invalid acknowledgement released reservation")}
}

func TestConcurrentWitnessUnionAndSinglePublicationReservation(t *testing.T) {
	s:=NewMemorySink();var wg sync.WaitGroup
	errors:=make(chan error,16)
	for i:=0;i<16;i++{wg.Add(1);go func(i int){defer wg.Done();o:=sinkFixtureObservation();o.Witness.Worker=fmt.Sprintf("worker-%02d",i);_,err:=s.Merge(context.Background(),o);errors<-err}(i)}
	wg.Wait();close(errors);for err:=range errors{if err!=nil{t.Fatal(err)}}
	findings:=s.Findings();if len(findings)!=1||len(findings[0].Witnesses)!=16{t.Fatal("concurrent witness union lost entries")}
	p:=sinkFixturePublication(findings[0]);fresh:=make(chan bool,16);errs:=make(chan error,16)
	for i:=0;i<16;i++{wg.Add(1);go func(){defer wg.Done();r,err:=s.Reserve(context.Background(),p);fresh<-r.Fresh;errs<-err}()}
	wg.Wait();close(fresh);close(errs)
	for err:=range errs{if err!=nil{t.Fatal(err)}}
	count:=0;for got:=range fresh{if got{count++}}
	if count!=1{t.Fatalf("effectful publication had %d owners",count)}
}

func TestCanceledSinkOperationsLeaveNoState(t *testing.T) {
	ctx,cancel:=context.WithCancel(context.Background());cancel()
	s:=NewMemorySink()
	if _,err:=s.Merge(ctx,sinkFixtureObservation());ExitCode(err)!=7{t.Fatal(err)}
	if err:=s.Collect(ctx,sinkFixtureReceipt());ExitCode(err)!=7{t.Fatal(err)}
	if len(s.Findings())!=0||len(s.Receipts("attempt-key"))!=0||len(s.Publications())!=0{t.Fatal("canceled operation changed store")}
}
