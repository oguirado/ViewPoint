package harvest

import (
    "archive/tar"
    "bytes"
    "encoding/json"
    "strings"
    "testing"

    "opensuperficies.local/srf/src/workerbridge"
)

// Synthetic reference adapted from the merged testdata/workerbridge/codec.json.
// All fixture quantities are TARGET values; this fixture starts no runtime.
const receiptFixtureJSON = "{\"grant\":{\"attempt_id\":\"a1\",\"collect_until_ms\":1960000,\"expires_at_ms\":1900000,\"isolation\":{\"cap_drop\":[\"ALL\"],\"core_dumps_enabled\":false,\"frames_root\":\"/dev/shm/worker-lease-example/frames\",\"hardening\":{\"bind_mounts\":[],\"daemon\":\"rootless\",\"devices\":[],\"egress\":{\"mode\":\"none\"},\"env_file\":null,\"env_names\":[\"LC_ALL\",\"TINI_SUBREAPER\",\"GIT_TERMINAL_PROMPT\"],\"exec_allowlist_sha256\":\"4444444444444444444444444444444444444444444444444444444444444444\",\"init\":\"tini\",\"interactive_cpus\":[0,1,2,3,4,5,6,7,8,9],\"memswap_equals_memory\":true,\"nofile\":8192,\"oom_score_adj\":800,\"read_only\":true,\"runtime_socket_mounted\":false},\"no_host_mounts\":true,\"no_new_privileges\":true,\"no_runtime_socket\":true,\"rootfs_read_only\":true,\"seccomp_profile_sha256\":\"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\",\"swap_enabled\":false,\"uid\":10001,\"workspace_root\":\"/workspace/repo\"},\"issued_at_ms\":1000000,\"lease_id\":\"lease-0123456789abcdef\",\"lease_nonce\":\"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\",\"limits\":{\"cpuset\":[10,11],\"memory_bytes\":268435456,\"pids\":64,\"ttl_ms\":900000,\"workspace_bytes\":134217728},\"network\":{\"allowlist\":[],\"mode\":\"none\"},\"repo_commit\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"runtime\":{\"backend\":\"docker\",\"base_image\":{\"approval_sha256\":\"6666666666666666666666666666666666666666666666666666666666666666\",\"digest\":\"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\",\"name\":\"os-worker-base\",\"recipe_sha256\":\"9999999999999999999999999999999999999999999999999999999999999999\"},\"commit\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"extract\":\"tar-exec\",\"inference\":\"host\",\"profile\":\"worker\",\"vmm\":null,\"workspace\":{\"cache\":{\"manifest_sha256\":\"5555555555555555555555555555555555555555555555555555555555555555\",\"read_only\":true,\"target\":\"/opt/cache\",\"volume\":\"os_worker_cache\"},\"ingest\":\"git-bundle-over-exec\",\"kind\":\"tmpfs\",\"options\":\"rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001\",\"purge\":\"task_end\",\"root\":\"/workspace\",\"size_bytes\":134217728}},\"schema\":\"os.cu.worker-seat/v1\",\"seat_id\":\"seat-example\",\"task_id\":\"os-w8-fixture\",\"task_sha256\":\"ba3d80a3bbbb044a5d93aa21248d6799acda7e88a0b47757a0ddd0eb5abddd38\"},\"receipt\":{\"attempt_id\":\"a1\",\"cleanup\":{\"remaining_pids\":0,\"remaining_workspace_bytes\":0,\"resources_released\":true,\"status\":\"verified\"},\"evidence\":{\"answers\":[{\"answer\":\"b2c70bba7bf0ef43959341e03b923efd23fed1a0b3ed1895736736d88a5b39b0\",\"request_id\":\"e0\"},{\"answer\":\"# W4\",\"request_id\":\"e1\"},{\"answer\":\"# W4 wave\",\"request_id\":\"e2\"},{\"answer\":\"gate rc 0\",\"request_id\":\"e3\"}],\"notes\":\"Synthetic fixture; no live runtime was started.\"},\"exit_code\":0,\"extract\":{\"bytes\":4096,\"entries\":2,\"method\":\"tar-exec\",\"mode\":\"0644\",\"owner_asserted\":true,\"sha256\":\"2222222222222222222222222222222222222222222222222222222222222222\",\"staging_residue\":0,\"text_only\":true},\"failure_reason\":null,\"finished_at_ms\":1000200,\"gate\":{\"argv\":[\"/bin/sh\",\"-ec\",\"go test ./...\"],\"cwd\":\".\",\"duration_ms\":50,\"exit_code\":0,\"raw_exit_code\":0,\"stderr\":{\"retained_bytes\":0,\"sha256\":null,\"total_bytes\":0,\"truncated\":false},\"stdout\":{\"retained_bytes\":0,\"sha256\":null,\"total_bytes\":0,\"truncated\":false},\"terminated_by\":null,\"timeout_ms\":10000},\"host_metric\":{\"name\":\"elapsed_ms\",\"provenance\":\"unavailable\",\"sample_count\":0,\"unit\":\"ms\",\"value\":null},\"lease_id\":\"lease-0123456789abcdef\",\"lease_nonce\":\"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\",\"observations\":{\"cap_drop\":[\"ALL\"],\"capsule_id\":\"capsule-example\",\"core_dumps_enabled\":false,\"cpuset\":[10,11],\"denied_network_attempts\":0,\"frames_disk_bytes\":0,\"frames_root\":\"/dev/shm/worker-lease-example/frames\",\"memory_limit_bytes\":268435456,\"network\":{\"allowlist\":[],\"mode\":\"none\"},\"no_host_mounts\":true,\"no_new_privileges\":true,\"no_runtime_socket\":true,\"peak_memory_bytes\":4096,\"peak_pids\":2,\"pids_limit\":64,\"probes\":{\"bind_mounts\":0,\"daemon\":\"rootless\",\"devices\":[],\"egress_probe\":\"BLOCKED\",\"env_file_used\":false,\"env_secret_names\":0,\"etc_write\":\"read_only\",\"home_admin\":\"absent\",\"inference_processes\":0,\"inspect_sha256\":\"3333333333333333333333333333333333333333333333333333333333333333\",\"memswap_equals_memory\":true,\"nofile\":8192,\"oom_score_adj\":800,\"orphans_after_task\":0,\"runtime_socket_visible\":false},\"public_listeners\":0,\"rootfs_read_only\":true,\"seccomp_profile_sha256\":\"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\",\"swap_enabled\":false,\"ttl_enforced\":true,\"uid\":10001,\"workspace_limit_bytes\":134217728,\"workspace_peak_bytes\":2048,\"workspace_root\":\"/workspace/repo\",\"x11_listeners\":0,\"xpra\":\"off\"},\"output\":{\"base_commit\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"branch\":\"bridge/task-example/a1\",\"files\":[{\"bytes\":17,\"mode\":\"100644\",\"operation\":\"modify\",\"path\":\"src/work.go\",\"sha256\":\"0c5cd5c85fd0b8947903d56a38e40920e023454470bd519f5f0fc5f9efbf3398\"}],\"head_commit\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"kind\":\"branch\",\"repo_url\":\"https://github.com/team/project\"},\"repo\":{\"commit\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"url\":\"https://github.com/team/project\"},\"runtime\":{\"backend\":\"docker\",\"base_image\":{\"approval_sha256\":\"6666666666666666666666666666666666666666666666666666666666666666\",\"digest\":\"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\",\"name\":\"os-worker-base\",\"recipe_sha256\":\"9999999999999999999999999999999999999999999999999999999999999999\"},\"commit\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"extract\":\"tar-exec\",\"inference\":\"host\",\"profile\":\"worker\",\"vmm\":null,\"workspace\":{\"cache\":{\"manifest_sha256\":\"5555555555555555555555555555555555555555555555555555555555555555\",\"read_only\":true,\"target\":\"/opt/cache\",\"volume\":\"os_worker_cache\"},\"ingest\":\"git-bundle-over-exec\",\"kind\":\"tmpfs\",\"options\":\"rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001\",\"purge\":\"task_end\",\"root\":\"/workspace\",\"size_bytes\":134217728}},\"schema\":\"os.cu.worker-receipt/v1\",\"seat_grant_sha256\":\"0771dffcbc855d5b0008890a7d289bd34280695966f9696c773b8f85a16194bb\",\"seat_id\":\"seat-example\",\"sources_observed\":[{\"path\":\"src/work.go\",\"sha256\":\"b2c70bba7bf0ef43959341e03b923efd23fed1a0b3ed1895736736d88a5b39b0\"},{\"path\":\"docs/W4.md\",\"sha256\":\"b2c70bba7bf0ef43959341e03b923efd23fed1a0b3ed1895736736d88a5b39b0\"}],\"started_at_ms\":1000100,\"task_id\":\"os-w8-fixture\",\"task_sha256\":\"ba3d80a3bbbb044a5d93aa21248d6799acda7e88a0b47757a0ddd0eb5abddd38\"}}"

