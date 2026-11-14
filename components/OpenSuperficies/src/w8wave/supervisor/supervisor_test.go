package supervisor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

type fakeSeat struct {
	mu sync.Mutex
	submits, waits, collects int
	onSubmit func(Attempt, int) (SubmitReply, error)
	onWait func(Ticket, int) (WaitReply, error)
	onCollect func(Ticket, int) (CollectReply, error)
}
func (f *fakeSeat) Submit(_ context.Context, a Attempt) (SubmitReply, error) {
	f.mu.Lock(); f.submits++; n := f.submits; f.mu.Unlock()
	return f.onSubmit(a, n)
}
func (f *fakeSeat) Wait(_ context.Context, ticket Ticket) (WaitReply, error) {
	f.mu.Lock(); f.waits++; n := f.waits; f.mu.Unlock()
	return f.onWait(ticket, n)
}
func (f *fakeSeat) Collect(_ context.Context, ticket Ticket) (CollectReply, error) {
	f.mu.Lock(); f.collects++; n := f.collects; f.mu.Unlock()
	return f.onCollect(ticket, n)
}
func baselineWork() Work {
	return Work{Identity: Identity{
		TaskDigest: strings.Repeat("a", 64), Commit: strings.Repeat("b", 40),
		GateArgvDigest: strings.Repeat("c", 64), EnvironmentManifestDigest: strings.Repeat("d", 64),
	}, LeaseID: "lease-A"}
}
func anotherWork() Work {
	w := baselineWork()
	w.Identity.TaskDigest = strings.Repeat("f", 64)
	return w
}
func openBook(t *testing.T) *wal.LibroConsumo {
	t.Helper()
	book, err := wal.AbrirLibro(filepath.Join(t.TempDir(), "attempts.wal"))
	if err != nil { t.Fatal(err) }
	t.Cleanup(func() { _ = book.Cerrar() })
	return book
}
func newForTest(t *testing.T, book *wal.LibroConsumo, seat Submitter, restarts int) *Supervisor {
	t.Helper()
	s, err := New(book, seat, Config{MaxRestarts: restarts, RestartPeriod: time.Hour})
	if err != nil { t.Fatal(err) }
	return s
}
func accepted(a Attempt) SubmitReply {
	return SubmitReply{AttemptID: a.AttemptID, Ticket: Ticket{AttemptID: a.AttemptID, Handle: "seat-handle"}}
}
func successWait(ticket Ticket) WaitReply {
	return WaitReply{AttemptID: ticket.AttemptID, Code: 0, ExecutionStarted: boolp(true), CleanupVerified: true}
}
func successCollect(ticket Ticket) CollectReply {
	return CollectReply{AttemptID: ticket.AttemptID, CleanupVerified: true,
		Evidence: &wal.EvidenciaExitoW8{AttemptID: ticket.AttemptID,
			GatesOK: true, ExtractOK: true, PurgeOK: true, ProbesOK: true}}
}
func normalSeat() *fakeSeat {
	return &fakeSeat{
		onSubmit: func(a Attempt, _ int) (SubmitReply, error) { return accepted(a), nil },
		onWait: func(ticket Ticket, _ int) (WaitReply, error) { return successWait(ticket), nil },
		onCollect: func(ticket Ticket, _ int) (CollectReply, error) { return successCollect(ticket), nil },
	}
}
func readAll(t *testing.T, book *wal.LibroConsumo) []wal.Registro {
	t.Helper()
	recs, err := wal.LeerRegistros(book.Path())
	if err != nil { t.Fatal(err) }
	return recs
}
func countKind(recs []wal.Registro, kind wal.TipoRegistro) int {
	n := 0
	for _, r := range recs { if r.Tipo == kind { n++ } }
	return n
}

