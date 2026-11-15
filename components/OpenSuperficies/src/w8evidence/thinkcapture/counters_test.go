package thinkcapture
import ("math";"testing")
func TestCounterRefusals(t *testing.T) {
 for _,tc:=range []struct{
  name string
  mutate func(*Provenance,*Snapshot)
 }{
  {"cgroup_decrease",func(p *Provenance,s *Snapshot){s.CgroupText="usage_usec 1"}},
  {"host_decrease",func(p *Provenance,s *Snapshot){s.HostText="cpu 0 0 0 100 0 0 0 0"}},
  {"clock_product_overflow",func(p *Provenance,s *Snapshot){p.ClockHz=math.MaxUint64}},
  {"process_sum_overflow",func(p *Provenance,s *Snapshot){s.ProcessText=proc(42,"owned",math.MaxUint64,1,900)}},
 } {
  t.Run(tc.name,func(t *testing.T){
   p:=meta();a:=snap(1,100,100,0,0,100000);b:=snap(2,200,100,20,0,200000)
   tc.mutate(&p,&b);clock:=&fakeClock{n:1e9};r:=NewRecorder(p,clock)
   _=r.Capture(Think,Start,a);clock.n=2e9;_=r.Capture(Think,End,b)
   status(t,r.Evaluate()[2],Refused)
  })
 }
}
