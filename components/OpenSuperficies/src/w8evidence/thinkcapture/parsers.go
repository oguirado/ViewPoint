// Package thinkcapture parses caller-supplied kernel snapshots only.
package thinkcapture

import (
 "math/bits"
 "strconv"
 "strings"
)

type Status string
const (
 Observed Status = "OBSERVED"
 Absent Status = "AUSENTE"
 Refused Status = "REFUSED"
 Censored Status = "CENSORED"
 Failed Status = "FAILED"
)
type EvidenceError struct { Status Status; Reason string }
func (e *EvidenceError) Error() string { return string(e.Status)+": "+e.Reason }
func problem(status Status, reason string) error { return &EvidenceError{status,reason} }
func classify(err error) (Status,string) {
 if e,ok:=err.(*EvidenceError);ok {return e.Status,e.Reason}
 return Refused,err.Error()
}
type Units struct { Host, Process, Cgroup string }
var KernelUnits = Units{Host:"ticks",Process:"ticks",Cgroup:"usec"}
type HostStat struct { Total, Idle, Busy uint64 }
func ParseHostStat(s string) (HostStat,error) {
 for _,line:=range strings.Split(s,"\n") {
  f:=strings.Fields(line);if len(f)==0 || f[0]!="cpu" {continue}
  if len(f)<5 || len(f)>11 {return HostStat{},problem(Refused,"malformed aggregate cpu row")}
  var total,idle uint64
  for i:=1;i<len(f);i++ {
   n,e:=strconv.ParseUint(f[i],10,64);if e!=nil {return HostStat{},problem(Refused,"invalid host cpu counter")}
   if i<=8 {v,carry:=bits.Add64(total,n,0);if carry!=0 {return HostStat{},problem(Refused,"host cpu counter overflow")};total=v}
   if i==4 || i==5 {v,carry:=bits.Add64(idle,n,0);if carry!=0 {return HostStat{},problem(Refused,"host idle overflow")};idle=v}
  }
  if total<idle {return HostStat{},problem(Refused,"host counters inconsistent")}
  return HostStat{Total:total,Idle:idle,Busy:total-idle},nil
 }
 if strings.TrimSpace(s)!="" {return HostStat{},problem(Refused,"aggregate cpu row absent in supplied text")}
 return HostStat{},problem(Absent,"aggregate cpu row missing")
}
type ProcessStat struct { PID int; Name string; UserTicks, SystemTicks, StartTicks uint64 }
func ParseProcessStat(s string) (ProcessStat,error) {
 s=strings.TrimSpace(s);open:=strings.IndexByte(s,'(');close:=strings.LastIndex(s,") ")
 if open<1 || close<=open || close+2>=len(s) {return ProcessStat{},problem(Refused,"process stat framing")}
 pid,e:=strconv.Atoi(strings.TrimSpace(s[:open]));if e!=nil || pid<=0 {return ProcessStat{},problem(Refused,"invalid process PID")}
 f:=strings.Fields(s[close+2:]);if len(f)<20 || len(f[0])!=1 {return ProcessStat{},problem(Refused,"process stat fields missing")}
 parse:=func(i int)(uint64,error){n,err:=strconv.ParseUint(f[i],10,64);if err!=nil{return 0,problem(Refused,"invalid process counter")};return n,nil}
 u,e:=parse(11);if e!=nil{return ProcessStat{},e};k,e:=parse(12);if e!=nil{return ProcessStat{},e};start,e:=parse(19);if e!=nil{return ProcessStat{},e}
 if start==0 {return ProcessStat{},problem(Refused,"process start identity missing")}
 return ProcessStat{PID:pid,Name:s[open+1:close],UserTicks:u,SystemTicks:k,StartTicks:start},nil
}
type CgroupStat struct { UsageUsec uint64 }
func ParseCgroupStat(s string) (CgroupStat,error) {
 var out CgroupStat;seen:=false
 for _,line:=range strings.Split(s,"\n") {
  f:=strings.Fields(line);if len(f)==0 {continue}
  if len(f)!=2 {return out,problem(Refused,"malformed cgroup cpu.stat")}
  n,e:=strconv.ParseUint(f[1],10,64);if e!=nil{return out,problem(Refused,"invalid cgroup counter")}
  if f[0]=="usage_usec" {if seen{return out,problem(Refused,"duplicate cgroup usage")};out.UsageUsec=n;seen=true}
 }
 if !seen{return out,problem(Absent,"cgroup usage_usec missing")}
 return out,nil
}