func receiptWire(t testing.TB, v any) []byte {
    t.Helper()
    b, err := workerbridge.Canonical(v)
    if err != nil { t.Fatal(err) }
    return b
}
func receiptDocument(t testing.TB, b []byte) workerbridge.Document {
    t.Helper()
    d, err := workerbridge.Parse(b)
    if err != nil { t.Fatal(err) }
    return d
}
func receiptTestWave(t testing.TB) FrozenWave {
    t.Helper()
    f, err := Freeze(Wave{
        WaveID:"wave-receipt", Repo:"example/repo",
        BaseCommit:strings.Repeat("a",40), Objective:"verify a bounded harvest",
        GateArgv:[]string{"go","test","./..."}, InputSHA256:strings.Repeat("d",64),
        ResourcePolicySHA256:strings.Repeat("e",64), Obligations:[]Obligation{{ID:"o",Weight:1}},
    })
    if err != nil { t.Fatal(err) }
    return f
}
func receiptTar(t testing.TB, files map[string][]byte) []byte {
    t.Helper()
    var buf bytes.Buffer
    tw := tar.NewWriter(&buf)
    // Synthetic fixture has one file; explicit order avoids map-order evidence.
    data := files["report.txt"]
    if err := tw.WriteHeader(&tar.Header{Name:"report.txt", Mode:0644, Size:int64(len(data)), Typeflag:tar.TypeReg}); err != nil { t.Fatal(err) }
    if _, err := tw.Write(data); err != nil { t.Fatal(err) }
    if err := tw.Close(); err != nil { t.Fatal(err) }
    return buf.Bytes()
}

