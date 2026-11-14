// Package uidmapcapture evaluates caller-owned, captured identity evidence.
// It is deliberately offline: it does not acquire evidence from the host.
package uidmapcapture

import (
 "crypto/sha256"
 "encoding/hex"
 "errors"
 "fmt"
 "sort"
 "strconv"
 "strings"
)

const (
 MaxCaptureBytes = 128 << 10
 MaxLineBytes = 4096
 MaxMapEntries = 64
 MaxRecordLines = 256
 idLimit = uint64(1) << 32
)

var ErrAbsent = errors.New("AUSENTE: required capture missing")

// IDRange is an observed half-open mapping [Inside, Inside+Length) to Outside.
// Its original order is preserved, including maps with multiple entries.
type IDRange struct {
 Inside uint32 `json:"inside"`
 Outside uint32 `json:"outside"`
 Length uint32 `json:"length"`
}
type HostRange struct {
 Start uint32 `json:"start"`
 Length uint32 `json:"length"`
}
type SubIDRange struct {
 Owner string `json:"owner"`
 Start uint32 `json:"start"`
 Length uint32 `json:"length"`
}
type Account struct {
 Name string `json:"name"`
 UID uint32 `json:"uid"`
 GID uint32 `json:"gid"`
 Shell string `json:"shell"`
}
type Group struct {
 Name string `json:"name"`
 GID uint32 `json:"gid"`
 Members []string `json:"members"`
}

// Capture is passive text bound by the caller to a specific owned process.
// ProcessStart is an opaque caller-verified process-start identity, not a PID.
// Passwd and Group must contain non-secret (placeholder-password) records only.
type Capture struct {
 Owner, ExpectedOwner string
 ProcessStart, ExpectedProcessStart string
 UIDMap, GIDMap string
 Passwd, Group string
 SubUID, SubGID string
}
type Policy struct {
 ExpectedAccount string
 ApprovedUIDRanges []HostRange
 ApprovedGIDRanges []HostRange
 ContainerUID, ContainerGID *uint32
 ForbidContainerRoot bool
 SupplementalGroupsSupplied bool
 ObservedSupplementalGIDs []uint32
 AllowedSupplementalGIDs []uint32
 DeviceGroupMembershipSupplied bool
 ObservedDeviceGroupGIDs []uint32
 ApprovedDeviceGroupGIDs []uint32
}
type RawHashes struct {
 UIDMap string `json:"uid_map"`
 GIDMap string `json:"gid_map"`
 Passwd string `json:"passwd"`
 Group string `json:"group"`
 SubUID string `json:"subuid"`
 SubGID string `json:"subgid"`
}

// Missing names unknown inputs; failures name present but invalid evidence.
// Nil host identities are AUSENTE and must never be interpreted as zero.
type Evidence struct {
 Decision string `json:"decision"`
 UIDMap []IDRange `json:"uid_map"`
 GIDMap []IDRange `json:"gid_map"`
 RawSHA256 RawHashes `json:"raw_sha256"`
 Missing []string `json:"missing"`
 Failures []string `json:"failures"`
 HostUID *uint32 `json:"host_uid"`
 HostGID *uint32 `json:"host_gid"`
 Account string `json:"account"`
}

func decimal(s string) (uint32, error) {
 if len(s)==0 || len(s)>10 { return 0, errors.New("invalid decimal") }
 for _,c:=range s { if c<'0' || c>'9' { return 0, errors.New("invalid decimal") } }
 n,err:=strconv.ParseUint(s,10,32)
 return uint32(n),err
}

func boundedLines(raw string, maximum int) ([]string,error) {
 if raw=="" { return nil,ErrAbsent }
 if len(raw)>MaxCaptureBytes || strings.ContainsRune(raw,'\x00') { return nil,errors.New("capture size or control-byte limit") }
 lines:=strings.Split(raw,"\n")
 out:=make([]string,0,len(lines))
 for _,line:=range lines {
  line=strings.TrimSuffix(line,"\r")
  if len(line)>MaxLineBytes { return nil,errors.New("line too long") }
  if strings.TrimSpace(line)=="" { continue }
  out=append(out,line)
  if len(out)>maximum { return nil,errors.New("too many records") }
 }
 if len(out)==0 { return nil,ErrAbsent }
 return out,nil
}
func checkedEnd(start,length uint32)(uint64,error){
 end:=uint64(start)+uint64(length)
 if length==0 || end>idLimit { return 0,errors.New("zero or overflowing range") }
 return end,nil
}
func overlaps(a,b,al,bl uint32)bool {
 return uint64(a)<uint64(b)+uint64(bl) && uint64(b)<uint64(a)+uint64(al)
}