func TestAdapterFullRCTable(t *testing.T) {
	ev := &wal.EvidenciaExitoW8{AttemptID: "stable-id", GatesOK: true, ExtractOK: true, PurgeOK: true, ProbesOK: true}
	cases := []struct {
		name string; rc int; started *bool; evidence *wal.EvidenciaExitoW8; want wal.SrfDesenlace
	}{
		{"success_proven", 0, boolp(true), ev, wal.DesenlaceConfirmado},
		{"success_not_started", 0, boolp(false), ev, wal.DesenlaceEnDuda},
		{"success_no_evidence", 0, boolp(true), nil, wal.DesenlaceEnDuda},
		{"invalid_before", 2, boolp(false), nil, wal.DesenlaceNoEnviado},
		{"invalid_after", 2, boolp(true), nil, wal.DesenlaceEnDuda},
		{"execution_before", 3, boolp(false), nil, wal.DesenlaceNoEnviado},
		{"execution_after", 3, boolp(true), nil, wal.DesenlaceEnviadoParcial},
		{"prerequisite_before", 4, boolp(false), nil, wal.DesenlaceNoEnviado},
		{"prerequisite_after", 4, boolp(true), nil, wal.DesenlaceEnDuda},
		{"gate_before", 5, boolp(false), nil, wal.DesenlaceNoEnviado},
		{"gate_after", 5, boolp(true), nil, wal.DesenlaceEnviadoParcial},
		{"uncertain_before", 6, boolp(false), nil, wal.DesenlaceEnDuda},
		{"uncertain_after", 6, boolp(true), nil, wal.DesenlaceEnDuda},
		{"operator_before", 7, boolp(false), nil, wal.DesenlaceNoEnviado},
		{"operator_after", 7, boolp(true), nil, wal.DesenlaceEnDuda},
		{"motor_rc_forbidden", 1, boolp(false), nil, wal.DesenlaceEnDuda},
		{"negative", -1, boolp(true), nil, wal.DesenlaceEnDuda},
		{"above_range", 8, boolp(true), nil, wal.DesenlaceEnDuda},
		{"nil_boundary", 0, nil, ev, wal.DesenlaceEnDuda},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := wal.ResolverDesenlaceW8(tc.rc, tc.started, "stable-id", tc.evidence); got != tc.want {
				t.Fatalf("W8 outcome (%d) = %s, want %s", tc.rc, got, tc.want)
			}
		})
	}
	mutants := []wal.EvidenciaExitoW8{
		{AttemptID: "recycled", GatesOK: true, ExtractOK: true, PurgeOK: true, ProbesOK: true},
		{AttemptID: "stable-id", ExtractOK: true, PurgeOK: true, ProbesOK: true},
		{AttemptID: "stable-id", GatesOK: true, PurgeOK: true, ProbesOK: true},
		{AttemptID: "stable-id", GatesOK: true, ExtractOK: true, ProbesOK: true},
		{AttemptID: "stable-id", GatesOK: true, ExtractOK: true, PurgeOK: true},
	}
	for i := range mutants {
		if wal.ResolverDesenlaceW8(0, boolp(true), "stable-id", &mutants[i]) != wal.DesenlaceEnDuda {
			t.Fatalf("mutated success evidence confirmed: %d", i)
		}
	}
}

func TestWaveHoldsWhenSeatDown(t *testing.T) {
	book := openBook(t)
	seat := normalSeat()
	seat.onSubmit = func(_ Attempt, _ int) (SubmitReply, error) { return SubmitReply{}, ErrSeatDown }
	s := newForTest(t, book, seat, 2)
	w := baselineWork()
	r, err := s.Run(context.Background(), w)
	if err != nil || r.Code != 6 || r.Outcome != wal.DesenlaceEnDuda { t.Fatalf("seat down: %+v %v", r, err) }
	w.LeaseID = "new-lease"
	r, err = newForTest(t, book, seat, 2).Run(context.Background(), w)
	if err != nil || r.Code != 6 || r.Outcome != wal.DesenlaceEnDuda { t.Fatalf("replay: %+v %v", r, err) }
	if seat.submits != 1 || seat.waits != 0 || seat.collects != 0 { t.Fatalf("seat down advanced pipeline or retried: %+v", seat) }
	recs := readAll(t, book)
	if countKind(recs, wal.TipoIntencion) != 1 || countKind(recs, wal.TipoDesenlace) != 1 { t.Fatal("unexpected WAL attempts") }
}

