package thinkcapture

import (
 "io"
 "strings"
)

type Provenance struct {
 Engine string; EnginePin string; SourcePin string
 Seed uint64; RequestedTicks uint64; ActualTicks uint64
 ClockHz uint64; ProcessPID int; ProcessStartTicks uint64
 Units Units
 // Set only when a separately verified cgroup membership contains this process.
 GroupIncludesOwned bool
}
func (p Provenance) validate() error {
 if p.ClockHz==0 {return problem(Absent,"caller-measured clock frequency missing")}
 if p.ProcessPID<=0 || p.ProcessStartTicks==0 || p.Engine=="" || p.EnginePin=="" || p.SourcePin=="" {return problem(Absent,"process identity or provenance pin missing")}
 if p.Units!=KernelUnits {return problem(Refused,"provenance unit mismatch")}
 return nil
}
type Phase string
const ( Boot Phase="boot"; Step Phase="step"; Think Phase="THINK"; Collection Phase="collection" )
var phases=[]Phase{Boot,Step,Think,Collection}
type Edge string
const ( Start Edge="start"; End Edge="end" )
type Clock interface { MonoNanos() int64 }
// Snapshot is passive caller-supplied text, never a path on the host.
type Snapshot struct {
 HostText, ProcessText, CgroupText string
 Sequence uint64; Units Units; EnginePin, SourcePin string
}
type Readers struct { Host, Process, Cgroup io.Reader }
// SnapshotFromReaders performs bounded reads of caller-provided streams only.
func SnapshotFromReaders(rs Readers, seq uint64, u Units, enginePin,sourcePin string) (Snapshot,error) {
 read:=func(r io.Reader)(string,error){if r==nil{return "",problem(Absent,"input reader missing")};b,e:=io.ReadAll(io.LimitReader(r,1<<20+1));if e!=nil{return "",problem(Refused,"input reader failed")};if len(b)>1<<20{return "",problem(Refused,"input exceeds size limit")};return string(b),nil}
 h,e:=read(rs.Host);if e!=nil{return Snapshot{},e};p,e:=read(rs.Process);if e!=nil{return Snapshot{},e};c,e:=read(rs.Cgroup);if e!=nil{return Snapshot{},e}
 return Snapshot{h,p,c,seq,u,enginePin,sourcePin},nil
}
type sample struct { at int64; seq uint64; host HostStat; proc ProcessStat; group CgroupStat; err error }
type pending struct { start,end *sample; mark Status; reason string }
type Recorder struct { meta Provenance; clock Clock; events map[Phase]*pending }
func NewRecorder(meta Provenance,clock Clock) *Recorder {return &Recorder{meta:meta,clock:clock,events:map[Phase]*pending{}}}
func validPhase(p Phase) bool {for _,x:=range phases {if x==p{return true}};return false}
func (r *Recorder) slot(p Phase)*pending {if r.events[p]==nil {r.events[p]=&pending{}};return r.events[p]}
func (r *Recorder) Capture(p Phase,edge Edge,input Snapshot) error {
 if !validPhase(p) || (edge!=Start && edge!=End) {return problem(Refused,"unknown phase or edge")}
 v:=r.slot(p);if (edge==Start && v.start!=nil)||(edge==End && v.end!=nil) {return problem(Refused,"duplicate phase endpoint")}
 s:=&sample{seq:input.Sequence}
 if r.clock==nil {s.err=problem(Absent,"monotonic clock missing")} else {s.at=r.clock.MonoNanos()}
 if s.err==nil {s.err=r.meta.validate()}
 if s.err==nil && input.Sequence==0 {s.err=problem(Absent,"sample sequence missing")}
 if s.err==nil && (input.Units!=r.meta.Units || input.EnginePin!=r.meta.EnginePin || input.SourcePin!=r.meta.SourcePin) {s.err=problem(Refused,"snapshot units or source pins mismatch")}
 if s.err==nil {s.host,s.err=ParseHostStat(input.HostText)}
 if s.err==nil {s.proc,s.err=ParseProcessStat(input.ProcessText)}
 if s.err==nil {s.group,s.err=ParseCgroupStat(input.CgroupText)}
 if s.err==nil && (s.proc.PID!=r.meta.ProcessPID || s.proc.StartTicks!=r.meta.ProcessStartTicks) {s.err=problem(Refused,"PID reused or process identity mismatch")}
 if edge==Start {v.start=s}else{v.end=s}
 return s.err
}
func (r *Recorder) CaptureReaders(p Phase, edge Edge, rs Readers,seq uint64,u Units,enginePin,sourcePin string) error {
 snap,err:=SnapshotFromReaders(rs,seq,u,enginePin,sourcePin)
 if err!=nil {
  if !validPhase(p)|| (edge!=Start&&edge!=End) {return err}
  v:=r.slot(p);s:=&sample{err:err}
  if edge==Start {if v.start!=nil{return problem(Refused,"duplicate phase endpoint")};v.start=s
  }else{if v.end!=nil{return problem(Refused,"duplicate phase endpoint")};v.end=s}
  return err
 }
 return r.Capture(p,edge,snap)
}
// Mark retains censored or failed intervals, including their explicit reason.
func (r *Recorder) Mark(p Phase,status Status,reason string) error {
 if !validPhase(p)|| (status!=Censored && status!=Failed)||strings.TrimSpace(reason)=="" {return problem(Refused,"invalid interval marker")}
 v:=r.slot(p);v.mark=status;v.reason=reason;return nil
}
