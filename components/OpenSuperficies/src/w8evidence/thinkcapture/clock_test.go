package thinkcapture
import "testing"
func TestClockAndIdentityRefusals(t *testing.T) {
 a:=snap(1,100,100,10,0,100000)
 b:=snap(2,200,100,20,0,200000)
 checks:=[]struct{name string;prepare func(*Provenance,*Snapshot,*int64);want Status}{
  {"missing_hz",func(p *Provenance,s *Snapshot,n *int64){p.ClockHz=0},Absent},
  {"zero_wall",func(p *Provenance,s *Snapshot,n *int64){*n=1e9},Refused},
  {"reused_pid",func(p *Provenance,s *Snapshot,n *int64){s.ProcessText=proc(42,"second",20,0,901)},Refused},
  {"stale_sequence",func(p *Provenance,s *Snapshot,n *int64){s.Sequence=1},Refused},
  {"wrong_units",func(p *Provenance,s *Snapshot,n *int64){s.Units.Cgroup="nsec"},Refused},
 }
 for _,tc:=range checks {t.Run(tc.name,func(t *testing.T){
  p:=meta();v:=b;end:=int64(2e9);tc.prepare(&p,&v,&end)
  clock:=&fakeClock{n:1e9};r:=NewRecorder(p,clock)
  _=r.Capture(Think,Start,a);clock.n=end;_=r.Capture(Think,End,v)
  status(t,r.Evaluate()[2],tc.want)
 })}
}