// safeFixture is shared by the package's orchestration tests. It returns an
// assigned Muse key, matching host proof, and no repro observations by default.
func safeFixture(t testing.TB, f FrozenWave, obligation string, suffix string, rc int) (Candidate, Execution, Extraction) {
    t.Helper()
    base := receiptDocument(t, []byte(receiptFixtureJSON))
    grant := receiptObject(base["grant"])
    receipt := receiptObject(base["receipt"])
    a := Attempt{
        ObligationID:obligation, TaskID:"task-"+obligation, AttemptID:"attempt-"+suffix,
        LeaseID:"lease-"+suffix, Nonce:digest([]byte("nonce:"+obligation+":"+suffix)),
        InputSHA256:f.wave.InputSHA256, ResourcePolicySHA256:f.wave.ResourcePolicySHA256,
        Backend:ProcessBackend,
    }
    repo := workerbridge.Document{"url":waveRepositoryURL(f.wave.Repo), "commit":f.wave.BaseCommit}
    worker := workerbridge.Document{
        "schema":"os.cu.worker-task/v1", "task_id":a.TaskID, "attempt_id":a.AttemptID, "lease_id":a.LeaseID,
        "repo":repo, "objective":f.wave.Objective,
        "sources":[]any{workerbridge.Document{"path":"input.txt", "sha256":f.wave.InputSHA256}},
        "write_paths":[]any{"report.txt"},
        "gate":workerbridge.Document{"argv":f.wave.GateArgv, "cwd":".", "timeout_ms":10000},
        "evidence":workerbridge.Document{"requests":[]any{}, "max_bytes":102400},
        "consumer":workerbridge.Document{"name":"auto_ingest"},
        "host_metric":workerbridge.Document{"name":"elapsed_ms", "unit":"ms", "target_max":nil, "required":false},
    }
    a.WorkerTask = receiptWire(t, worker)
    candidate := Candidate{ID:"candidate-"+obligation+"-"+suffix, Attempt:a, Route:MuseSeatRoute}
    checked, err := validateCandidate(f, candidate)
    if err != nil { t.Fatal(err) }
    candidate.Attempt = checked
    a = checked
    for _, d := range []workerbridge.Document{grant,receipt} {
        d["task_id"] = a.TaskID
        d["attempt_id"] = a.AttemptID
        d["lease_id"] = a.LeaseID
        d["lease_nonce"] = a.Nonce
        d["task_sha256"] = digest(a.WorkerTask)
    }
    grant["repo_commit"] = f.wave.BaseCommit
    runtime := receiptObject(grant["runtime"])
    runtime["commit"] = f.wave.BaseCommit
    receipt["runtime"] = runtime
    receipt["repo"] = repo
    receipt["seat_grant_sha256"] = digest(receiptWire(t,grant))
    gate := receiptObject(receipt["gate"])
    gate["argv"] = f.wave.GateArgv
    receipt["sources_observed"] = []any{workerbridge.Document{"path":"input.txt", "sha256":f.wave.InputSHA256}}
    receipt["evidence"] = workerbridge.Document{"answers":[]any{}, "notes":"Synthetic host receipt fixture; no live execution."}
    body := []byte("synthetic verified text\n")
    files := map[string][]byte{"report.txt":body}
    payload := receiptTar(t, files)
    extract := receiptObject(receipt["extract"])
    extract["entries"] = 1
    extract["bytes"] = len(payload)
    extract["sha256"] = digest(payload)
    receipt["output"] = workerbridge.Document{
        "kind":"branch", "repo_url":waveRepositoryURL(f.wave.Repo), "branch":"harvest/fixture",
        "base_commit":f.wave.BaseCommit, "head_commit":strings.Repeat("b",40),
        "files":[]any{workerbridge.Document{"path":"report.txt", "operation":"add", "mode":"100644", "bytes":len(body), "sha256":digest(body)}},
    }
    receipt["exit_code"] = rc
    if rc != 0 {
        reasons := map[int]string{2:"contract",3:"execution",4:"unavailable",5:"gate",6:"indeterminate",7:"operator_capability"}
        receipt["failure_reason"] = reasons[rc]
        receipt["output"] = nil
        gate["raw_exit_code"] = 1
        gate["exit_code"] = 5
        if rc != 5 { receipt["gate"] = nil }
    }
    cleanup := CleanupProof{AttemptKey:a.Key, LeaseID:a.LeaseID, Nonce:a.Nonce, Known:true, Verified:true}
    if rc == 6 {
        receipt["cleanup"] = workerbridge.Document{"status":"not_observed", "remaining_pids":nil, "remaining_workspace_bytes":nil, "resources_released":nil}
        cleanup.Known = false
        cleanup.Verified = false
    }
    grantBytes := receiptWire(t,grant)
    execution := Execution{Handle:a.Key, Accepted:true, Grant:grantBytes, Usage:MissingCost()}
    extraction := Extraction{
        AttemptKey:a.Key, Nonce:a.Nonce, HostValidated:true, A4Validated:true,
        Receipt:receiptWire(t,receipt), Grant:bytes.Clone(grantBytes), Payload:payload,
        Files:files, Cleanup:cleanup, Usage:MissingCost(),
    }
    for definition,d := range map[string]workerbridge.Document{"task":receiptDocument(t,a.WorkerTask),"seat_grant":receiptDocument(t,grantBytes),"receipt":receiptDocument(t,extraction.Receipt)} {
        if err = workerbridge.ValidateDef(d, definition); err != nil { t.Fatalf("%s fixture: %v", definition, err) }
    }
    return candidate, execution, extraction
}

