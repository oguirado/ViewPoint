package pool

import (
    "errors"
    "math"
    "reflect"
    "sync"
    "sync/atomic"
    "testing"
    "time"
)

type fakeGovernor struct { reservations map[string]Vec; refused bool; releases int }
func (g *fakeGovernor) Reserve(id string,v Vec) error {if g.refused{return errors.New("admission refused")};if g.reservations==nil{g.reservations=map[string]Vec{}};g.reservations[id]=v;return nil}
func (g *fakeGovernor) Release(id string,v Vec) error {if g.refused{return errors.New("release refused")};if g.reservations[id]!=v{return errors.New("wrong governor release")};delete(g.reservations,id);g.releases++;return nil}
type fakeBoot struct { count int; refused bool }
func (b *fakeBoot) Acquire(string)(func(),error){if b.refused{return nil,errors.New("permit refused")};b.count++;return func(){},nil}
type fakeCleaner struct { proof *CleanupEvidence; err error; calls int }
func (c *fakeCleaner) Clean(id string,gen uint64)(CleanupProof,error){c.calls++;if c.err!=nil{return nil,c.err};if c.proof!=nil{return c.proof,nil};return &CleanupEvidence{EntryID:id,Generation:gen,Verified:true},nil}
type fakeClock struct{}
func (fakeClock) Now() time.Time{return time.Unix(1234,0)}
func goodTemplate() Template{return Template{Storage:"verified-noswap-ram",TaskFree:true,Immutable:true,Pin:"fixed",State:"clean"}}
func setup(t *testing.T,cap uint64)(*Pool,*fakeGovernor,*fakeBoot,*fakeCleaner){t.Helper();g,b,c:=&fakeGovernor{},&fakeBoot{},&fakeCleaner{};p,err:=NewPool(Vec{cap,cap,cap,cap},g,b,c,fakeClock{},"fixed");if err!=nil{t.Fatal(err)};return p,g,b,c}
func v(x uint64) Vec{return Vec{x,x,x,x}}
func must(t *testing.T,err error){t.Helper();if err!=nil{t.Fatal(err)}}
func reserve(t *testing.T,p *Pool,id string,n uint64){t.Helper();must(t,p.Reserve(id,v(n),p.Snapshot().Version))}
func cold(t *testing.T,p *Pool,id string){t.Helper();must(t,p.ColdReady(id,goodTemplate()))}
func claim(t *testing.T,p *Pool,id string)Lease{t.Helper();l,err:=p.Claim(id,p.Snapshot().Version);must(t,err);return l}
func cleanup(t *testing.T,p *Pool,id string)*CleanupEvidence{t.Helper();must(t,p.Fence(id));must(t,p.Collect(id));must(t,p.Stop(id));must(t,p.Purge(id));proof,err:=p.Verify(id);must(t,err);return proof}
func denied(t *testing.T,err error){t.Helper();if err==nil{t.Fatal("must fail but succeeded")}}

