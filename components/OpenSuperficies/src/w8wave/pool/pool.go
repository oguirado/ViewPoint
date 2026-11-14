// Package pool models W8X capsule reservations, cold preparation, leases and
// verified cleanup. It performs no launches or I/O; the host owns its ports.
package pool

import (
    "errors"
    "fmt"
    "math"
    "sort"
    "sync"
    "time"
)

// EntryState is the closed set of lifecycle states.
type EntryState string
const (
    Preparing EntryState = "preparing"
    Warm EntryState = "warm"
    Claimed EntryState = "claimed"
    Draining EntryState = "draining"
    Dirty EntryState = "dirty"
    Cleaning EntryState = "cleaning"
    Released EntryState = "released"
    Quarantined EntryState = "quarantined"
)

// Template records the independently attested snapshot prerequisites.
// Enabled authorizes template restoration only; cold preparation works with it false.
type Template struct {
    Storage string
    TaskFree bool
    Immutable bool
    Pin string
    State string
    Enabled bool
}

// Compatible deliberately does not grant the separate Enabled permission.
func Compatible(t Template, pin string) bool {
    return pin != "" && t.Storage == "verified-noswap-ram" && t.TaskFree &&
        t.Immutable && t.Pin == pin && t.State == "clean"
}

// Vec uses checked unsigned counters; every dimension must fit independently.
type Vec struct { MemBytes, CPUMilli, PIDs, Files uint64 }
func (v Vec) empty() bool { return v == (Vec{}) }
func add(a,b Vec) (Vec,bool) {
    if b.MemBytes > math.MaxUint64-a.MemBytes || b.CPUMilli > math.MaxUint64-a.CPUMilli ||
        b.PIDs > math.MaxUint64-a.PIDs || b.Files > math.MaxUint64-a.Files { return Vec{},false }
    return Vec{a.MemBytes+b.MemBytes,a.CPUMilli+b.CPUMilli,a.PIDs+b.PIDs,a.Files+b.Files},true
}
func fits(a,cap Vec) bool {
    return a.MemBytes<=cap.MemBytes && a.CPUMilli<=cap.CPUMilli &&
        a.PIDs<=cap.PIDs && a.Files<=cap.Files
}
func subtract(a,b Vec) Vec {
    return Vec{a.MemBytes-b.MemBytes,a.CPUMilli-b.CPUMilli,a.PIDs-b.PIDs,a.Files-b.Files}
}

// Ports are deliberately narrow; the existing governor is the sole admission
// authority. Its future adapter must persist/reconcile reservations with WAL.
type Governor interface { Reserve(id string, v Vec) error; Release(id string, v Vec) error }
type BootPermit interface { Acquire(id string) (release func(), err error) }
type Cleaner interface { Clean(id string, generation uint64) (CleanupProof, error) }
type Clock interface { Now() time.Time }

// CleanupProof is pointer-shaped so nil is a rejectable missing witness.
// A verified witness is valid only for its exact entry and lease generation.
type CleanupEvidence struct { EntryID string; Generation uint64; Verified bool }
type CleanupProof = *CleanupEvidence

type Lease struct { ID string; Generation uint64; Version uint64; ClaimedAt time.Time }

type CleanupStage uint8
const (
    StepNone CleanupStage = iota
    StepFenced
    StepCollected
    StepStopped
    StepPurged
    StepVerified
)

const ( SourceCold = "cold"; SourceTemplate = "template" )

// EntrySnapshot contains all replay-relevant state, including released
// tombstones for idempotency and generation fencing.
type EntrySnapshot struct {
    ID string
    Resources Vec
    State EntryState
    Generation uint64
    Template Template
    Source string
    Stage CleanupStage
    ReleaseKey string
    ClaimedAt time.Time
}
type PoolSnapshot struct { Cap Vec; Holds Vec; Version uint64; Entries []EntrySnapshot }

// Fault exposes the W8 exit-code category (never rc 1).
type Fault struct { RC int; Reason string }
func (e *Fault) Error() string { return fmt.Sprintf("pool rc %d: %s",e.RC,e.Reason) }
func (e *Fault) Code() int { return e.RC }
func refuse(code int, reason string) error { return &Fault{code,reason} }

// Pool serializes every state transition, including calls to its fakeable ports.
// Do not mutate exported Cap or Version directly after construction.
type Pool struct {
    mu sync.Mutex
    Version uint64
    Cap Vec
    holds Vec
    entries map[string]*EntrySnapshot
    governor Governor
    boot BootPermit
    cleaner Cleaner
    clock Clock
    pin string
}

func NewPool(cap Vec, governor Governor, boot BootPermit, cleaner Cleaner, clock Clock, pin string) (*Pool,error) {
    if governor==nil || boot==nil || cleaner==nil || clock==nil || pin=="" {
        return nil,refuse(2,"governor, boot permit, cleaner, clock and pin required")
    }
    return &Pool{Cap:cap,entries:make(map[string]*EntrySnapshot),governor:governor,boot:boot,cleaner:cleaner,clock:clock,pin:pin},nil
}
func (p *Pool) bump() error {
    if p.Version==math.MaxUint64 { return refuse(5,"version exhausted") }
    p.Version++
    return nil
}
func (p *Pool) entry(id string) (*EntrySnapshot,error) {
    e,ok:=p.entries[id]
    if !ok { return nil,refuse(4,"entry unavailable (backpressure)") }
    return e,nil
}

