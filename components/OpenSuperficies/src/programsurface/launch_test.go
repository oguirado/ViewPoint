package programsurface

import (
	"reflect"
	"strings"
	"testing"
)

func TestRunArgsGolden(t *testing.T) {
	p := Program{ID: "notepad", Image: "opensuperficies-program-gtk-arm64:0.1.0", Network: NetNone, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir, A11y: true, Enabled: true}
	tiles := []Tile{{"w1", 0, 0, 640, 900}, {"w2", 640, 0, 640, 900}}
	for _, network := range []Network{NetNone, NetProxy} {
		t.Run(string(network), func(t *testing.T) {
			p.Network = network
			want := []string{"docker", "run", "-d", "--name", "srf-pg-test", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--network", string(network)}
			if network == NetProxy {
				want[len(want)-1] = "srf-idnet"
			}
			want = append(want, "--cpuset-cpus", "10-14", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256", "--shm-size", "2g",
				"--tmpfs", "/run/vqa/fb:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
				"--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=256m",
				"--tmpfs", "/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
				"--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
				"--tmpfs", "/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
				"--mount", "type=bind,src=/profiles/test,dst=/profile", "--mount", "type=bind,src=/views/test,dst=/srfview")
			proxy := ""
			if network == NetProxy {
				proxy = "http://proxy:3128"
				want = append(want, "--env", "SRF_EGRESS_PROXY="+proxy)
			}
			want = append(want, "--env", "SRF_PROGRAM=notepad", "--env", "SRF_WINDOWS=2", "--env", `SRF_TILES=[{"id":"w1","x":0,"y":0,"w":640,"h":900},{"id":"w2","x":640,"y":0,"w":640,"h":900}]`, "--env", "NO_AT_BRIDGE=0", p.Image)
			if got := RunArgs("srf-pg-test", "/profiles/test", "/views/test", p, tiles, proxy); !reflect.DeepEqual(got, want) {
				t.Fatalf("argv\ngot  %q\nwant %q", got, want)
			}
		})
	}
	p.Network = NetProxy
	if RunArgs("srf-pg-test", "/p", "/v", p, tiles, "") != nil {
		t.Fatal("missing proxy accepted")
	}
	p.Network = NetNone
	p.A11y = false
	for _, arg := range RunArgs("srf-pg-test", "/p", "/v", p, tiles, "ignored") {
		if strings.Contains(arg, "SRF_EGRESS_PROXY") || arg == "NO_AT_BRIDGE=0" {
			t.Fatal("unexpected env", arg)
		}
	}
}

func TestMetaviewerRunArgsGolden(t *testing.T) {
	p, err := Lookup("metaviewer-nested")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"docker", "run", "-d", "--name", "srf-pg-metaviewer", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--network", "none",
		"--cpuset-cpus", "10-14", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256", "--shm-size", "2g",
		"--tmpfs", "/run/vqa/fb:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
		"--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=256m",
		"--tmpfs", "/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
		"--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
		"--tmpfs", "/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
		"--mount", "type=bind,src=/profiles/test,dst=/profile", "--mount", "type=bind,src=/views/test,dst=/srfview",
		"--env", "SRF_PROGRAM=metaviewer-nested", "--env", "SRF_WINDOWS=1", "--env", `SRF_TILES=[{"id":"w1","x":0,"y":0,"w":1280,"h":720}]`, "--env", "NO_AT_BRIDGE=0", "opensuperficies-program-electron-arm64:0.1.0"}
	got := RunArgs("srf-pg-metaviewer", "/profiles/test", "/views/test", p, []Tile{{"w1", 0, 0, 1280, 720}}, "ignored")
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("argv\ngot  %q\nwant %q", got, want)
	}
}