func TestCleanInfrastructureRetryThenSuccessAndReplay(t *testing.T) {
	book := openBook(t)
	seat := normalSeat()
	seat.onSubmit = func(a Attempt, n int) (SubmitReply, error) {
		if n == 1 { return SubmitReply{AttemptID:a.AttemptID, Code:4, ExecutionStarted:boolp(false), Fault:FaultInfrastructure, CleanupVerified:true}, nil }
		return accepted(a), nil
	}
	w := baselineWork()
	r, err := newForTest(t, book, seat, 2).Run(context.Background(), w)
	if err != nil || r.Outcome != wal.DesenlaceConfirmado || r.Attempts != 2 || seat.submits != 2 { t.Fatalf("clean retry: %+v %v", r, err) }
	w.LeaseID = "renewed"
	again, err := newForTest(t, book, seat, 2).Run(context.Background(), w)
	if err != nil || again.AttemptID != r.AttemptID || seat.submits != 2 { t.Fatalf("idempotence: %+v %v", again, err) }
	recs := readAll(t, book)
	if countKind(recs, wal.TipoIntencion) != 2 || countKind(recs, wal.TipoDesenlace) != 2 { t.Fatal("unexpected WAL records") }
	if verification := wal.VerificarLibro(recs, true, true); verification.Violaciones != 0 { t.Fatalf("WAL invariants: %+v", verification) }
}

func TestRetryPoisonClassesNeverRepeat(t *testing.T) {
	for _, tc := range []struct{name string; code int; fault FaultClass}{
		{"deterministic_gate",5,FaultGate}, {"invalid_input",2,FaultInput},
		{"containment",5,FaultContainment}, {"operator",7,FaultOperator},
		{"uncertain",6,FaultInfrastructure}, {"unclassified_prerequisite",4,FaultUnknown},
	} {
		t.Run(tc.name, func(t *testing.T) {
			seat := normalSeat()
			seat.onSubmit = func(a Attempt, _ int) (SubmitReply,error) {
				return SubmitReply{AttemptID:a.AttemptID, Code:tc.code, ExecutionStarted:boolp(false), Fault:tc.fault, CleanupVerified:true},nil
			}
			r, err := newForTest(t,openBook(t),seat,2).Run(context.Background(),baselineWork())
			if err != nil || r.Outcome == wal.DesenlaceConfirmado || seat.submits != 1 { t.Fatalf("poison retry: %+v %v",r,err) }
		})
	}
}

func TestAttemptCounterPersistsAcrossSupervisorAndLease(t *testing.T) {
	book := openBook(t)
	seat := normalSeat()
	seat.onSubmit = func(a Attempt,_ int) (SubmitReply,error) {
		return SubmitReply{AttemptID:a.AttemptID, Code:3, Fault:FaultInfrastructure, CleanupVerified:true, ExecutionStarted:boolp(false)},nil
	}
	w := baselineWork()
	r, err := newForTest(t,book,seat,4).Run(context.Background(),w)
	if err != nil || r.Attempts != MaxAttempts || seat.submits != MaxAttempts { t.Fatalf("attempt cap: %+v %v",r,err) }
	w.LeaseID = "brand-new-lease"
	r, err = newForTest(t,book,seat,4).Run(context.Background(),w)
	if err != nil || r.Attempts != MaxAttempts || seat.submits != MaxAttempts { t.Fatalf("counter reset: %+v %v",r,err) }
}

func TestOpenIntentionReplayAbstainsAndIsIdempotent(t *testing.T) {
	book := openBook(t)
	w := baselineWork()
	digest,_ := identityHash(w.Identity)
	chain := rootChain(digest)
	a := Attempt{Identity:w.Identity, IdentityDigest:digest, ChainID:chain,
		AttemptID:attemptKey(chain,1), Ordinal:1, LeaseID:w.LeaseID}
	seat := normalSeat()
	s := newForTest(t,book,seat,2)
	if err := s.beginAttempt(a); err != nil { t.Fatal(err) }
	w.LeaseID = "restarted"
	r, err := s.Run(context.Background(),w)
	if err != nil || r.Outcome != wal.DesenlaceEnDuda || seat.submits != 0 { t.Fatalf("replayed dispatch: %+v %v",r,err) }
	first := len(readAll(t,book))
	_,err = newForTest(t,book,seat,2).Run(context.Background(),w)
	if err != nil || len(readAll(t,book)) != first || seat.submits != 0 { t.Fatalf("replay not idempotent: %v",err) }
}

