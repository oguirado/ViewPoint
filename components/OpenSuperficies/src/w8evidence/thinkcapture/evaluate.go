package thinkcapture

import (
 "fmt"
 "math"
 "math/big"
 "math/bits"
)

type Ratio struct { Numerator,Denominator uint64 }
type Interval struct {
 Phase Phase; Status Status; Reason string; WallNanos int64
 HostNonIdle Ratio; OwnedProcess Ratio; CgroupDemand Ratio
 // Measured only with explicitly verified GroupIncludesOwned.
 // Do not sum host, process, and cgroup: their accounting overlaps.
 HostBackground *Ratio
}
func ratio(n,mult,den1,den2 uint64)(Ratio,error) {
 hi,x:=bits.Mul64(n,mult);if hi!=0{return Ratio{},problem(Refused,"CPU numerator overflow")}
 hi,y:=bits.Mul64(den1,den2);if hi!=0||y==0{return Ratio{},problem(Refused,"CPU denominator overflow")}
 return Ratio{x,y},nil
}
func delta(end,start uint64)(uint64,error){if end<start{return 0,problem(Refused,"CPU counter decreased")};return end-start,nil}
func add(a,b uint64)(uint64,error){v,carry:=bits.Add64(a,b,0);if carry!=0{return 0,problem(Refused,"process CPU overflow")};return v,nil}
func fraction(r Ratio)*big.Rat {return new(big.Rat).SetFrac(new(big.Int).SetUint64(r.Numerator),new(big.Int).SetUint64(r.Denominator))}
func remainder(a,b Ratio)(*Ratio,error) {
 x:=new(big.Rat).Sub(fraction(a),fraction(b));if x.Sign()<0{return nil,problem(Refused,"cgroup demand exceeds host non-idle demand")}
 if !x.Num().IsUint64()||!x.Denom().IsUint64(){return nil,problem(Refused,"background fraction overflow")}
 return &Ratio{x.Num().Uint64(),x.Denom().Uint64()},nil
}
func (r *Recorder) Evaluate() []Interval {
 result:=make([]Interval,0,len(phases))
 for _,p:=range phases {
  in:=Interval{Phase:p};v:=r.events[p]
  switch {
  case v==nil:in.Status=Absent;in.Reason="phase endpoints missing"
  case v.mark!= "":in.Status=v.mark;in.Reason=v.reason
  case v.start==nil || v.end==nil:in.Status=Absent;in.Reason="phase endpoints missing"
  case v.start.err!=nil:in.Status,in.Reason=classify(v.start.err)
  case v.end.err!=nil:in.Status,in.Reason=classify(v.end.err)
  default:
   a,b:=v.start,v.end
   var err error
   if b.at<=a.at {err=problem(Refused,"nonpositive monotonic wall interval")
   } else if uint64(b.at)-uint64(a.at)>math.MaxInt64 {err=problem(Refused,"monotonic interval overflow")
   } else if b.seq<=a.seq {err=problem(Refused,"stale or reordered snapshot")
   } else if b.proc.PID!=a.proc.PID || b.proc.StartTicks!=a.proc.StartTicks {err=problem(Refused,"PID reuse")}
   if err==nil {
    in.WallNanos=b.at-a.at;wall:=uint64(in.WallNanos)
    var h,pu,ps,g uint64
    h,err=delta(b.host.Busy,a.host.Busy)
    if err==nil {pu,err=delta(b.proc.UserTicks,a.proc.UserTicks)}
    if err==nil {ps,err=delta(b.proc.SystemTicks,a.proc.SystemTicks)}
    if err==nil {g,err=delta(b.group.UsageUsec,a.group.UsageUsec)}
    if err==nil {pu,err=add(pu,ps)}
    if err==nil {in.HostNonIdle,err=ratio(h,1_000_000_000,r.meta.ClockHz,wall)}
    if err==nil {in.OwnedProcess,err=ratio(pu,1_000_000_000,r.meta.ClockHz,wall)}
    if err==nil {in.CgroupDemand,err=ratio(g,1_000,wall,1)}
    if err==nil && r.meta.GroupIncludesOwned {in.HostBackground,err=remainder(in.HostNonIdle,in.CgroupDemand)}
   }
   if err!=nil {in.Status,in.Reason=classify(err)}else{in.Status=Observed}
  }
  result=append(result,in)
 }
 return result
}
// GameCapacity refuses idle CPU or inference-seat counts as game throughput evidence.
func GameCapacity(basis string) error {return problem(Absent,"game capacity unmeasured: "+basis+" is not a game benchmark")}
func (i Interval) String() string {return fmt.Sprintf("%s %s: %s",i.Phase,i.Status,i.Reason)}
