// Package scalepolicy implements an offline, fail-closed scale-out proposal.
// It has no launcher, governor authority or production integration.
package scalepolicy

import (
 "crypto/sha256"
 "encoding/hex"
 "errors"
 "math"
 "math/big"
 "strings"
 "sync"
)
type Ratio struct { N,D int64 }
func ratio(r Ratio,zero bool) bool { return r.D>0 && (r.N>0 || (zero && r.N==0)) }
func le(a,b Ratio) bool {
 x:=new(big.Int).Mul(big.NewInt(a.N),big.NewInt(b.D))
 y:=new(big.Int).Mul(big.NewInt(b.N),big.NewInt(a.D))
 return x.Cmp(y)<=0
}
type Limits struct { CPU Ratio; RAMBytes,PIDs,FDs int64 }
func(l Limits) valid()bool{return ratio(l.CPU,false)&&l.RAMBytes>0&&l.PIDs>0&&l.FDs>0}
type Reservation struct {
 CPU Ratio; PIDs,FDs int64
 GuestRAMBytes,VMMBytes,TmpfsBytes,HostCLIBytes,CollectorBytes,StartupBytes int64
}
func add(a,b int64)(int64,bool){if a<0||b<0||a>math.MaxInt64-b{return 0,false};return a+b,true}
func sum(vals ...int64)(int64,bool){v:=int64(0);for _,x:=range vals{var ok bool;v,ok=add(v,x);if !ok{return 0,false}};return v,true}
func(r Reservation) TotalRAM()(int64,bool){return sum(r.GuestRAMBytes,r.VMMBytes,r.TmpfsBytes,r.HostCLIBytes,r.CollectorBytes,r.StartupBytes)}
func(r Reservation) valid()bool{
 if !ratio(r.CPU,false)||r.PIDs<=0||r.FDs<=0{return false}
 for _,x:=range []int64{r.GuestRAMBytes,r.VMMBytes,r.TmpfsBytes,r.HostCLIBytes,r.CollectorBytes,r.StartupBytes}{if x<=0{return false}}
 _,ok:=r.TotalRAM();return ok
}
type Policy struct {
 MaxWidth,ExpectedAncestors int
 MaxSampleAgeNS,MaxTailLatencyNS,MinDwellNS,MinReactionReserveBytes,MinPhysicalHeadroomBytes int64
 MaxPressure Ratio
 Ceiling Limits
 PerOuter Reservation
}
func(p Policy) valid()bool{
 return p.MaxWidth>0&&p.ExpectedAncestors>0&&p.MaxSampleAgeNS>0&&p.MaxTailLatencyNS>0&&p.MinDwellNS>0&&p.MinReactionReserveBytes>0&&p.MinPhysicalHeadroomBytes>0&&ratio(p.MaxPressure,false)&&p.Ceiling.valid()&&p.PerOuter.valid()
}
// Capture is local test evidence, not a parser or an exported capture protocol.
// The raw text is passive data; inventory alone never qualifies behavior.
type Capture struct {
 SampleID,WorkloadRepo,WorkloadCommit,EngineSHA256,NamespaceID string
 Seed,RequestedTicks,ActualTicks,AccountUID,PID,ProcessStart,AcquiredMonotonicNS int64
 ClockUnit,RawCapture,RawCaptureSHA256 string
 Inventory bool
 BehaviorMarker,RAMSource string
 CPU *Ratio; RAMBytes *int64
 Censored bool; Status,Reason string
}
func hex64(s string)bool{if len(s)!=64||strings.ToLower(s)!=s{return false};_,e:=hex.DecodeString(s);return e==nil}
func(c Capture) valid()bool{
 if c.SampleID==""||c.WorkloadRepo==""||c.WorkloadCommit==""||!hex64(c.EngineSHA256)||c.NamespaceID==""||c.RequestedTicks<=0||c.ActualTicks<=0||c.ActualTicks>c.RequestedTicks||c.AccountUID<0||c.PID<=0||c.ProcessStart<=0||c.AcquiredMonotonicNS<=0||c.ClockUnit!="ns"||c.Censored||c.Status!="verified"||c.Reason!=""{return false}
 if c.CPU==nil||!ratio(*c.CPU,true)||c.RAMBytes==nil||*c.RAMBytes<=0||c.RAMSource!="observed"{return false}
 if !hex64(c.RawCaptureSHA256)||c.RawCapture==""||strings.TrimSpace(c.BehaviorMarker)==""||!strings.Contains(c.RawCapture,c.BehaviorMarker){return false}
 digest:=sha256.Sum256([]byte(c.RawCapture));return hex.EncodeToString(digest[:])==c.RawCaptureSHA256
}
type Entry struct{
 EntryID,LeaseID,State string
 Generation,Epoch uint64
 ReservedBytes int64
 CleanupVerified,Fresh bool
}
const(Cold="cold";Warm="warm";Booting="booting";Running="running";Think="think";Stopping="stopping";Quarantined="quarantined";Hold="HOLD";Propose="PROPOSE")
func(e Entry) valid()bool{return e.EntryID!=""&&e.LeaseID!=""&&e.Generation>0&&e.Epoch>0}
type Snapshot struct{
 SampleID,WorkloadCommit string; AcquiredMonotonicNS int64
 CPU *Ratio;RAMBytes,PIDs,FDs *int64
 AncestorLimits []Limits
 PhysicalHeadroomBytes,StartupBytes,CollectorBytes,ReactionReserveBytes *int64
 Pressure *Ratio;TailLatencyNS,DwellNS *int64
 LastQualifiedWidth int
 Entries []Entry
 InferenceSeats int // Informational, NEVER an outer capacity input.
}
type Decision struct {State,Reason string; Width int; NeedsReservationAndPermit bool; Entry Entry}
func hold(s Snapshot,reason string)Decision{w:=s.LastQualifiedWidth;if w<0{w=0};return Decision{State:Hold,Reason:reason,Width:w}}
func observed(x *int64)bool{return x!=nil&&*x>=0}
func fresh(now,then,max int64)bool{return then>0&&now>=then&&now-then<=max}
func fits(u,c Limits)bool{return ratio(u.CPU,true)&&u.RAMBytes>=0&&u.PIDs>=0&&u.FDs>=0&&le(u.CPU,c.CPU)&&u.RAMBytes<=c.RAMBytes&&u.PIDs<=c.PIDs&&u.FDs<=c.FDs}
func usage(s Snapshot,r Reservation,liability int64)(Limits,bool){
 n,ok:=sum(*s.RAMBytes,liability,r.GuestRAMBytes,r.VMMBytes,r.TmpfsBytes,r.HostCLIBytes,r.CollectorBytes,r.StartupBytes,*s.StartupBytes,*s.CollectorBytes);if !ok{return Limits{},false}
 p,ok:=add(*s.PIDs,r.PIDs);if !ok{return Limits{},false}
 f,ok:=add(*s.FDs,r.FDs);if !ok{return Limits{},false}
 a:=new(big.Int).Mul(big.NewInt(s.CPU.N),big.NewInt(r.CPU.D))
 b:=new(big.Int).Mul(big.NewInt(r.CPU.N),big.NewInt(s.CPU.D));a.Add(a,b)
 d:=new(big.Int).Mul(big.NewInt(s.CPU.D),big.NewInt(r.CPU.D))
 if !a.IsInt64()||!d.IsInt64(){return Limits{},false}
 return Limits{CPU:Ratio{a.Int64(),d.Int64()},RAMBytes:n,PIDs:p,FDs:f},true
}
// Evaluate produces an incremental width proposal, never an admission grant.
func Evaluate(p Policy,s Snapshot,c Capture,nowNS int64)Decision{
 if !p.valid(){return hold(s,"missing_frozen_policy")}
 if s.LastQualifiedWidth<0||s.LastQualifiedWidth>=p.MaxWidth{return hold(s,"width_ceiling")}
 if !c.valid()||!fresh(nowNS,c.AcquiredMonotonicNS,p.MaxSampleAgeNS){return hold(s,"capture_absent_stale_unqualified")}
 if s.SampleID==""||s.SampleID!=c.SampleID||s.WorkloadCommit==""||s.WorkloadCommit!=c.WorkloadCommit||!fresh(nowNS,s.AcquiredMonotonicNS,p.MaxSampleAgeNS){return hold(s,"snapshot_stale_mismatched")}
 if s.CPU==nil||!ratio(*s.CPU,true)||!observed(s.RAMBytes)||!observed(s.PIDs)||!observed(s.FDs)||!observed(s.PhysicalHeadroomBytes)||!observed(s.StartupBytes)||!observed(s.CollectorBytes)||!observed(s.ReactionReserveBytes)||!observed(s.TailLatencyNS)||!observed(s.DwellNS)||s.Pressure==nil||!ratio(*s.Pressure,true){return hold(s,"AUSENTE_metrics")}
 if len(s.AncestorLimits)!=p.ExpectedAncestors{return hold(s,"partial_ancestor_vector")}
 for _,l:=range s.AncestorLimits{if !l.valid(){return hold(s,"invalid_ancestor_limit")}}
 if *s.ReactionReserveBytes<p.MinReactionReserveBytes||*s.PhysicalHeadroomBytes<p.MinPhysicalHeadroomBytes||*s.TailLatencyNS>p.MaxTailLatencyNS||*s.DwellNS<p.MinDwellNS||!le(*s.Pressure,p.MaxPressure){return hold(s,"pressure_tail_dwell_headroom")}
 if *c.CPU!=*s.CPU||*c.RAMBytes!=*s.RAMBytes{return hold(s,"measurement_mismatch")}
 seen:=map[string]bool{};var cold *Entry;liability:=int64(0);active:=0
 for _,e:=range s.Entries{
  if !e.valid()||seen[e.EntryID]{return hold(s,"entry_identity_duplicate")};seen[e.EntryID]=true
  switch e.State{
  case Cold:
   if e.ReservedBytes!=0||!e.CleanupVerified||!e.Fresh{return hold(s,"dirty_reuse_missing_reset")}
   if cold==nil{x:=e;cold=&x}
  case Warm,Booting,Running,Think,Stopping,Quarantined:
   if e.ReservedBytes<=0{return hold(s,"missing_resident_liability")}
   var ok bool;liability,ok=add(liability,e.ReservedBytes);if !ok{return hold(s,"liability_overflow")};active++
  default:return hold(s,"unknown_state")
  }
 }
 if cold==nil||active>s.LastQualifiedWidth{return hold(s,"no_fresh_cold_outer")}
 ram,ok:=p.PerOuter.TotalRAM();if !ok{return hold(s,"RAM_overflow")}
 need,ok:=sum(ram,liability,*s.StartupBytes,*s.CollectorBytes,*s.ReactionReserveBytes);if !ok||need>*s.PhysicalHeadroomBytes{return hold(s,"physical_headroom")}
 u,ok:=usage(s,p.PerOuter,liability);if !ok||!fits(u,p.Ceiling){return hold(s,"host_vector")}
 for _,a:=range s.AncestorLimits{if !fits(u,a){return hold(s,"ancestor_vector")}}
 return Decision{State:Propose,Reason:"bounded_increment_only",Width:s.LastQualifiedWidth+1,NeedsReservationAndPermit:true,Entry:*cold}
}
type Claim struct{EntryID,LeaseID string;Generation,Epoch uint64;Resources Reservation}
type ReservationReceipt struct{Claim Claim;ID string}
type BootPermit struct{Claim Claim;ReservationID,OrderID string}
type CleanupProof struct{EntryID,LeaseID string;Generation,Epoch uint64;Stopped,NoPIDs,WorkspacePurged,NoListeners,NoContainers bool}
// Governor, BootPermitter and CleanupVerifier are ports, not implementations.
type Governor interface{Reserve(Claim)(ReservationReceipt,error);Release(ReservationReceipt)error;ReleaseActiveCPU(ReservationReceipt)error}
type BootPermitter interface{OrderedPermit(ReservationReceipt)(BootPermit,error)}
type CleanupVerifier interface{Verify(CleanupProof)error}
type Admission struct{Decision Decision;Receipt *ReservationReceipt;Permit *BootPermit}
type held struct{r ReservationReceipt;cpuReturned bool}
type Adapter struct{
 mu sync.Mutex;p Policy;g Governor;b BootPermitter;v CleanupVerifier
 inFlight map[string]*held;used map[string]uint64
}
func NewAdapter(p Policy,g Governor,b BootPermitter,v CleanupVerifier)*Adapter{return &Adapter{p:p,g:g,b:b,v:v,inFlight:map[string]*held{},used:map[string]uint64{}}}
func(a *Adapter)Acquire(s Snapshot,c Capture,now int64)Admission{
 a.mu.Lock();defer a.mu.Unlock()
 d:=Evaluate(a.p,s,c,now);if d.State!=Propose{return Admission{Decision:d}}
 if a.g==nil||a.b==nil||a.v==nil{return Admission{Decision:hold(s,"missing_authority_ports")}}
 e:=d.Entry
 if a.inFlight[e.EntryID]!=nil||a.used[e.EntryID]>=e.Generation{return Admission{Decision:hold(s,"duplicate_or_replayed_claim")}}
 claim:=Claim{EntryID:e.EntryID,LeaseID:e.LeaseID,Generation:e.Generation,Epoch:e.Epoch,Resources:a.p.PerOuter}
 // Fence this generation even if a faulty authority returns an unbound receipt.
 a.used[e.EntryID]=e.Generation
 receipt,err:=a.g.Reserve(claim);if err!=nil{return Admission{Decision:hold(s,"governor_reserve_refused")}}
 if receipt.ID==""||receipt.Claim!=claim{return Admission{Decision:hold(s,"unbound_reservation")}}
 a.inFlight[e.EntryID]=&held{r:receipt};a.used[e.EntryID]=e.Generation
 permit,err:=a.b.OrderedPermit(receipt)
 if err!=nil||permit.Claim!=claim||permit.ReservationID!=receipt.ID||permit.OrderID==""{return Admission{Decision:hold(s,"boot_permit_refused_liability_held"),Receipt:&receipt}}
 return Admission{Decision:d,Receipt:&receipt,Permit:&permit}
}
// THINK returns active CPU only; the complete resident liability stays held.
func(a *Adapter)EnterThink(id string,generation uint64)error{
 a.mu.Lock();defer a.mu.Unlock();h:=a.inFlight[id]
 if h==nil||h.r.Claim.Generation!=generation||h.cpuReturned{return errors.New("unfenced_or_repeated_CPU_release")}
 if e:=a.g.ReleaseActiveCPU(h.r);e!=nil{return e};h.cpuReturned=true;return nil
}
// Revoke intent alone is never proof of physical cleanup.
func(a *Adapter)Release(p CleanupProof)error{
 a.mu.Lock();defer a.mu.Unlock();h:=a.inFlight[p.EntryID]
 if h==nil{return errors.New("no_resident_claim")}
 c:=h.r.Claim
 if p.EntryID!=c.EntryID||p.LeaseID!=c.LeaseID||p.Generation!=c.Generation||p.Epoch!=c.Epoch||!p.Stopped||!p.NoPIDs||!p.WorkspacePurged||!p.NoListeners||!p.NoContainers{return errors.New("incomplete_or_unbound_cleanup")}
 if e:=a.v.Verify(p);e!=nil{return e}
 if e:=a.g.Release(h.r);e!=nil{return e}
 delete(a.inFlight,p.EntryID);return nil
}