func TestOperatorRequeueIsAuditableAndIdempotent(t *testing.T) {
	book := openBook(t)
	seat := normalSeat()
	s := newForTest(t,book,seat,2)
	w := baselineWork()
	first,err := s.Run(context.Background(),w)
	if err != nil || first.Outcome != wal.DesenlaceConfirmado { t.Fatalf("first: %+v %v",first,err) }
	w.LeaseID = "different-lease"
	_,err = s.Run(context.Background(),w)
	if err != nil || seat.submits != 1 { t.Fatalf("lease recycled: %v",err) }
	req := OperatorRequeue{Identity:w.Identity,EventID:"OPS-42",Operator:"oncall",Reason:"reviewed_and_approved"}
	newChain,err := s.Requeue(req)
	if err != nil || newChain == first.ChainID { t.Fatalf("no new chain: %s %v",newChain,err) }
	duplicate,err := s.Requeue(req)
	if err != nil || duplicate != newChain { t.Fatalf("duplicate not idempotent: %v",err) }
	fresh,err := s.Run(context.Background(),w)
	if err != nil || fresh.ChainID != newChain || fresh.Attempts != 1 || seat.submits != 2 { t.Fatalf("requeue: %+v %v",fresh,err) }
	req.Reason = "mutated"
	if _,err := s.Requeue(req); !errors.Is(err,ErrLedgerConflict) { t.Fatalf("event replay changed reason: %v",err) }
	if countKind(readAll(t,book),wal.TipoIntencion) != 2 { t.Fatal("historical attempt deleted") }
}

func TestRecycledSeatIdentityCannotConfirm(t *testing.T) {
	seat := normalSeat()
	seat.onCollect = func(_ Ticket,_ int) (CollectReply,error) {
		return CollectReply{AttemptID:strings.Repeat("0",64), CleanupVerified:true},nil
	}
	r,err := newForTest(t,openBook(t),seat,2).Run(context.Background(),baselineWork())
	if err != nil || r.Outcome != wal.DesenlaceEnDuda || seat.submits != 1 { t.Fatalf("recycled receipt: %+v %v",r,err) }
}

func TestRestForOneWithinAttempt(t *testing.T) {
	for _,target := range []string{"wait","collect"} {
		t.Run(target,func(t *testing.T) {
			seat := normalSeat()
			if target == "wait" {
				seat.onWait = func(ticket Ticket,n int)(WaitReply,error) {
					if n==1 { return WaitReply{}, &ReadFailure{Err:errors.New("transient read")} }
					return successWait(ticket),nil
				}
			} else {
				seat.onCollect = func(ticket Ticket,n int)(CollectReply,error) {
					if n==1 { return CollectReply{}, &ReadFailure{Err:errors.New("transient read")} }
					return successCollect(ticket),nil
				}
			}
			r,err := newForTest(t,openBook(t),seat,2).Run(context.Background(),baselineWork())
			if err != nil || r.Outcome != wal.DesenlaceConfirmado || seat.submits != 1 { t.Fatalf("resubmitted attempt: %+v %v",r,err) }
			if target=="wait" && (seat.waits!=2 || seat.collects!=1) { t.Fatalf("wait child restart: %+v",seat) }
			if target=="collect" && (seat.waits!=1 || seat.collects!=2) { t.Fatalf("collect child restart: %+v",seat) }
		})
	}
}

func TestRestartIntensityStopsParentWave(t *testing.T) {
	book:=openBook(t)
	seat:=normalSeat()
	seat.onWait=func(_ Ticket,_ int)(WaitReply,error){return WaitReply{},&ReadFailure{Err:errors.New("transport break")}}
	s:=newForTest(t,book,seat,1)
	results,err:=s.RunWave(context.Background(),[]Work{baselineWork(),anotherWork()})
	if err!=nil || len(results)!=2 || !results[0].Escalated || !results[1].Escalated ||
		results[0].Outcome!=wal.DesenlaceEnDuda || results[1].Outcome!=wal.DesenlaceEnDuda || seat.submits!=1 {
		t.Fatalf("wave not halted: %+v %v",results,err)
	}
	_,err=newForTest(t,book,seat,2).Run(context.Background(),baselineWork())
	if err!=nil || seat.submits!=1 {t.Fatalf("replay cleared halt: %v",err)}
}

