package thinkcapture

import "testing"

func TestPhaseIsolationAndExactRatios(t *testing.T){
 c:=&fakeClock{};r:=NewRecorder(meta(),c)
 fixtures:=[]struct{p Phase;at0,at1 int64;a,b Snapshot}{
  {Boot,1e9,2e9,snap(1,100,100,10,0,100000),snap(2,200,110,90,0,850000)},
  {Step,3e9,4e9,snap(3,200,110,90,0,850000),snap(4,300,120,130,0,1150000)},
  {Think,5e9,7e9,snap(5,300,120,130,0,1150000),snap(6,450,130,230,0,2150000)},
  {Collection,8e9,9e9,snap(7,450,130,230,0,2150000),snap(8,650,140,400,0,3850000)},
 }
 for _,f:=range fixtures {c.n=f.at0;if e:=r.Capture(f.p,Start,f.a);e!=nil{t.Fatal(e)};c.n=f.at1;if e:=r.Capture(f.p,End,f.b);e!=nil{t.Fatal(e)}}
 got:=r.Evaluate();if len(got)!=4{t.Fatal(got)};for _,v:=range got{status(t,v,Observed)}
 think:=got[2];if think.WallNanos!=2e9{t.Fatal(think)}
 // 100 owned CPU ticks, measured frequency 100Hz, exactly 2 THINK wall seconds.
 if think.OwnedProcess!=(Ratio{100_000_000_000,200_000_000_000}){t.Fatal("owned exact ratio",think)}
 if think.CgroupDemand!=(Ratio{1_000_000_000,2_000_000_000}){t.Fatal("cgroup ratio",think)}
 // Host aggregate overlaps with owned and cgroup; background is a residual, never the sum.
 if think.HostNonIdle!=(Ratio{150_000_000_000,200_000_000_000}){t.Fatal("host ratio",think)}
 if think.HostBackground==nil||*think.HostBackground!=(Ratio{1,4}){t.Fatal("background",think)}
 if got[0].OwnedProcess==think.OwnedProcess||got[3].OwnedProcess==think.OwnedProcess{t.Fatal("cross-phase averaging")}
}