// Reserve holds capacity and calls the governor BEFORE any launch is eligible.
// The hold belongs to this id even when idle, dirty or quarantined.
func (p *Pool) Reserve(id string,v Vec,ver uint64) error {
    p.mu.Lock(); defer p.mu.Unlock()
    if id=="" || v.empty() { return refuse(2,"id and nonzero vector required") }
    if ver!=p.Version { return refuse(5,"stale reservation version") }
    if _,ok:=p.entries[id];ok { return refuse(5,"id already used") }
    if p.Version==math.MaxUint64 { return refuse(5,"version exhausted") }
    h,ok:=add(p.holds,v)
    if !ok || !fits(h,p.Cap) { return refuse(4,"capacity exhausted (backpressure)") }
    if err:=p.governor.Reserve(id,v);err!=nil { return fmt.Errorf("governor reserve: %w",err) }
    p.entries[id]=&EntrySnapshot{ID:id,Resources:v,State:Preparing,Generation:1}
    p.holds=h
    return p.bump()
}

// ColdReady models a qualified cold launch. Its permit is the same ordered
// permit used by all launches; a failed permit keeps the initial hold charged.
func (p *Pool) ColdReady(id string,t Template) error {
    p.mu.Lock(); defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State!=Preparing {return refuse(5,"not preparing")}
    if !Compatible(t,p.pin) {return refuse(5,"incompatible cold template")}
    if p.Version==math.MaxUint64 {return refuse(5,"version exhausted")}
    done,err:=p.boot.Acquire(id);if err!=nil{return fmt.Errorf("boot permit: %w",err)}
    if done==nil {return refuse(5,"boot permit not granted")}
    defer done()
    e.Template=t;e.Source=SourceCold;e.State=Warm
    return p.bump()
}

// TemplateReady is explicitly opt-in. The default disabled template mode has
// no privileged restore path; it never grants a cold-qualified origin.
func (p *Pool) TemplateReady(id string,t Template) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State!=Preparing || !t.Enabled || !Compatible(t,p.pin) {return refuse(5,"template restore disabled or invalid")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.Template=t;e.Source=SourceTemplate;e.State=Warm
    return p.bump()
}

// Claim is exclusive, version-fenced and never launches on a cache miss.
func (p *Pool) Claim(id string,ver uint64) (Lease,error) {
    p.mu.Lock();defer p.mu.Unlock()
    if ver!=p.Version {return Lease{},refuse(5,"stale claim version")}
    e,err:=p.entry(id);if err!=nil{return Lease{},err}
    if e.State!=Warm || !Compatible(e.Template,p.pin) {return Lease{},refuse(5,"entry not warm or incompatible")}
    if e.Source!=SourceCold && !(e.Source==SourceTemplate && e.Template.Enabled) {
        return Lease{},refuse(5,"template-restored entry disabled")
    }
    if p.Version==math.MaxUint64 || e.Generation==math.MaxUint64{return Lease{},refuse(5,"generation or version exhausted")}
    e.Generation++;e.State=Claimed;e.ClaimedAt=p.clock.Now()
    _=p.bump()
    return Lease{ID:id,Generation:e.Generation,Version:p.Version,ClaimedAt:e.ClaimedAt},nil
}