func mutateReceipt(t testing.TB, x *Extraction, fn func(workerbridge.Document)) {
    t.Helper()
    d := receiptDocument(t,x.Receipt)
    fn(d)
    x.Receipt = receiptWire(t,d)
}
func mutateWorker(t testing.TB, c *Candidate, fn func(workerbridge.Document)) {
    t.Helper()
    d := receiptDocument(t,c.Attempt.WorkerTask)
    fn(d)
    c.Attempt.WorkerTask = receiptWire(t,d)
}
func mutateGrant(t testing.TB, execution *Execution, x *Extraction, fn func(workerbridge.Document)) {
    t.Helper()
    d := receiptDocument(t,x.Grant)
    fn(d)
    x.Grant = receiptWire(t,d)
    execution.Grant = bytes.Clone(x.Grant)
    mutateReceipt(t,x,func(r workerbridge.Document){r["seat_grant_sha256"] = digest(x.Grant)})
}

func TestReceiptCompleteSyntheticControl(t *testing.T) {
    f := receiptTestWave(t)
    c,e,x := safeFixture(t,f,"o","a1",0)
    if rc,err := validateExtraction(f,c.Attempt,e,x); err != nil || rc != 0 { t.Fatalf("matching host receipt: rc=%d err=%v",rc,err) }
}
func TestReceiptExplainedFailureIsNotSuccess(t *testing.T) {
    f := receiptTestWave(t)
    c,e,x := safeFixture(t,f,"o","a1",5)
    if rc,err := validateExtraction(f,c.Attempt,e,x); err != nil || rc != 5 { t.Fatalf("verified failed gate: rc=%d err=%v",rc,err) }
}
func TestCandidateRefusesUnsafeAdmission(t *testing.T) {
    cases := []struct{name string; mutate func(*Candidate)}{
        {"guest-inference",func(c *Candidate){c.GuestInference=true}},
        {"bind-mount",func(c *Candidate){c.BindMounts=[]string{"/host"}}},
        {"env-file",func(c *Candidate){c.EnvFile="environment.txt"}},
        {"runtime-socket",func(c *Candidate){c.RuntimeSocket=true}},
        {"direct-route",func(c *Candidate){c.Route="direct"}},
        {"KVM-on-process",func(c *Candidate){c.Attempt.RequiresKVM=true}},
        {"unknown-backend",func(c *Candidate){c.Attempt.Backend="unknown"}},
        {"missing-input",func(c *Candidate){c.Attempt.InputSHA256=""}},
        {"changed-input",func(c *Candidate){c.Attempt.InputSHA256=strings.Repeat("f",64)}},
        {"changed-policy",func(c *Candidate){c.Attempt.ResourcePolicySHA256=strings.Repeat("f",64)}},
        {"unknown-obligation",func(c *Candidate){c.Attempt.ObligationID="lost"}},
        {"wrong-key",func(c *Candidate){c.Attempt.Key=strings.Repeat("f",64)}},
        {"changed-commit",func(c *Candidate){mutateWorker(t,c,func(d workerbridge.Document){receiptObject(d["repo"])["commit"]=strings.Repeat("f",40)})}},
        {"changed-argv-order",func(c *Candidate){mutateWorker(t,c,func(d workerbridge.Document){receiptObject(d["gate"])["argv"]=[]any{"go","./...","test"}})}},
        {"changed-objective",func(c *Candidate){mutateWorker(t,c,func(d workerbridge.Document){d["objective"]="changed"})}},
    }
    f := receiptTestWave(t)
    for _,tc := range cases {
        t.Run(tc.name,func(t *testing.T){
            c,_,_ := safeFixture(t,f,"o","a1",0)
            tc.mutate(&c)
            if _,err := validateCandidate(f,c); err==nil || ExitCode(err)!=5 { t.Fatalf("unsafe candidate accepted: %v",err) }
        })
    }
}
func TestCandidateMuseKeyCannotChangeAcrossWaves(t *testing.T) {
    f := receiptTestWave(t)
    c,_,_ := safeFixture(t,f,"o","a1",0)
    next := f.Wave()
    next.WaveID = "next-wave"
    f2,err := Freeze(next)
    if err!=nil { t.Fatal(err) }
    a,err := validateCandidate(f2,c)
    if err!=nil || a.Key!=c.Attempt.Key { t.Fatalf("existing Muse identity changed: %v",err) }
}
func TestReceiptMustFailFixtures(t *testing.T) {
    cases := []struct{name string; code int; mutate func(*Execution,*Extraction)}{
        {"forged-done",5,func(_ *Execution,x *Extraction){x.Receipt=[]byte("{\"done\":true}")}},
        {"guest-receipt",5,func(_ *Execution,x *Extraction){x.HostValidated=false}},
        {"missing-A4",5,func(_ *Execution,x *Extraction){x.A4Validated=false}},
        {"wrong-attempt-key",5,func(_ *Execution,x *Extraction){x.AttemptKey="other"}},
        {"unknown-purge",6,func(_ *Execution,x *Extraction){x.Cleanup.Known=false}},
        {"failed-purge",6,func(_ *Execution,x *Extraction){x.Cleanup.Verified=false}},
        {"foreign-cleanup",5,func(_ *Execution,x *Extraction){x.Cleanup.LeaseID="other"}},
        {"wrong-task",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["task_id"]="other"})}},
        {"wrong-attempt",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["attempt_id"]="other"})}},
        {"wrong-lease",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["lease_id"]="other"})}},
        {"wrong-nonce",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["lease_nonce"]=strings.Repeat("f",64)})}},
        {"wrong-task-digest",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["task_sha256"]=strings.Repeat("f",64)})}},
        {"wrong-grant-digest",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["seat_grant_sha256"]=strings.Repeat("f",64)})}},
        {"receipt-unknown-purge",6,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(d["cleanup"])["status"]="not_observed"})}},
        {"receipt-failed-purge",6,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(d["cleanup"])["status"]="failed"})}},
        {"remaining-workspace",6,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(d["cleanup"])["remaining_workspace_bytes"]=1})}},
        {"changed-commit",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(d["repo"])["commit"]=strings.Repeat("f",40)})}},
        {"changed-gate",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(d["gate"])["argv"]=[]any{"go","./...","test"}})}},
        {"unsafe-probe",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(receiptObject(d["observations"])["probes"])["inference_processes"]=1})}},
        {"changed-source",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){receiptObject(receiptArray(d["sources_observed"])[0])["sha256"]=strings.Repeat("f",64)})}},
        {"changed-payload",5,func(_ *Execution,x *Extraction){x.Payload=append(x.Payload,1)}},
        {"changed-file-map",5,func(_ *Execution,x *Extraction){x.Files["report.txt"]=[]byte("forged text\n")}},
        {"unknown-receipt-key",5,func(_ *Execution,x *Extraction){mutateReceipt(t,x,func(d workerbridge.Document){d["done"]=true})}},
        {"interactive-cpu",5,func(e *Execution,x *Extraction){mutateGrant(t,e,x,func(d workerbridge.Document){receiptObject(d["limits"])["cpuset"]=[]any{0}})}},
        {"expired-grant",5,func(e *Execution,x *Extraction){mutateGrant(t,e,x,func(d workerbridge.Document){d["expires_at_ms"]=1000050})}},
        {"grant-observation-drift",5,func(e *Execution,x *Extraction){mutateGrant(t,e,x,func(d workerbridge.Document){receiptObject(receiptObject(d["isolation"])["hardening"])["nofile"]=4096})}},
    }
    f := receiptTestWave(t)
    for _,tc := range cases {
        t.Run(tc.name,func(t *testing.T){
            c,e,x := safeFixture(t,f,"o","a1",0)
            tc.mutate(&e,&x)
            rc,err := validateExtraction(f,c.Attempt,e,x)
            if err==nil || rc!=tc.code || ExitCode(err)!=tc.code { t.Fatalf("want refusal %d, got rc=%d err=%v",tc.code,rc,err) }
        })
    }
}
func TestReceiptStrictDuplicateKeys(t *testing.T) {
    f := receiptTestWave(t)
    c,e,x := safeFixture(t,f,"o","a1",0)
    x.Receipt = append([]byte("{\"exit_code\":0,"),x.Receipt[1:]...)
    if rc,err := validateExtraction(f,c.Attempt,e,x); err==nil || rc!=5 { t.Fatalf("duplicate key accepted: %v",err) }
}
func TestReceiptRejectsArchiveLinksAndBinaryText(t *testing.T) {
    f := receiptTestWave(t)
    for _,kind := range []string{"symlink","binary","traversal"} {
        t.Run(kind,func(t *testing.T){
            c,e,x := safeFixture(t,f,"o","a1",0)
            var buf bytes.Buffer
            tw := tar.NewWriter(&buf)
            name := "report.txt"
            body := []byte("synthetic verified text\n")
            header := &tar.Header{Name:name,Mode:0644,Size:int64(len(body)),Typeflag:tar.TypeReg}
            if kind=="symlink" { header.Typeflag=tar.TypeSymlink; header.Size=0; header.Linkname="target"; body=nil }
            if kind=="binary" { body=[]byte{0,255}; header.Size=int64(len(body)) }
            if kind=="traversal" { header.Name="../report.txt"; name=header.Name }
            if err:=tw.WriteHeader(header); err!=nil { t.Fatal(err) }
            if _,err:=tw.Write(body); err!=nil { t.Fatal(err) }
            if err:=tw.Close(); err!=nil { t.Fatal(err) }
            x.Payload=buf.Bytes()
            x.Files=map[string][]byte{name:body}
            mutateReceipt(t,&x,func(d workerbridge.Document){
                extract:=receiptObject(d["extract"])
                extract["bytes"]=len(x.Payload)
                extract["sha256"]=digest(x.Payload)
            })
            if rc,err:=validateExtraction(f,c.Attempt,e,x); err==nil || rc!=5 { t.Fatalf("unsafe extract accepted: %v",err) }
        })
    }
}
func TestReceiptValidationDoesNotMutateEvidence(t *testing.T) {
    f := receiptTestWave(t)
    c,e,x := safeFixture(t,f,"o","a1",0)
    before,_ := json.Marshal(struct{C Candidate; E Execution; X Extraction}{c,e,x})
    if _,err:=validateExtraction(f,c.Attempt,e,x); err!=nil { t.Fatal(err) }
    after,_ := json.Marshal(struct{C Candidate; E Execution; X Extraction}{c,e,x})
    if !bytes.Equal(before,after) { t.Fatal("validation mutated admitted evidence") }
}