func TestConcurrentSupervisorsShareWALLock(t *testing.T) {
	book:=openBook(t)
	seat:=normalSeat()
	s1:=newForTest(t,book,seat,1)
	s2:=newForTest(t,book,seat,1)
	var wg sync.WaitGroup
	for _,s:=range []*Supervisor{s1,s2} {
		wg.Add(1)
		go func(s *Supervisor) {
			defer wg.Done()
			r,err:=s.Run(context.Background(),baselineWork())
			if err!=nil || r.Outcome!=wal.DesenlaceConfirmado {t.Errorf("parallel result: %+v %v",r,err)}
		}(s)
	}
	wg.Wait()
	if seat.submits!=1 {t.Fatalf("parallel duplicate submit: %d",seat.submits)}
}

// This fixture proves that the source guard rejects forbidden launch/TTL and
// return-code aliasing primitives without embedding forbidden grep literals.
func TestForbiddenSurfaceMutationFixtures(t *testing.T) {
	forbid:=[]string{
		"Journal"+"Create", "docker"+" run", "docker"+" cp",
		"\""+"ru"+"n"+"\"", "\""+"os/"+"exec"+"\"",
		"time.NewTicker(", "time.AfterFunc(", "janitor.Start(", "capsuleTTL(",
		"Desenlace"+"DeRC(",
	}
	check:=func(s string) bool {
		for _,token:=range forbid {if strings.Contains(s,token){return false}}
		return true
	}
	for _,mutant:=range forbid {
		if check("func illegal() { "+mutant+" }"){t.Fatalf("mutant passed: %q",mutant)}
	}
	_,file,_,_:=runtime.Caller(0)
	entries,err:=os.ReadDir(filepath.Dir(file))
	if err!=nil {t.Fatal(err)}
	for _,entry:=range entries {
		if !strings.HasSuffix(entry.Name(),".go")||strings.HasSuffix(entry.Name(),"_test.go"){continue}
		src,err:=os.ReadFile(filepath.Join(filepath.Dir(file),entry.Name()))
		if err!=nil {t.Fatal(err)}
		if !check(string(src)){t.Errorf("forbidden primitive in %s",entry.Name())}
	}
}

func TestWALRecycledAttemptIdentityIsPoison(t *testing.T) {
	book:=openBook(t)
	w:=baselineWork()
	digest,_:=identityHash(w.Identity)
	chain:=rootChain(digest)
	id:=attemptKey(chain,1)
	forged:=audit{Kind:markAttempt,IdentityDigest:strings.Repeat("f",64),ChainID:chain,
		AttemptID:id,Ordinal:1,LeaseID:"stolen-lease"}
	if _,err:=book.EscribirIntencion("w8:"+id,id,forged);err!=nil{t.Fatal(err)}
	seat:=normalSeat()
	r,err:=newForTest(t,book,seat,2).Run(context.Background(),w)
	if !errors.Is(err,ErrLedgerConflict)||r.Outcome!=wal.DesenlaceEnDuda||seat.submits!=0 {
		t.Fatalf("forged identity accepted: %+v %v",r,err)
	}
}

func TestInfrastructureRequiresVerifiedCleanupAndNoExecution(t *testing.T) {
	for _,tc:=range []struct{name string;started,cleanup bool;want wal.SrfDesenlace}{
		{"cleanup_missing",false,false,wal.DesenlaceNoEnviado},
		{"already_started",true,true,wal.DesenlaceEnDuda},
	}{
		t.Run(tc.name,func(t *testing.T){
			seat:=normalSeat()
			seat.onSubmit=func(a Attempt,_ int)(SubmitReply,error){
				return SubmitReply{AttemptID:a.AttemptID,Code:4,ExecutionStarted:boolp(tc.started),
					Fault:FaultInfrastructure,CleanupVerified:tc.cleanup},nil
			}
			r,err:=newForTest(t,openBook(t),seat,4).Run(context.Background(),baselineWork())
			if err!=nil||r.Outcome!=tc.want||seat.submits!=1 {t.Fatalf("unsafe retry: %+v %v",r,err)}
		})
	}
}

func TestMissingSuccessEvidenceIsNotConfirmation(t *testing.T) {
	seat:=normalSeat()
	seat.onCollect=func(ticket Ticket,_ int)(CollectReply,error){
		ok:=successCollect(ticket)
		ok.Evidence.ProbesOK=false
		return ok,nil
	}
	r,err:=newForTest(t,openBook(t),seat,2).Run(context.Background(),baselineWork())
	if err!=nil||r.Outcome!=wal.DesenlaceEnDuda||r.Code!=0||seat.submits!=1 {
		t.Fatalf("false success or retry: %+v %v",r,err)
	}
}
