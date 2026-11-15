package thinkcapture

import (
 "strings"
 "testing"
)

type fakeClock struct{ n int64 }
func(c *fakeClock) MonoNanos() int64{return c.n}
func proc(pid int, name string, user,sys,start uint64) string {
 return strings.Join([]string{
  itoa(pid),"("+name+")","S","1","1","1","0","0","0","0","0","0","0",
  utoa(user),utoa(sys),"0","0","0","0","0","0",utoa(start)}," ")
}
func itoa(i int) string {if i==42{return "42"};if i==43{return "43"};return "44"}
func utoa(n uint64) string {const digits="0123456789";if n==0{return "0"};b:="";for n>0 {b=string(digits[n%10])+b;n/=10};return b}
func snap(seq uint64,busy,idle,user,sys,cgroup uint64) Snapshot {
 return Snapshot{HostText:"cpu  "+utoa(busy)+" 0 0 "+utoa(idle)+" 0 0 0 0 0 0\ncpu0 1 2 3 4",ProcessText:proc(42,"game (THINK) engine",user,sys,900),CgroupText:"usage_usec "+utoa(cgroup)+"\nuser_usec 100\nsystem_usec 100\n",Sequence:seq,Units:KernelUnits,EnginePin:"engine@sha256:deadbeef",SourcePin:"source@53df"}
}
func meta() Provenance {return Provenance{Engine:"game",EnginePin:"engine@sha256:deadbeef",SourcePin:"source@53df",Seed:7,RequestedTicks:12,ActualTicks:11,ClockHz:100,ProcessPID:42,ProcessStartTicks:900,Units:KernelUnits,GroupIncludesOwned:true}}
func status(t *testing.T,got Interval,want Status){t.Helper();if got.Status!=want {t.Fatalf("%s: status %s != %s (%s)",got.Phase,got.Status,want,got.Reason)}}
func TestParsers(t *testing.T){
 p,e:=ParseProcessStat(proc(42,"name (nested) with spaces ) bracket",81,19,900));if e!=nil||p.PID!=42||p.UserTicks!=81||p.SystemTicks!=19||p.StartTicks!=900||p.Name!="name (nested) with spaces ) bracket" {t.Fatalf("proc = %+v %v",p,e)}
 h,e:=ParseHostStat("cpu 10 2 3 20 5 6 7 8 999 999\ncpu0 1 2 3 4");if e!=nil||h.Total!=61||h.Idle!=25||h.Busy!=36 {t.Fatalf("host=%+v %v",h,e)}
 g,e:=ParseCgroupStat("usage_usec 123\nuser_usec 100\nsystem_usec 23\n");if e!=nil||g.UsageUsec!=123 {t.Fatalf("cgroup=%+v %v",g,e)}
 for _,bad:=range []string{"","cpu 1","cpu 1 0 0 x"}{if _,e:=ParseHostStat(bad);e==nil{t.Errorf("host accepted %q",bad)}}
 for _,bad:=range []string{"1 (bad) S","42 (bad) S broken","pid (bad) S 1"}{if _,e:=ParseProcessStat(bad);e==nil{t.Errorf("process accepted %q",bad)}}
 for _,bad:=range []string{"user_usec 1","usage_usec 1\nusage_usec 2","usage_usec -1"}{if _,e:=ParseCgroupStat(bad);e==nil{t.Errorf("cgroup accepted %q",bad)}}
}