func rebindReceiptTask(t testing.TB, candidate Candidate, execution *Execution, extraction *Extraction) {
    t.Helper()
    taskHash := digest(candidate.Attempt.WorkerTask)
    mutateGrant(t,execution,extraction,func(grant workerbridge.Document){grant["task_sha256"]=taskHash})
    mutateReceipt(t,extraction,func(receipt workerbridge.Document){receipt["task_sha256"]=taskHash})
}

func TestReceiptSHA256AnswerMustMatchObservedSource(t *testing.T) {
    f := receiptTestWave(t)
    cases := []struct{name,answer string; reject bool}{
        {"matching",f.wave.InputSHA256,false},
        {"different-digest",strings.Repeat("f",64),true},
        {"uppercase",strings.ToUpper(f.wave.InputSHA256),true},
        {"short",f.wave.InputSHA256[:63],true},
    }
    for _,tc := range cases {
        t.Run(tc.name,func(t *testing.T){
            candidate,execution,extraction := safeFixture(t,f,"o","sha",0)
            mutateWorker(t,&candidate,func(worker workerbridge.Document){
                receiptObject(worker["evidence"])["requests"] = []any{workerbridge.Document{"request_id":"source", "kind":"sha256", "path":"input.txt"}}
            })
            rebindReceiptTask(t,candidate,&execution,&extraction)
            mutateReceipt(t,&extraction,func(receipt workerbridge.Document){
                receiptObject(receipt["evidence"])["answers"] = []any{workerbridge.Document{"request_id":"source", "answer":tc.answer}}
            })
            rc,err := validateExtraction(f,candidate.Attempt,execution,extraction)
            if tc.reject {
                if err==nil || rc!=5 { t.Fatalf("SHA256 mismatch accepted: rc=%d err=%v",rc,err) }
            } else if err!=nil || rc!=0 { t.Fatalf("matching SHA256 refused: rc=%d err=%v",rc,err) }
        })
    }
}

