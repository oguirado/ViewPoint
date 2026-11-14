package thinkcapture

import (
 "io"
 "strings"
 "testing"
)

func TestMissingCensoredFailedAndReaders(t *testing.T){
 clock:=&fakeClock{n:1};r:=NewRecorder(meta(),clock)
 a:=snap(1,100,100,10,0,100)
 if err:=r.Capture(Think,Start,a);err!=nil{t.Fatal(err)}
 status(t,r.Evaluate()[2],Absent)
 if err:=r.Mark(Boot,Censored,"deadline expired");err!=nil{t.Fatal(err)}
 if err:=r.Mark(Step,Failed,"execution rejected");err!=nil{t.Fatal(err)}
 got:=r.Evaluate()
 status(t,got[0],Censored);status(t,got[1],Failed)
 if got[0].Reason==""||got[1].Reason==""{t.Fatal("missing interval reasons")}
 rs:=Readers{Host:strings.NewReader(a.HostText),Process:strings.NewReader(a.ProcessText),Cgroup:strings.NewReader(a.CgroupText)}
 in,err:=SnapshotFromReaders(rs,1,KernelUnits,a.EnginePin,a.SourcePin)
 if err!=nil||in.HostText!=a.HostText{t.Fatal(err)}
 tooLong:=strings.Repeat("x",1<<20+1)
 _,err=SnapshotFromReaders(Readers{Host:strings.NewReader(tooLong),Process:strings.NewReader(""),Cgroup:strings.NewReader("")},1,KernelUnits,"e","s")
 if err==nil{t.Fatal("accepted unbounded input")}
 clock.n=2
 err=r.CaptureReaders(Collection,Start,Readers{Host:io.LimitReader(strings.NewReader(a.HostText),1),Process:strings.NewReader(a.ProcessText),Cgroup:strings.NewReader(a.CgroupText)},3,KernelUnits,a.EnginePin,a.SourcePin)
 if err==nil{t.Fatal("accepted truncated host row")}
 clock.n=3
 _=r.Capture(Collection,End,snap(4,200,100,20,0,200))
 status(t,r.Evaluate()[3],Refused)
}
func TestCapacityIsNotCPUIdleOrInferenceSeats(t *testing.T){
 for _,source:=range []string{"idle CPU","inference-seat count"}{
  if err:=GameCapacity(source);err==nil{t.Errorf("incorrect game-capacity evidence from %s",source)}
 }
}