func TestCompatibilityMustFail(t *testing.T){
    base:=goodTemplate()
    cases:=map[string]func(*Template){
        "disk snapshot":func(x *Template){x.Storage="disk"},
        "used template":func(x *Template){x.TaskFree=false},
        "dirty template":func(x *Template){x.State="dirty"},
        "mutable template":func(x *Template){x.Immutable=false},
        "pin drift":func(x *Template){x.Pin="different"},
    }
    if !Compatible(base,"fixed"){t.Fatal("valid RAM template rejected")}
    for name,change:=range cases{t.Run(name,func(t *testing.T){x:=base;change(&x);if Compatible(x,"fixed"){t.Fatal("unsafe template accepted")};p,_,_,_:=setup(t,5);reserve(t,p,"cold",1);denied(t,p.ColdReady("cold",x))})}
}
func TestCapacityAndVerifiedReplacement(t *testing.T){
    p,g,b,_:=setup(t,10)
    reserve(t,p,"template",2)
    reserve(t,p,"dirty-old",4);cold(t,p,"dirty-old")
    old:=claim(t,p,"dirty-old");must(t,p.MarkDirty(old.ID))
    reserve(t,p,"warm-new",4);cold(t,p,"warm-new")
    readyVersion:=p.Snapshot().Version
    got,err:=p.Claim("warm-new",readyVersion);must(t,err)
    if got.Generation==0 || b.count!=2{t.Fatalf("cold generation / permits %+v %d",got,b.count)}
    _,err=p.Claim("warm-new",p.Snapshot().Version);denied(t,err)
    _,err=p.Claim("warm-new",readyVersion);denied(t,err)
    if err=p.Reserve("replacement",v(4),p.Snapshot().Version);CodeOf(err)!=4{t.Fatalf("over-cap replacement rc=%d err=%v",CodeOf(err),err)}
    if p.Holds()!=v(10){t.Fatalf("quarantined/dirty hold lost %+v",p.Holds())}
    denied(t,p.Release("dirty-old",nil,"first"))
    denied(t,p.Release("dirty-old",&CleanupEvidence{EntryID:"dirty-old",Generation:old.Generation,Verified:true},"first")) // claimed-but-unfenced
    proof:=cleanup(t,p,"dirty-old")
    denied(t,p.Release("dirty-old",&CleanupEvidence{EntryID:"dirty-old",Generation:old.Generation,Verified:false},"first"))
    denied(t,p.Release("missing",proof,"first"))
    must(t,p.Release("dirty-old",proof,"first"))
    if g.releases!=1||p.Holds()!=v(6){t.Fatalf("released wrongly: %d %+v",g.releases,p.Holds())}
    before:=p.Snapshot().Version
    must(t,p.Release("dirty-old",proof,"first")) // idempotent same key
    if p.Snapshot().Version!=before||g.releases!=1{t.Fatal("idempotent release changed state")}
    denied(t,p.Release("dirty-old",proof,"second"))
    reserve(t,p,"replacement",4)
    if p.Holds()!=v(10){t.Fatal("replacement not charged")}
    snap:=p.Snapshot();clone,_,_,_:=setup(t,10)
    must(t,clone.Restore(snap))
    if !reflect.DeepEqual(clone.Snapshot(),snap){t.Fatal("journal snapshot differs after restore")}
    denied(t,p.Restore(PoolSnapshot{Cap:snap.Cap,Version:snap.Version+1,Holds:snap.Holds,Entries:snap.Entries}))
    denied(t,p.ColdReady("warm-new",goodTemplate())) // claimed can never return to warm
    if p.Snapshot().Version!=snap.Version{t.Fatal("rejected ops changed version")}
}
func TestQuarantineAndProof(t *testing.T){
    p,_,_,cleaner:=setup(t,4)
    reserve(t,p,"old",4);cold(t,p,"old");lease:=claim(t,p,"old")
    must(t,p.MarkDirty("old"))
    must(t,p.Fence("old"));must(t,p.Collect("old"));must(t,p.Stop("old"));must(t,p.Purge("old"))
    cleaner.err=errors.New("uncertain")
    _,err:=p.Verify("old");denied(t,err)
    if p.Holds()!=v(4){t.Fatal("quarantine incorrectly counted as free")}
    denied(t,p.Release("old",nil,"key"))
    if err=p.Reserve("new",v(4),p.Snapshot().Version);CodeOf(err)!=4{t.Fatalf("quarantine capacity rc=%d",CodeOf(err))}
    wrong:=&CleanupEvidence{EntryID:"old",Generation:lease.Generation+1,Verified:true}
    denied(t,p.Release("old",wrong,"key"))
    must(t,p.Release("old",&CleanupEvidence{EntryID:"old",Generation:lease.Generation,Verified:true},"key"))
    if p.Holds()!=v(0){t.Fatal("verified cleanup did not free hold")}
}
func TestDisabledRestorationAndBackpressure(t *testing.T){
    p,_,boot,_:=setup(t,2)
    _,err:=p.Claim("",p.Snapshot().Version)
    if CodeOf(err)!=4{t.Fatalf("empty pool rc=%d",CodeOf(err))}
    reserve(t,p,"restored",2)
    denied(t,p.TemplateReady("restored",goodTemplate()))
    snapshot:=p.Snapshot()
    snapshot.Version++
    snapshot.Entries[0].State=Warm
    snapshot.Entries[0].Source=SourceTemplate
    snapshot.Entries[0].Template=goodTemplate()
    fresh,_,_,_:=setup(t,2)
    must(t,fresh.Restore(snapshot))
    _,err=fresh.Claim("restored",fresh.Snapshot().Version)
    denied(t,err)
    if boot.count!=0{t.Fatal("restored template somehow launched")}
}
func TestCheckedOverflowAndPermit(t *testing.T){
    p,g,b,_:=setup(t,math.MaxUint64)
    reserve(t,p,"big",math.MaxUint64)
    denied(t,p.Reserve("overflow",v(1),p.Snapshot().Version))
    if len(g.reservations)!=1{t.Fatal("governor admitted overflowing hold")}
    b.refused=true
    denied(t,p.ColdReady("big",goodTemplate()))
    if p.Holds()!=v(math.MaxUint64) || p.Snapshot().Entries[0].State!=Preparing {t.Fatal("failed boot permit uncharged reservation")}
}
func TestWrongOrderAndImmutableSnapshots(t *testing.T){
    p,_,_,_:=setup(t,3);reserve(t,p,"a",3);cold(t,p,"a");claim(t,p,"a")
    denied(t,p.Collect("a"));denied(t,p.Stop("a"));denied(t,p.Purge("a"))
    must(t,p.Fence("a"));denied(t,p.Purge("a"));denied(t,p.Release("a",&CleanupEvidence{EntryID:"a",Generation:2,Verified:true},"k"))
    snap:=p.Snapshot();snap.Entries[0].ID="tampered"
    if p.Snapshot().Entries[0].ID!="a"{t.Fatal("snapshot aliased live entry")}
    bad:=p.Snapshot();bad.Holds=v(2)
    fresh,_,_,_:=setup(t,3);denied(t,fresh.Restore(bad))
    if fresh.Snapshot().Version!=0{t.Fatal("invalid restore modified pool")}
}
func TestRacingClaimExactlyOneWinner(t *testing.T){
    p,_,_,_:=setup(t,2);reserve(t,p,"racer",2);cold(t,p,"racer")
    ver:=p.Snapshot().Version
    const n=48
    var success atomic.Int64
    var wg sync.WaitGroup
    for i:=0;i<n;i++{wg.Add(1);go func(){defer wg.Done();if _,err:=p.Claim("racer",ver);err==nil{success.Add(1)}}()}
    wg.Wait()
    if success.Load()!=1{t.Fatalf("expected one claim winner got %d",success.Load())}
}