func (p *Pool) MarkDirty(id string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State!=Claimed {return refuse(5,"only claimed entries become dirty")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.State=Dirty;return p.bump()
}

// The following steps enforce fence -> collect -> stop -> purge -> verify.
func (p *Pool) Fence(id string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if (e.State!=Claimed && e.State!=Dirty) || e.Stage!=StepNone {return refuse(5,"fence requires an unfinished lease")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.State=Draining;e.Stage=StepFenced;return p.bump()
}
func (p *Pool) Collect(id string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State!=Draining || e.Stage!=StepFenced {return refuse(5,"collect requires fence")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.Stage=StepCollected;return p.bump()
}
func (p *Pool) Stop(id string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State!=Draining || e.Stage!=StepCollected {return refuse(5,"stop requires collect")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.State=Cleaning;e.Stage=StepStopped;return p.bump()
}
func (p *Pool) Purge(id string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State!=Cleaning || e.Stage!=StepStopped {return refuse(5,"purge requires stop")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.Stage=StepPurged;return p.bump()
}

// Verify delegates only proof creation to Cleaner. Ambiguous cleanup is
// quarantined and remains charged, never available for replenishment.
func (p *Pool) Verify(id string) (CleanupProof,error) {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return nil,err}
    if e.State!=Cleaning || e.Stage!=StepPurged {return nil,refuse(5,"verify requires purge")}
    if p.Version==math.MaxUint64{return nil,refuse(5,"version exhausted")}
    proof,cleanErr:=p.cleaner.Clean(id,e.Generation)
    if cleanErr!=nil || proof==nil || !proof.Verified || proof.EntryID!=id || proof.Generation!=e.Generation {
        e.State=Quarantined;_ = p.bump()
        if cleanErr!=nil {return nil,fmt.Errorf("cleanup indeterminate: %w",cleanErr)}
        return nil,refuse(6,"cleanup indeterminate; hold quarantined")
    }
    e.Stage=StepVerified;_ = p.bump()
    return proof,nil
}

// Release requires full ordered cleanup and an exact verified witness.
// Only a retry with the SAME idempotency key is a no-op.
func (p *Pool) Release(id string,proof CleanupProof,key string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if key=="" {return refuse(2,"release key required")}
    if e.State==Released {
        if e.ReleaseKey==key {return nil}
        return refuse(5,"already released with another key")
    }
    if proof==nil || !proof.Verified || proof.EntryID!=id || proof.Generation!=e.Generation {
        return refuse(5,"missing or wrong cleanup witness")
    }
    if e.Stage!=StepVerified && !(e.State==Quarantined && e.Stage==StepPurged) {
        return refuse(5,"cleanup sequence incomplete or lease unfenced")
    }
    if e.State!=Cleaning && e.State!=Quarantined {return refuse(5,"entry not releasable")}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    if err:=p.governor.Release(id,e.Resources);err!=nil {return fmt.Errorf("governor release: %w",err)}
    p.holds=subtract(p.holds,e.Resources)
    e.State=Released;e.ReleaseKey=key
    return p.bump()
}
func (p *Pool) Quarantine(id string) error {
    p.mu.Lock();defer p.mu.Unlock()
    e,err:=p.entry(id);if err!=nil{return err}
    if e.State==Released {return refuse(5,"cannot quarantine released entry")}
    if e.State==Quarantined {return nil}
    if p.Version==math.MaxUint64{return refuse(5,"version exhausted")}
    e.State=Quarantined;return p.bump()
}

// Snapshot is an immutable point-in-time copy; no live pointers escape.
func (p *Pool) Snapshot() PoolSnapshot {
    p.mu.Lock();defer p.mu.Unlock()
    s:=PoolSnapshot{Cap:p.Cap,Holds:p.holds,Version:p.Version,Entries:make([]EntrySnapshot,0,len(p.entries))}
    for _,e:=range p.entries {s.Entries=append(s.Entries,*e)}
    sort.Slice(s.Entries,func(i,j int)bool{return s.Entries[i].ID<s.Entries[j].ID})
    return s
}

func validState(s EntryState) bool {
    switch s {case Preparing,Warm,Claimed,Draining,Dirty,Cleaning,Released,Quarantined:return true}
    return false
}
// Restore validates a journal snapshot before changing anything. A fresh pool
// may restore any version; an active pool cannot roll backwards or jump ahead.
// The governor's durable holds must be reconciled independently by its adapter.
func (p *Pool) Restore(s PoolSnapshot) error {
    p.mu.Lock();defer p.mu.Unlock()
    if s.Cap!=p.Cap{return refuse(5,"capacity differs from journal")}
    if p.Version!=0 && p.Version!=s.Version{return refuse(5,"snapshot version mismatch")}
    if p.Version==0 && len(p.entries)!=0{return refuse(5,"nonempty fresh state")}
    if s.Version==0 && len(s.Entries)!=0{return refuse(5,"unversioned entries")}
    next:=make(map[string]*EntrySnapshot,len(s.Entries))
    var held Vec
    for _,entry:=range s.Entries {
        if entry.ID=="" || entry.Generation==0 || !validState(entry.State) {return refuse(5,"malformed snapshot entry")}
        if _,exists:=next[entry.ID];exists{return refuse(5,"duplicate snapshot id")}
        if entry.State==Warm && (entry.Source!=SourceCold && entry.Source!=SourceTemplate || !Compatible(entry.Template,p.pin)) {
            return refuse(5,"invalid warm snapshot")
        }
        if entry.State==Released {
            if entry.ReleaseKey=="" {return refuse(5,"released tombstone without key")}
        } else {
            var ok bool
            held,ok=add(held,entry.Resources)
            if !ok || !fits(held,p.Cap){return refuse(5,"snapshot exceeds capacity")}
        }
        copy:=entry;next[entry.ID]=&copy
    }
    if held!=s.Holds{return refuse(5,"journal holds mismatch")}
    p.entries=next;p.holds=held;p.Version=s.Version
    return nil
}

// Holds is a synchronized read of all charged capacity, including quarantine.
func (p *Pool) Holds() Vec {p.mu.Lock();defer p.mu.Unlock();return p.holds}

// CodeOf is suitable for the W8 CLI adapter; wrapping preserves the error code.
func CodeOf(err error) int {
    if err==nil{return 0}
    var f *Fault
    if errors.As(err,&f){return f.RC}
    return 3
}