func TestParseReady(t *testing.T) {
	tiles := []Tile{{"w1", 0, 0, 640, 900}, {"w2", 640, 0, 640, 900}}
	valid := `PROGRAM_READY {"windows":[{"id":"w1","x":0,"y":0,"w":640,"h":900,"xid":"0x1"},{"id":"w2","x":640,"y":0,"w":640,"h":900,"xid":"0xabcdef12"}]}`
	got, err := ParseReady(valid, tiles)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[1].XID != "0xabcdef12" {
		t.Fatalf("windows: %+v", got)
	}
	cases := map[string]string{
		"prefix": strings.Replace(valid, "PROGRAM_READY", "IDENTITY_READY", 1), "separator": strings.Replace(valid, "READY ", "READY", 1),
		"trailing": valid + ` {}`, "extra top": strings.Replace(valid, `{"windows":`, `{"other":0,"windows":`, 1),
		"extra window":  strings.Replace(valid, `"id":"w1"`, `"extra":0,"id":"w1"`, 1),
		"missing field": strings.Replace(valid, `"x":0,`, "", 1), "null field": strings.Replace(valid, `"x":0`, `"x":null`, 1),
		"duplicate key":         strings.Replace(valid, `"x":0`, `"x":0,"x":0`, 1),
		"duplicate top":         strings.Replace(valid, `{"windows":`, `{"windows":[],"windows":`, 1),
		"xid numeric duplicate": strings.Replace(valid, "0xabcdef12", "0x01", 1),
		"xid duplicate":         strings.Replace(valid, "0xabcdef12", "0x1", 1), "xid uppercase": strings.Replace(valid, "0x1", "0xA", 1),
		"xid long": strings.Replace(valid, "0x1", "0x123456789", 1), "xid prefix": strings.Replace(valid, "0x1", "1", 1),
		"geometry": strings.Replace(valid, `"w":640`, `"w":641`, 1), "id order": strings.Replace(valid, `"id":"w1"`, `"id":"w2"`, 1),
		"count": `PROGRAM_READY {"windows":[]}`, "null": `PROGRAM_READY {"windows":null}`, "malformed": `PROGRAM_READY {`,
		"oversize": valid + strings.Repeat(" ", 65537),
	}
	for name, line := range cases {
		t.Run(name, func(t *testing.T) { _, err := ParseReady(line, tiles); requireCode(t, err, 6) })
	}
}

func TestVMRunArgsGolden(t *testing.T) {
	t.Setenv("SRF_KVM_GID", "109")
	p := vmProgram()
	want := []string{"docker", "run", "-d", "--name", "srf-vm-test", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--network", "none",
		"--cpuset-cpus", "10-14", "--memory", "8192m", "--memory-swap", "8192m", "--pids-limit", "256", "--shm-size", "64m",
		"--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=64m",
		"--tmpfs", "/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
		"--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
		"--tmpfs", "/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
		"--device", "/dev/kvm", "--group-add", "109",
		"--mount", "type=bind,src=/p,dst=/profile", "--mount", "type=bind,src=/v,dst=/srfview",
		"--env", "SRF_PROGRAM=notepad-vm", "--env", "SRF_WINDOWS=1", "--env", `SRF_TILES=[{"id":"w1","x":0,"y":0,"w":1280,"h":900}]`,
		"--env", "SRF_GUEST_CPUS=4", "--env", "SRF_GUEST_MEM_MIB=6144", "--env", "SRF_PROFILE_DISK_MIB=1024", "--env", "NO_AT_BRIDGE=0", p.Image}
	got := RunArgs("srf-vm-test", "/p", "/v", p, []Tile{{"w1", 0, 0, 1280, 900}}, "ignored")
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q; want %q", got, want)
	}
	for _, id := range []string{"", "srf-pg-test", "srf-vm-", "srf-vm-A", "srf-vm--a", "srf-vm-a\n", "srf-vm-" + strings.Repeat("a", 41)} {
		if RunArgs(id, "/p", "/v", p, nil, "") != nil {
			t.Errorf("accepted name %q", id)
		}
	}
	for _, id := range []string{"srf-vm-a", "srf-vm-0", "srf-vm-" + strings.Repeat("a", 40)} {
		if RunArgs(id, "/p", "/v", p, nil, "") == nil {
			t.Errorf("refused name %q", id)
		}
	}
	for _, gid := range []string{"", "-1", "+1", " 109", "109\n", "109,110", "kvm", "١٠٩"} {
		t.Setenv("SRF_KVM_GID", gid)
		if RunArgs("srf-vm-test", "/p", "/v", p, nil, "") != nil {
			t.Errorf("accepted gid %q", gid)
		}
	}
}