// ParseMap parses the exact observed uid_map/gid_map format without fabricating
// a single-entry mapping. Both inside and outside ranges must be disjoint.
func ParseMap(raw string)([]IDRange,error){
 lines,err:=boundedLines(raw,MaxMapEntries);if err!=nil { return nil,err }
 out:=make([]IDRange,0,len(lines))
 for _,line:=range lines {
  fields:=strings.Fields(line)
  if len(fields)!=3 { return nil,errors.New("map requires three integers") }
  a,e1:=decimal(fields[0]);b,e2:=decimal(fields[1]);n,e3:=decimal(fields[2])
  if e1!=nil || e2!=nil || e3!=nil { return nil,errors.New("invalid map integer") }
  if _,err=checkedEnd(a,n);err!=nil { return nil,err }
  if _,err=checkedEnd(b,n);err!=nil { return nil,err }
  for _,existing:=range out {
   if overlaps(a,existing.Inside,n,existing.Length)||overlaps(b,existing.Outside,n,existing.Length) {
    return nil,errors.New("overlapping observed mapping")
   }
  }
  out=append(out,IDRange{a,b,n})
 }
 return out,nil
}

// Translate returns false if an inside ID was not observed; it never guesses.
func Translate(ranges []IDRange,inside uint32)(uint32,bool){
 for _,r:=range ranges {
  if uint64(inside)>=uint64(r.Inside)&&uint64(inside)<uint64(r.Inside)+uint64(r.Length) {
   return uint32(uint64(r.Outside)+uint64(inside-r.Inside)),true
  }
 }
 return 0,false
}
func validName(s string)bool{
 if s==""||len(s)>128 { return false }
 for _,c:=range s {
  if !((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='_'||c=='-'||c=='.'){ return false }
 }
 return true
}
func placeholder(s string)bool {return s=="x"||s=="*"||s=="!"}

// ParseAccounts never returns password, GECOS or home fields.
func ParseAccounts(raw string)([]Account,error){
 lines,err:=boundedLines(raw,MaxRecordLines);if err!=nil{return nil,err}
 out:=make([]Account,0,len(lines))
 seenName,seenID:=map[string]bool{},map[uint32]bool{}
 for _,line:=range lines {
  f:=strings.Split(line,":")
  if len(f)!=7||!validName(f[0])||!placeholder(f[1])||f[6]=="" {return nil,errors.New("invalid or non-placeholder account record")}
  uid,e1:=decimal(f[2]);gid,e2:=decimal(f[3])
  if e1!=nil||e2!=nil||seenName[f[0]]||seenID[uid]{return nil,errors.New("invalid or duplicate account identity")}
  seenName[f[0]],seenID[uid]=true,true
  out=append(out,Account{Name:f[0],UID:uid,GID:gid,Shell:f[6]})
 }
 return out,nil
}
// ParseGroups treats password columns as placeholders, never as credentials.
func ParseGroups(raw string)([]Group,error){
 lines,err:=boundedLines(raw,MaxRecordLines);if err!=nil{return nil,err}
 out:=make([]Group,0,len(lines))
 seenName,seenID:=map[string]bool{},map[uint32]bool{}
 for _,line:=range lines {
  f:=strings.Split(line,":")
  if len(f)!=4||!validName(f[0])||!placeholder(f[1]){return nil,errors.New("invalid or non-placeholder group record")}
  gid,e:=decimal(f[2]);if e!=nil||seenName[f[0]]||seenID[gid]{return nil,errors.New("invalid or duplicate group identity")}
  seenName[f[0]],seenID[gid]=true,true
  members:=[]string{}
  if f[3]!=""{
   members=strings.Split(f[3],",")
   seen:=map[string]bool{}
   for _,member:=range members{
    if !validName(member)||seen[member]{return nil,errors.New("invalid group membership record")}
    seen[member]=true
   }
  }
  out=append(out,Group{Name:f[0],GID:gid,Members:members})
 }
 return out,nil
}
// ParseSubIDs parses owner:start:length grant lines; it does not read /etc.
func ParseSubIDs(raw string)([]SubIDRange,error){
 lines,err:=boundedLines(raw,MaxRecordLines);if err!=nil{return nil,err}
 out:=make([]SubIDRange,0,len(lines))
 for _,line:=range lines{
  f:=strings.Split(line,":")
  if len(f)!=3||!validName(f[0]){return nil,errors.New("invalid subid owner or fields")}
  start,e1:=decimal(f[1]);n,e2:=decimal(f[2])
  if e1!=nil||e2!=nil{return nil,errors.New("invalid subid integer")}
  if _,err:=checkedEnd(start,n);err!=nil{return nil,err}
  for _,previous:=range out{
   if previous.Owner==f[0]&&overlaps(start,previous.Start,n,previous.Length){return nil,errors.New("overlapping subid grants")}
  }
  out=append(out,SubIDRange{Owner:f[0],Start:start,Length:n})
 }
 return out,nil
}
func covered(start,length uint32,intervals []HostRange)bool{
 if _,err:=checkedEnd(start,length);err!=nil{return false}
 sorted:=append([]HostRange(nil),intervals...)
 sort.Slice(sorted,func(i,j int)bool{return sorted[i].Start<sorted[j].Start})
 end,cursor:=uint64(start)+uint64(length),uint64(start)
 for _,r:=range sorted{
  e,err:=checkedEnd(r.Start,r.Length);if err!=nil{return false}
  if uint64(r.Start)>cursor{break}
  if e>cursor{cursor=e}
  if cursor>=end{return true}
 }
 return false
}
func equalInsideCoverage(a,b []IDRange)bool{
 type span struct{start,length uint64}
 normalize:=func(in []IDRange)[]span{
  intervals:=make([]span,0,len(in))
  for _,r:=range in{intervals=append(intervals,span{uint64(r.Inside),uint64(r.Length)})}
  sort.Slice(intervals,func(i,j int)bool{return intervals[i].start<intervals[j].start})
  out:=[]span{}
  for _,v:=range intervals{
   if len(out)>0&&out[len(out)-1].start+out[len(out)-1].length==v.start{out[len(out)-1].length+=v.length}else{out=append(out,v)}
  }
  return out
 }
 x,y:=normalize(a),normalize(b)
 if len(x)!=len(y){return false}
 for i:=range x{if x[i]!=y[i]{return false}}
 return true
}
func hashText(s string)string{sum:=sha256.Sum256([]byte(s));return hex.EncodeToString(sum[:])}
func includes(a []uint32,v uint32)bool{for _,x:=range a{if x==v{return true}};return false}
func includesMember(a []string,v string)bool{for _,x:=range a{if x==v{return true}};return false}
func nonLogin(s string)bool{return strings.HasSuffix(s,"/nologin")||strings.HasSuffix(s,"/false")}

// Evaluate has no side effects. Every absent, unbound or contradictory fact HOLDs.
// Policy and Capture are supplied evidence, not independently measured facts.
func Evaluate(c Capture,p Policy)Evidence{
 e:=Evidence{Decision:"HOLD",UIDMap:[]IDRange{},GIDMap:[]IDRange{},Missing:[]string{},Failures:[]string{}}
 missing:=func(name string){e.Missing=append(e.Missing,name)}
 fail:=func(reason string){e.Failures=append(e.Failures,reason)}
 if c.Owner==""{missing("capture_owner")};if c.ExpectedOwner==""{missing("expected_owner")}
 if c.Owner!=""&&c.ExpectedOwner!=""&&c.Owner!=c.ExpectedOwner{fail("capture_owner_mismatch")}
 if c.ProcessStart==""{missing("process_start")};if c.ExpectedProcessStart==""{missing("expected_process_start")}
 if c.ProcessStart!=""&&c.ExpectedProcessStart!=""&&c.ProcessStart!=c.ExpectedProcessStart{fail("process_start_mismatch")}
 if p.ExpectedAccount==""{missing("expected_account")}
 if len(p.ApprovedUIDRanges)==0{missing("approved_uid_ranges")}
 if len(p.ApprovedGIDRanges)==0{missing("approved_gid_ranges")}
 if p.ContainerUID==nil{missing("container_uid")}
 if p.ContainerGID==nil{missing("container_gid")}
 if !p.SupplementalGroupsSupplied{missing("supplemental_group_evidence")}
 if !p.DeviceGroupMembershipSupplied{missing("device_group_evidence")}
 for _,item:=range []struct{name,raw string;target *string}{
  {"uid_map",c.UIDMap,&e.RawSHA256.UIDMap},{"gid_map",c.GIDMap,&e.RawSHA256.GIDMap},
  {"passwd",c.Passwd,&e.RawSHA256.Passwd},{"group",c.Group,&e.RawSHA256.Group},
  {"subuid",c.SubUID,&e.RawSHA256.SubUID},{"subgid",c.SubGID,&e.RawSHA256.SubGID},
 }{if item.raw==""{missing(item.name)}else{*item.target=hashText(item.raw)}}
 var uid,gid []IDRange
 if c.UIDMap!=""{m,err:=ParseMap(c.UIDMap);if err!=nil{fail("uid_map_invalid")}else{uid=m;e.UIDMap=m}}
 if c.GIDMap!=""{m,err:=ParseMap(c.GIDMap);if err!=nil{fail("gid_map_invalid")}else{gid=m;e.GIDMap=m}}
 if uid!=nil&&gid!=nil&&!equalInsideCoverage(uid,gid){fail("uid_gid_container_coverage_mismatch")}
 var accounts []Account
 if c.Passwd!=""{var err error;accounts,err=ParseAccounts(c.Passwd);if err!=nil{fail("passwd_invalid")}}
 var groups []Group
 if c.Group!=""{var err error;groups,err=ParseGroups(c.Group);if err!=nil{fail("group_invalid")}}
 var su,sg []SubIDRange
 if c.SubUID!=""{var err error;su,err=ParseSubIDs(c.SubUID);if err!=nil{fail("subuid_invalid")}}
 if c.SubGID!=""{var err error;sg,err=ParseSubIDs(c.SubGID);if err!=nil{fail("subgid_invalid")}}
 var account *Account
 for i:=range accounts{if accounts[i].Name==p.ExpectedAccount{account=&accounts[i];break}}
 if len(accounts)>0&&p.ExpectedAccount!=""&&account==nil{fail("expected_account_not_found")}
 if account!=nil{
  e.Account=account.Name
  if account.UID==0||account.GID==0{fail("dedicated_account_is_root")}
  if !nonLogin(account.Shell){fail("dedicated_account_has_login_shell")}
 }
 var uidValue,gidValue *uint32
 if uid!=nil&&p.ContainerUID!=nil{
  v,ok:=Translate(uid,*p.ContainerUID);if !ok{fail("uid_unmapped")}else{uidValue=&v;e.HostUID=&v}
 }
 if gid!=nil&&p.ContainerGID!=nil{
  v,ok:=Translate(gid,*p.ContainerGID);if !ok{fail("gid_unmapped")}else{gidValue=&v;e.HostGID=&v}
 }
 if p.ForbidContainerRoot&&p.ContainerUID!=nil&&*p.ContainerUID==0{fail("container_root_forbidden")}
 if uidValue!=nil&&*uidValue==0{fail("host_root_uid_forbidden")}
 if gidValue!=nil&&*gidValue==0{fail("host_root_gid_forbidden")}
 if account!=nil&&uidValue!=nil&&*uidValue!=account.UID{fail("uid_account_mismatch")}
 if account!=nil&&gidValue!=nil&&*gidValue!=account.GID{fail("gid_account_mismatch")}
 for _,pair:=range []struct{kind string;mapping []IDRange;approved []HostRange;grants []SubIDRange;direct uint32}{
  {"uid",uid,p.ApprovedUIDRanges,su,func()uint32{if account!=nil{return account.UID};return 0}()},
  {"gid",gid,p.ApprovedGIDRanges,sg,func()uint32{if account!=nil{return account.GID};return 0}()},
 }{
  for _,m:=range pair.mapping{
   if len(pair.approved)>0&&!covered(m.Outside,m.Length,pair.approved){fail(pair.kind+"_host_range_unapproved")}
   if account!=nil&&len(pair.grants)>0&&!(m.Length==1&&m.Outside==pair.direct){
    ownerRanges:=[]HostRange{}
    for _,grant:=range pair.grants{if grant.Owner==p.ExpectedAccount{ownerRanges=append(ownerRanges,HostRange{grant.Start,grant.Length})}}
    if !covered(m.Outside,m.Length,ownerRanges){fail(pair.kind+"_subid_range_missing")}
   }
  }
 }
 if p.SupplementalGroupsSupplied&&account!=nil&&groups!=nil{
  for _,observed:=range p.ObservedSupplementalGIDs{
   if !includes(p.AllowedSupplementalGIDs,observed){fail("unexpected_supplemental_group");continue}
   found:=false
   for _,group:=range groups{
    if group.GID==observed{
     found=true
     if !includesMember(group.Members,p.ExpectedAccount){fail("supplemental_group_member_missing")}
     if group.Name=="kvm"||group.Name=="render"||group.Name=="video"||group.Name=="input"||group.Name=="disk"||group.Name=="docker"{
      if !p.DeviceGroupMembershipSupplied||!includes(p.ObservedDeviceGroupGIDs,observed)||!includes(p.ApprovedDeviceGroupGIDs,observed){fail("device_group_not_authorized")}
     }
    }
   }
   if !found{fail("supplemental_group_record_missing")}
  }
  for _,device:=range p.ObservedDeviceGroupGIDs{
   if !includes(p.ObservedSupplementalGIDs,device)||!includes(p.ApprovedDeviceGroupGIDs,device){fail("unexpected_device_group")}
  }
 }
 sort.Strings(e.Missing);sort.Strings(e.Failures)
 if len(e.Missing)==0&&len(e.Failures)==0{e.Decision="PASS"}
 return e
}

// RejectUnknown makes the HOLD contract explicit for consuming callers.
func(e Evidence)RejectUnknown()error{
 if e.Decision=="PASS"{return nil}
 return fmt.Errorf("HOLD: AUSENTE or invalid identity evidence (%d missing, %d failures)",len(e.Missing),len(e.Failures))
}