func TestFailedReceiptWithoutExtractStillHonorsEvidenceBudget(t *testing.T) {
    f := receiptTestWave(t)
    cases := []struct{name string; budget,retained int; reject bool}{
        {"within-budget",102400,0,false},
        {"receipt-over-budget",1024,0,true},
        {"retained-logs-over-budget",8192,8192,true},
    }
    for _,tc := range cases {
        t.Run(tc.name,func(t *testing.T){
            candidate,execution,extraction := safeFixture(t,f,"o","budget",5)
            mutateWorker(t,&candidate,func(worker workerbridge.Document){receiptObject(worker["evidence"])["max_bytes"]=tc.budget})
            rebindReceiptTask(t,candidate,&execution,&extraction)
            mutateReceipt(t,&extraction,func(receipt workerbridge.Document){
                receipt["extract"] = nil
                if tc.retained>0 {
                    receiptObject(receipt["gate"])["stdout"] = workerbridge.Document{"sha256":strings.Repeat("c",64), "retained_bytes":tc.retained, "total_bytes":tc.retained, "truncated":false}
                }
            })
            extraction.Payload=nil
            extraction.Files=nil
            extraction.Repros=nil
            rc,err := validateExtraction(f,candidate.Attempt,execution,extraction)
            if tc.reject {
                if err==nil || rc!=5 { t.Fatalf("oversized receipt accepted without extract: rc=%d err=%v",rc,err) }
            } else if err!=nil || rc!=5 { t.Fatalf("complete failed receipt refused: rc=%d err=%v",rc,err) }
        })
    }
}