func TestKVMOnlyForVM(t *testing.T) {
	t.Setenv("SRF_KVM_GID", "109")
	p, _ := Lookup("notepad")
	for _, runtime := range []Runtime{"", RuntimeContainer} {
		p.Runtime = runtime
		args := RunArgs("srf-pg-test", "/p", "/v", p, nil, "")
		if args == nil {
			t.Fatal("container refused")
		}
		for _, arg := range args {
			if strings.Contains(arg, "kvm") || arg == "--device" || arg == "--group-add" {
				t.Fatalf("container KVM: %q", args)
			}
		}
	}
}

func TestVMRunArgsKVMGIDBounds(t *testing.T) {
	p := vmProgram()
	for _, gid := range []string{"0", "00", "01", "0109", "4294967295", "4294967296", "18446744073709551616", "+109", "-109"} {
		t.Run("reject_"+gid, func(t *testing.T) {
			t.Setenv("SRF_KVM_GID", gid)
			if got := RunArgs("srf-vm-test", "/p", "/v", p, nil, ""); got != nil {
				t.Errorf("accepted invalid KVM GID %q", gid)
			}
		})
	}
	for _, gid := range []string{"1", "109", "4294967294"} {
		t.Run("accept_"+gid, func(t *testing.T) {
			t.Setenv("SRF_KVM_GID", gid)
			got := RunArgs("srf-vm-test", "/p", "/v", p, nil, "")
			if got == nil {
				t.Fatalf("refused valid KVM GID %q", gid)
			}
			for i, arg := range got {
				if arg == "--group-add" && got[i+1] != gid {
					t.Fatalf("GID changed: %q", got[i+1])
				}
			}
		})
	}
}

func TestRunArgsMountPathValidation(t *testing.T) {
	t.Setenv("SRF_KVM_GID", "109")
	container, _ := Lookup("notepad")
	for _, p := range []Program{container, vmProgram()} {
		t.Run(p.ID, func(t *testing.T) {
			name := "srf-pg-test"
			if p.Runtime == RuntimeVM {
				name = "srf-vm-test"
			}
			for _, path := range []string{"", "relative", "p/v", "/p/", "/p//v", "/p/./v", "/p/../v", "/p/..", "/../p", "/p,dst=/etc", "/p\nv", "/p\rv", "/p\x00v", "/p\xffv"} {
				for _, field := range []string{"profile", "view"} {
					t.Run(field+"_"+path, func(t *testing.T) {
						profile, view := "/profiles/test", "/views/test"
						if field == "profile" {
							profile = path
						} else {
							view = path
						}
						if got := RunArgs(name, profile, view, p, nil, ""); got != nil {
							t.Errorf("accepted unsafe %s path %q", field, path)
						}
					})
				}
			}
			for _, path := range []string{"/", "/profiles/a-b_1.ext", "/home/user/Música/TeamWork_Antigravity2/campaigns/os-vm-0/state/identities/profiles/notepad-1", "/p with spaces/{view}=value", "/p..v", "/p=v", t.TempDir()} {
				for _, field := range []string{"profile", "view"} {
					t.Run("accept_"+field+"_"+path, func(t *testing.T) {
						profile, view, dst := path, "/views/test", "/profile"
						if field == "view" {
							profile, view, dst = "/profiles/test", path, "/srfview"
						}
						args := RunArgs(name, profile, view, p, nil, "")
						want := "type=bind,src=" + path + ",dst=" + dst
						for i, arg := range args {
							if arg == "--mount" && i+1 < len(args) && args[i+1] == want {
								return
							}
						}
						t.Fatalf("mount path not preserved as one argv element: %q in %q", path, args)
					})
				}
			}
		})
	}
}
