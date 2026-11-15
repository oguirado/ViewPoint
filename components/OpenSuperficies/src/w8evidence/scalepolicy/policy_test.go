package scalepolicy

import(
 "crypto/sha256"
 "encoding/hex"
 "errors"
 "strings"
 "sync"
 "testing"
)
func ptr(n int64)*int64{return &n}
func rat(n,d int64)*Ratio{return &Ratio{n,d}}
func fixture()(Policy,Snapshot,Capture,int64){
 p:=Policy{MaxWidth:4,ExpectedAncestors:2,MaxSampleAgeNS:1000,MaxTailLatencyNS:250,MinDwellNS:20,MinReactionReserveBytes:100,MinPhysicalHeadroomBytes:200,MaxPressure:Ratio{1,5},
 Ceiling:Limits{CPU:Ratio{8,1},RAMBytes:100000,PIDs:100,FDs:100},
 PerOuter:Reservation{CPU:Ratio{1,2},PIDs:2,FDs:2,GuestRAMBytes:300,VMMBytes:100,TmpfsBytes:100,HostCLIBytes:50,CollectorBytes:50,StartupBytes:100}}
 raw:="identity ok\nbounded behavior evidenced\ninventory present\n"
 digest:=sha256.Sum256([]byte(raw))
 c:=Capture{SampleID:"sample-a",WorkloadRepo:"test/repo",WorkloadCommit:"workload-commit",EngineSHA256:strings.Repeat("a",64),Seed:1,RequestedTicks:5,ActualTicks:5,AccountUID:10001,PID:20,ProcessStart:3,NamespaceID:"pid-identity",AcquiredMonotonicNS:10000,
 ClockUnit:"ns",RawCapture:raw,RawCaptureSHA256:hex.EncodeToString(digest[:]),Inventory:true,BehaviorMarker:"bounded behavior evidenced",RAMSource:"observed",CPU:rat(1,1),RAMBytes:ptr(1000),Status:"verified"}
 s:=Snapshot{SampleID:c.SampleID,WorkloadCommit:c.WorkloadCommit,AcquiredMonotonicNS:10000,CPU:rat(1,1),RAMBytes:ptr(1000),PIDs:ptr(2),FDs:ptr(2),
 AncestorLimits:[]Limits{p.Ceiling,p.Ceiling},PhysicalHeadroomBytes:ptr(10000),StartupBytes:ptr(100),CollectorBytes:ptr(100),ReactionReserveBytes:ptr(100),Pressure:rat(1,10),TailLatencyNS:ptr(100),DwellNS:ptr(25),LastQualifiedWidth:0,
 Entries:[]Entry{{EntryID:"outer-a",LeaseID:"lease-a",Generation:1,Epoch:1,State:Cold,CleanupVerified:true,Fresh:true}},InferenceSeats:88}
 return p,s,c,10100
}
func TestNextWidthAndFallback(t *testing.T){
 p,s,c,n:=fixture()
 d:=Evaluate(p,s,c,n)
 if d.State!=Propose||d.Width!=1||!d.NeedsReservationAndPermit{t.Fatalf("no bounded next width: %+v",d)}
 s.LastQualifiedWidth=2
 s.Entries=append(s.Entries,Entry{EntryID:"older",LeaseID:"lease-older",Generation:2,Epoch:2,State:Stopping,ReservedBytes:1500})
 d=Evaluate(p,s,c,n)
 if d.State!=Propose||d.Width!=3{t.Fatalf("increment failed: %+v",d)}
 s.Pressure=rat(1,1)
 d=Evaluate(p,s,c,n)
 if d.State!=Hold||d.Width!=2{t.Fatalf("guard failure lost last qualified width: %+v",d)}
}
func TestMustFail(t *testing.T){
 cases:=[]struct{name string; change func(*Policy,*Snapshot,*Capture,*int64)}{
 {"stale telemetry",func(p *Policy,s *Snapshot,c *Capture,n *int64){*n=100000}},
 {"partial ancestor vector",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.AncestorLimits=s.AncestorLimits[:1]}},
 {"invented RAM estimate",func(p *Policy,s *Snapshot,c *Capture,n *int64){c.RAMSource="estimate"}},
 {"missing RAM observation",func(p *Policy,s *Snapshot,c *Capture,n *int64){c.RAMBytes=nil}},
 {"pressure violation",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Pressure=rat(1,2)}},
 {"dirty reuse",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries[0].CleanupVerified=false}},
 {"missing reset proof",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries[0].Fresh=false}},
 {"inference seats are not capacity",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries=nil;s.InferenceSeats=500}},
 {"inventory alone is not behavior",func(p *Policy,s *Snapshot,c *Capture,n *int64){c.BehaviorMarker="not in text"}},
 {"raw capture digest mismatch",func(p *Policy,s *Snapshot,c *Capture,n *int64){c.RawCapture="modified raw"}},
 {"missing finite CPU",func(p *Policy,s *Snapshot,c *Capture,n *int64){p.Ceiling.CPU.D=0}},
 {"unknown ancestor CPU",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.AncestorLimits[0].CPU.D=0}},
 {"unfrozen memory category",func(p *Policy,s *Snapshot,c *Capture,n *int64){p.PerOuter.VMMBytes=0}},
 {"pressure observation absent",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Pressure=nil}},
 {"excess tail latency",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.TailLatencyNS=ptr(500)}},
 {"dwell violation",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.DwellNS=ptr(5)}},
 {"reaction reserve violation",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.ReactionReserveBytes=ptr(0)}},
 {"physical headroom violation",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.PhysicalHeadroomBytes=ptr(100)}},
 {"resident host limit",func(p *Policy,s *Snapshot,c *Capture,n *int64){p.Ceiling.RAMBytes=1500}},
 {"ancestor FD limit",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.AncestorLimits[0].FDs=3}},
 {"quarantined liability retained",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries=append(s.Entries,Entry{EntryID:"quarantine",LeaseID:"q-lease",Generation:2,Epoch:2,State:Quarantined,ReservedBytes:95000})}},
 {"THINK retains resident memory",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries=append(s.Entries,Entry{EntryID:"thinker",LeaseID:"t-lease",Generation:2,Epoch:2,State:Think,ReservedBytes:95000})}},
 {"booting liability retained",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries=append(s.Entries,Entry{EntryID:"boot",LeaseID:"b-lease",Generation:2,Epoch:2,State:Booting,ReservedBytes:95000})}},
 {"PID evidence absent",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.PIDs=nil}},
 {"unqualified active width",func(p *Policy,s *Snapshot,c *Capture,n *int64){s.Entries=append(s.Entries,Entry{EntryID:"active",LeaseID:"active-lease",Generation:2,Epoch:2,State:Running,ReservedBytes:500})}},
 {"censored behavior",func(p *Policy,s *Snapshot,c *Capture,n *int64){c.Censored=true}},
 }
 for _,tc:=range cases{t.Run(tc.name,func(t *testing.T){p,s,c,n:=fixture();tc.change(&p,&s,&c,&n);d:=Evaluate(p,s,c,n);if d.State!=Hold||d.Width!=s.LastQualifiedWidth||d.NeedsReservationAndPermit{t.Fatalf("must HOLD %q: %+v",tc.name,d)}})}
}
type govFake struct{mu sync.Mutex;events []string;reservations,releases,cpuOnly int;badReceipt bool;deny bool}
func(g *govFake)push(x string){g.mu.Lock();defer g.mu.Unlock();g.events=append(g.events,x)}
func(g *govFake)Reserve(c Claim)(ReservationReceipt,error){g.mu.Lock();defer g.mu.Unlock();g.events=append(g.events,"reserve");g.reservations++;if g.deny{return ReservationReceipt{},errors.New("denied")};if g.badReceipt{c.Generation++};return ReservationReceipt{Claim:c,ID:"reservation-a"},nil}
func(g *govFake)Release(r ReservationReceipt)error{g.mu.Lock();defer g.mu.Unlock();g.events=append(g.events,"release");g.releases++;return nil}
func(g *govFake)ReleaseActiveCPU(r ReservationReceipt)error{g.mu.Lock();defer g.mu.Unlock();g.events=append(g.events,"cpu");g.cpuOnly++;return nil}
type permitFake struct{g *govFake;deny,badOrder bool}
func(p permitFake)OrderedPermit(r ReservationReceipt)(BootPermit,error){p.g.push("permit");if p.deny{return BootPermit{},errors.New("no boot permit")};order:="order-a";if p.badOrder{order=""};return BootPermit{Claim:r.Claim,ReservationID:r.ID,OrderID:order},nil}
type proofFake struct{deny bool}
func(p proofFake)Verify(CleanupProof)error{if p.deny{return errors.New("cleanup not verified")};return nil}
func cleanProof()CleanupProof{return CleanupProof{EntryID:"outer-a",LeaseID:"lease-a",Generation:1,Epoch:1,Stopped:true,NoPIDs:true,WorkspacePurged:true,NoListeners:true,NoContainers:true}}
func TestOrderedAdmissionAndCleanup(t *testing.T){
 p,s,c,n:=fixture();g:=&govFake{};a:=NewAdapter(p,g,permitFake{g:g},proofFake{})
 r:=a.Acquire(s,c,n)
 if r.Decision.State!=Propose||r.Permit==nil||r.Receipt==nil{t.Fatalf("no ordered boot proof: %+v",r)}
 if got:=strings.Join(g.events,",");got!="reserve,permit"{t.Fatalf("port order: %s",got)}
 if e:=a.EnterThink("outer-a",1);e!=nil{t.Fatal(e)}
 if g.releases!=0||g.cpuOnly!=1{t.Fatal("THINK released resident RAM")}
 if e:=a.EnterThink("outer-a",1);e==nil{t.Fatal("double CPU release")}
 if e:=a.Release(CleanupProof{EntryID:"outer-a",LeaseID:"lease-a",Generation:1,Epoch:1});e==nil{t.Fatal("revoke intent released reservation")}
 if g.releases!=0{t.Fatal("early release")}
 if e:=a.Release(cleanProof());e!=nil{t.Fatal(e)}
 if g.releases!=1{t.Fatal("verified cleanup did not release")}
 if r:=a.Acquire(s,c,n);r.Decision.State!=Hold{t.Fatal("same generation claimed again")}
}
func TestConcurrencyRefusesDoubleClaim(t *testing.T){
 p,s,c,n:=fixture();g:=&govFake{};a:=NewAdapter(p,g,permitFake{g:g},proofFake{})
 const count=24;results:=make(chan Admission,count);var wg sync.WaitGroup
 for i:=0;i<count;i++{wg.Add(1);go func(){defer wg.Done();results<-a.Acquire(s,c,n)}()}
 wg.Wait();close(results)
 permits:=0;for r:=range results{if r.Permit!=nil{permits++}else if r.Decision.State!=Hold{t.Fatalf("unexpected result %+v",r)}}
 if permits!=1||g.reservations!=1{t.Fatalf("double claim: permits %d reserve %d",permits,g.reservations)}
}
func TestPermitRefusalAndCleanupDenialRetainLiability(t *testing.T){
 for _,test:=range []struct{name string;denyBoot,denyCleanup bool}{
 {"permit refused",true,false},{"reset proof refused",false,true},
 }{t.Run(test.name,func(t *testing.T){
 p,s,c,n:=fixture();g:=&govFake{};a:=NewAdapter(p,g,permitFake{g:g,deny:test.denyBoot},proofFake{deny:test.denyCleanup})
 r:=a.Acquire(s,c,n);if test.denyBoot&&(r.Decision.State!=Hold||r.Permit!=nil){t.Fatal("boot was not refused")}
 if r:=a.Acquire(s,c,n);r.Decision.State!=Hold{t.Fatal("double claimed after failure")}
 e:=a.Release(cleanProof());if test.denyCleanup&&(e==nil||g.releases!=0){t.Fatal("released without verified cleanup")}
 })}
}
func TestWrongGenerationNeverReleases(t *testing.T){
 p,s,c,n:=fixture();g:=&govFake{};a:=NewAdapter(p,g,permitFake{g:g},proofFake{})
 if a.Acquire(s,c,n).Permit==nil{t.Fatal("no boot permit")}
 for _,change:=range []func(*CleanupProof){
 func(q *CleanupProof){q.Generation++},func(q *CleanupProof){q.LeaseID="other"},func(q *CleanupProof){q.Epoch++},
 func(q *CleanupProof){q.NoPIDs=false},func(q *CleanupProof){q.WorkspacePurged=false},func(q *CleanupProof){q.NoContainers=false},
 }{q:=cleanProof();change(&q);if e:=a.Release(q);e==nil{t.Fatal("released without bound complete proof")}}
 if g.releases!=0{t.Fatal("early release")}
}
func TestUnboundReservationAndBootOrder(t *testing.T){
 for _,bad:=range []bool{false,true}{p,s,c,n:=fixture();g:=&govFake{badReceipt:bad};a:=NewAdapter(p,g,permitFake{g:g,badOrder:!bad},proofFake{})
 r:=a.Acquire(s,c,n);if r.Decision.State!=Hold||r.Permit!=nil{t.Fatalf("unbound boot: %+v",r)}}
}
