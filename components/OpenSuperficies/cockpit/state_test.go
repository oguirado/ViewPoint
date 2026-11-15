//go:build cockpit

package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestStateCanonicalAndGeometry(t *testing.T) {
	d, err := buildStateSnapshot(stateInput{Host: "osbox", WindowMode: "column_1_3_dual", Cap: 4, Seq: 7, Timestamp: 6000, Capsules: []stateCapsuleInput{{Slot: 1, Archetype: "desktop", Lifecycle: "booting", Health: "attaching", Mode: "read_only"}, {Slot: 0, Archetype: "browser", Lifecycle: "ready", Health: "healthy", Mode: "read_only"}}})
	if err != nil {
		t.Fatal(err)
	}
	b, err := canonicalState(d)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateStateBytes(b); err != nil {
		t.Fatal(err)
	}
	if d.Capsules[1].Geometry.Y != 1024 || d.Capsules[0].SlotID != "srf-ng-0" {
		t.Fatal("geometry/order")
	}
	var x map[string]any
	if err := json.Unmarshal(b, &x); err != nil {
		t.Fatal(err)
	}
}

const stateGoodFixture = `{"capsules":[{"archetype":"browser","cdp":9220,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":0,"slot_id":"srf-ng-0","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-0","workspace":1,"xpra_port":10020},{"archetype":"desktop","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":1024},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"booting","mode":"read_only","ready":false,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":1,"slot_id":"srf-ng-1","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-1","workspace":1,"xpra_port":10021}],"host":"osbox","schema":"os.cu.cockpit-state/v2","seq":7,"ts_mono_ms":6000,"window_mode":"column_1_3_dual","workspaces":[{"layout":"1x2","n":1,"rect":{"h":2048,"w":1264,"x":0,"y":0},"tree":{"direction":"vertical","left":{"scale_q16":64716,"slot_id":"srf-ng-0","type":"leaf"},"ratio_q16":32768,"right":{"scale_q16":64716,"slot_id":"srf-ng-1","type":"leaf"},"type":"split"}}]}`
const stateFixtureMutations = `[{"name":"good","ok":true,"patches":[]},{"name":"ok_two_workspaces","ok":true,"patches":[{"path":["capsules"],"value":[{"archetype":"browser","cdp":9220,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":0,"slot_id":"srf-ng-0","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-0","workspace":1,"xpra_port":10020},{"archetype":"desktop","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":1024},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"booting","mode":"read_only","ready":false,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":1,"slot_id":"srf-ng-1","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-1","workspace":1,"xpra_port":10021},{"archetype":"terminal","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":2,"slot_id":"srf-ng-2","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/2/slot/srf-ng-2","workspace":2,"xpra_port":10022}]},{"path":["workspaces"],"value":[{"layout":"1x2","n":1,"rect":{"h":2048,"w":1264,"x":0,"y":0},"tree":{"direction":"vertical","left":{"slot_id":"srf-ng-0","type":"leaf","scale_q16":64716},"ratio_q16":32768,"right":{"slot_id":"srf-ng-1","type":"leaf","scale_q16":64716},"type":"split"}},{"layout":"1x1","n":2,"rect":{"h":1024,"w":1264,"x":0,"y":0},"tree":{"slot_id":"srf-ng-2","type":"leaf","scale_q16":64716}}]}]},{"name":"ok_recording_ram","ok":true,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"recording"},{"path":["capsules",0,"rec_state","store"],"value":"ram"},{"path":["capsules",0,"rec_state","ttl_ms"],"value":600000}]},{"name":"ok_sealed_disk","ok":true,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"sealed"},{"path":["capsules",0,"rec_state","store"],"value":"disk"}]},{"name":"ok_agent_locked","ok":true,"patches":[{"path":["capsules",0,"lease_epoch"],"value":3},{"path":["capsules",0,"lease_holder"],"value":"wender-agent"},{"path":["capsules",0,"mode"],"value":"agent_locked"}]},{"name":"missing_ready","ok":false,"patches":[{"path":["capsules",0,"ready"],"remove":true}]},{"name":"wrong_geometry","ok":false,"patches":[{"path":["capsules",1,"geometry","y"],"value":1064}]},{"name":"invented_telemetry_fixture","ok":false,"patches":[{"path":["capsules",0,"telemetry","cpu_permille"],"value":{"source":"fixture","ts_mono_ms":5000,"value":420}}]},{"name":"invented_telemetry_bare","ok":false,"patches":[{"path":["capsules",0,"telemetry","cpu_permille"],"value":420}]},{"name":"telemetry_no_ts","ok":false,"patches":[{"path":["capsules",0,"telemetry","mem_bytes","ts_mono_ms"],"remove":true},{"path":["capsules",0,"telemetry","mem_bytes","value"],"value":1}]},{"name":"float_value","ok":false,"patches":[{"path":["capsules",0,"telemetry","mem_bytes","value"],"value":1.5}]},{"name":"disk_path_leak","ok":false,"patches":[{"path":["capsules",0,"rec_state","dir"],"value":"/home/x"}]},{"name":"extra_field","ok":false,"patches":[{"path":["capsules",0,"token"],"value":"abc"}]},{"name":"ready_lies","ok":false,"patches":[{"path":["capsules",1,"ready"],"value":true}]},{"name":"agent_without_holder","ok":false,"patches":[{"path":["capsules",0,"mode"],"value":"agent_locked"}]},{"name":"dup_slot","ok":false,"patches":[{"path":["capsules",1,"archetype"],"value":"browser"},{"path":["capsules",1,"cdp"],"value":9220},{"path":["capsules",1,"geometry","y"],"value":0},{"path":["capsules",1,"lifecycle"],"value":"ready"},{"path":["capsules",1,"ready"],"value":true},{"path":["capsules",1,"slot"],"value":0},{"path":["capsules",1,"slot_id"],"value":"srf-ng-0"},{"path":["capsules",1,"uri"],"value":"os://osbox/ws/1/slot/srf-ng-0"},{"path":["capsules",1,"xpra_port"],"value":10020}]},{"name":"uri_mismatch","ok":false,"patches":[{"path":["capsules",0,"uri"],"value":"os://osbox/ws/2/slot/srf-ng-0"}]},{"name":"bad_port","ok":false,"patches":[{"path":["capsules",0,"xpra_port"],"value":10021}]},{"name":"ram_without_ttl","ok":false,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"recording"},{"path":["capsules",0,"rec_state","store"],"value":"ram"}]},{"name":"leaf_below_useful_width","ok":false,"patches":[{"path":["capsules",0,"geometry","w"],"value":632},{"path":["capsules",1,"geometry","w"],"value":632},{"path":["capsules",1,"geometry","x"],"value":632},{"path":["capsules",1,"geometry","y"],"value":0},{"path":["window_mode"],"value":"tile_1_6"},{"path":["workspaces",0,"rect","h"],"value":1024},{"path":["workspaces",0,"tree","direction"],"value":"horizontal"}]},{"name":"split_in_tile_1_6","ok":false,"patches":[{"path":["window_mode"],"value":"tile_1_6"}]},{"name":"side_by_side_2528","ok":false,"patches":[{"path":["capsules",1,"geometry","x"],"value":1264},{"path":["capsules",1,"geometry","y"],"value":0},{"path":["workspaces",0,"rect","h"],"value":1024},{"path":["workspaces",0,"rect","w"],"value":2528},{"path":["workspaces",0,"tree","direction"],"value":"horizontal"}]},{"name":"idle_with_store","ok":false,"patches":[{"path":["capsules",0,"rec_state","store"],"value":"ram"},{"path":["capsules",0,"rec_state","ttl_ms"],"value":5000}]},{"name":"recording_to_disk","ok":false,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"recording"},{"path":["capsules",0,"rec_state","store"],"value":"disk"}]},{"name":"recording_store_null","ok":false,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"recording"}]},{"name":"error_on_disk","ok":false,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"error"},{"path":["capsules",0,"rec_state","store"],"value":"disk"}]},{"name":"dup_tree_leaf","ok":false,"patches":[{"path":["capsules"],"value":[{"archetype":"browser","cdp":9220,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":0,"slot_id":"srf-ng-0","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-0","workspace":1,"xpra_port":10020},{"archetype":"desktop","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":1024},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"booting","mode":"read_only","ready":false,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":1,"slot_id":"srf-ng-1","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-1","workspace":1,"xpra_port":10021},{"archetype":"terminal","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":2,"slot_id":"srf-ng-2","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/2/slot/srf-ng-2","workspace":2,"xpra_port":10022}]},{"path":["workspaces"],"value":[{"layout":"1x2","n":1,"rect":{"h":2048,"w":1264,"x":0,"y":0},"tree":{"direction":"vertical","left":{"slot_id":"srf-ng-0","type":"leaf","scale_q16":64716},"ratio_q16":32768,"right":{"slot_id":"srf-ng-1","type":"leaf","scale_q16":64716},"type":"split"}},{"layout":"1x1","n":2,"rect":{"h":1024,"w":1264,"x":0,"y":0},"tree":{"slot_id":"srf-ng-0","type":"leaf","scale_q16":64716}}]}]},{"name":"ghost_tree_leaf","ok":false,"patches":[{"path":["workspaces"],"value":[{"layout":"1x2","n":1,"rect":{"h":2048,"w":1264,"x":0,"y":0},"tree":{"direction":"vertical","left":{"slot_id":"srf-ng-0","type":"leaf","scale_q16":64716},"ratio_q16":32768,"right":{"slot_id":"srf-ng-1","type":"leaf","scale_q16":64716},"type":"split"}},{"layout":"1x1","n":2,"rect":{"h":1024,"w":1264,"x":0,"y":0},"tree":{"slot_id":"srf-ng-9","type":"leaf","scale_q16":64716}}]}]},{"name":"unsorted_workspaces","ok":false,"patches":[{"path":["capsules"],"value":[{"archetype":"browser","cdp":9220,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":0,"slot_id":"srf-ng-0","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-0","workspace":1,"xpra_port":10020},{"archetype":"desktop","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":1024},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"booting","mode":"read_only","ready":false,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":1,"slot_id":"srf-ng-1","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-1","workspace":1,"xpra_port":10021},{"archetype":"terminal","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":2,"slot_id":"srf-ng-2","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/2/slot/srf-ng-2","workspace":2,"xpra_port":10022}]},{"path":["workspaces"],"value":[{"layout":"1x1","n":2,"rect":{"h":1024,"w":1264,"x":0,"y":0},"tree":{"slot_id":"srf-ng-2","type":"leaf","scale_q16":64716}},{"layout":"1x2","n":1,"rect":{"h":2048,"w":1264,"x":0,"y":0},"tree":{"direction":"vertical","left":{"slot_id":"srf-ng-0","type":"leaf","scale_q16":64716},"ratio_q16":32768,"right":{"slot_id":"srf-ng-1","type":"leaf","scale_q16":64716},"type":"split"}}]}]},{"name":"layout_leaf_mismatch","ok":false,"patches":[{"path":["workspaces",0,"layout"],"value":"2x2"}]},{"name":"orphan_capsule","ok":false,"patches":[{"path":["capsules"],"value":[{"archetype":"browser","cdp":9220,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":0,"slot_id":"srf-ng-0","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-0","workspace":1,"xpra_port":10020},{"archetype":"desktop","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":1024},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"booting","mode":"read_only","ready":false,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":1,"slot_id":"srf-ng-1","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-1","workspace":1,"xpra_port":10021},{"archetype":"desktop","cdp":null,"geometry":{"h":1024,"w":1264,"x":0,"y":0},"health":"healthy","lease_epoch":0,"lease_holder":null,"lifecycle":"ready","mode":"read_only","ready":true,"rec_state":{"phase":"idle","store":null,"ttl_ms":null},"seq":7,"slot":3,"slot_id":"srf-ng-3","telemetry":{"cpu_permille":null,"mem_bytes":{"source":"cgroup","ts_mono_ms":5000,"value":104857600}},"uri":"os://osbox/ws/1/slot/srf-ng-3","workspace":1,"xpra_port":10023}]}]},{"name":"extreme_q16_ratio","ok":false,"patches":[{"path":["workspaces",0,"tree","ratio_q16"],"value":1}]},{"name":"tree_depth_3","ok":false,"patches":[{"path":["workspaces",0,"tree","left","slot_id"],"remove":true},{"path":["workspaces",0,"tree","left","direction"],"value":"vertical"},{"path":["workspaces",0,"tree","left","left"],"value":{"direction":"vertical","left":{"slot_id":"srf-ng-0","type":"leaf","scale_q16":64716},"ratio_q16":32768,"right":{"slot_id":"srf-ng-4","type":"leaf","scale_q16":64716},"type":"split"}},{"path":["workspaces",0,"tree","left","ratio_q16"],"value":32768},{"path":["workspaces",0,"tree","left","right"],"value":{"slot_id":"srf-ng-5","type":"leaf","scale_q16":64716}},{"path":["workspaces",0,"tree","left","type"],"value":"split"}]},{"name":"unlisted_sensor_source","ok":false,"patches":[{"path":["capsules",0,"telemetry","mem_bytes","source"],"value":"stub"}]},{"name":"zero_ts_mono","ok":false,"patches":[{"path":["capsules",0,"telemetry","mem_bytes","ts_mono_ms"],"value":0}]},{"name":"stale_metric_not_nulled","ok":false,"patches":[{"path":["ts_mono_ms"],"value":8001}]},{"name":"future_metric_ts","ok":false,"patches":[{"path":["capsules",0,"telemetry","mem_bytes","ts_mono_ms"],"value":6001}]},{"name":"missing_root_ts","ok":false,"patches":[{"path":["ts_mono_ms"],"remove":true}]},{"name":"agent_locked_epoch_zero","ok":false,"patches":[{"path":["capsules",0,"lease_holder"],"value":"wender-agent"},{"path":["capsules",0,"mode"],"value":"agent_locked"}]},{"name":"dot_leader_slug","ok":false,"patches":[{"path":["capsules",0,"lease_epoch"],"value":1},{"path":["capsules",0,"lease_holder"],"value":"..."},{"path":["capsules",0,"mode"],"value":"agent_locked"}]},{"name":"browser_without_cdp","ok":false,"patches":[{"path":["capsules",0,"cdp"],"value":null}]},{"name":"desktop_with_cdp","ok":false,"patches":[{"path":["capsules",1,"cdp"],"value":9221}]},{"name":"missing_archetype","ok":false,"patches":[{"path":["capsules",0,"archetype"],"remove":true}]},{"name":"degraded_as_mode","ok":false,"patches":[{"path":["capsules",0,"mode"],"value":"DEGRADED"}]},{"name":"missing_health","ok":false,"patches":[{"path":["capsules",0,"health"],"remove":true}]},{"name":"sealed_store_null","ok":false,"patches":[{"path":["capsules",0,"rec_state","phase"],"value":"sealed"}]}]`

func TestStateFrozenOracleFixtures(t *testing.T) {
	var cases []struct {
		Name    string `json:"name"`
		OK      bool   `json:"ok"`
		Patches []struct {
			Path   []any `json:"path"`
			Value  any   `json:"value"`
			Remove bool  `json:"remove"`
		} `json:"patches"`
	}
	if err := json.Unmarshal([]byte(stateFixtureMutations), &cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			var raw any
			if err := json.Unmarshal([]byte(stateGoodFixture), &raw); err != nil {
				t.Fatal(err)
			}
			for _, p := range c.Patches {
				o := raw
				for _, k := range p.Path[:len(p.Path)-1] {
					switch key := k.(type) {
					case string:
						o = o.(map[string]any)[key]
					case float64:
						o = o.([]any)[int(key)]
					}
				}
				k := p.Path[len(p.Path)-1]
				switch key := k.(type) {
				case string:
					if p.Remove {
						delete(o.(map[string]any), key)
					} else {
						o.(map[string]any)[key] = p.Value
					}
				case float64:
					o.([]any)[int(key)] = p.Value
				}
			}
			b, err := json.Marshal(raw)
			if err != nil {
				t.Fatal(err)
			}
			err = validateStateBytes(b)
			if (err == nil) != c.OK {
				t.Fatalf("want valid=%v, error=%v", c.OK, err)
			}
		})
	}
}

func stateTestInput() stateInput {
	return stateInput{Host: "osbox", WindowMode: "column_1_3_dual", Cap: 4, Timestamp: 6000, Capsules: []stateCapsuleInput{{Slot: 0, Archetype: "browser", Lifecycle: "ready", Health: "healthy", Mode: "read_only"}, {Slot: 1, Archetype: "desktop", Lifecycle: "booting", Health: "attaching", Mode: "read_only"}}}
}
func stateTestServer(t *testing.T) (*App, *stateServer) {
	t.Helper()
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
	t.Setenv("OSCU_STATE_ADDR", "")
	a := &App{}
	stop, err := startStateServer(a)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stop)
	stateServers.Lock()
	s := stateServers.items[a]
	stateServers.Unlock()
	s.source = func() (stateInput, error) { return stateTestInput(), nil }
	return a, s
}
func TestStateTransport(t *testing.T) {
	_, s := stateTestServer(t)
	cases := []struct {
		name, method, path, auth, host, origin, site string
		want                                         int
	}{
		{name: "missing bearer", method: "GET", path: "/state", want: 403},
		{name: "wrong bearer", method: "GET", path: "/state", auth: "Bearer wrong", want: 403},
		{name: "query only", method: "GET", path: "/state?t=" + s.credential, want: 403},
		{name: "query with auth", method: "GET", path: "/state?t=hidden", auth: "ok", want: 403},
		{name: "post", method: "POST", path: "/state", auth: "ok", want: 405},
		{name: "put", method: "PUT", path: "/state", auth: "ok", want: 405},
		{name: "delete", method: "DELETE", path: "/state", auth: "ok", want: 405},
		{name: "options", method: "OPTIONS", path: "/state", auth: "ok", want: 405},
		{name: "other route", method: "GET", path: "/x", auth: "ok", want: 404},
		{name: "encoded route", method: "GET", path: "/st%61te", auth: "ok", want: 404},
		{name: "bad host", method: "GET", path: "/state", auth: "ok", host: "evil.invalid", want: 403},
		{name: "host no port", method: "GET", path: "/state", auth: "ok", host: "localhost", want: 403},
		{name: "origin", method: "GET", path: "/state", auth: "ok", origin: "http://localhost", want: 403},
		{name: "null origin", method: "GET", path: "/state", auth: "ok", origin: "null", want: 403},
		{name: "cross site", method: "GET", path: "/state", auth: "ok", site: "cross-site", want: 403},
		{name: "same origin", method: "GET", path: "/state", auth: "ok", site: "same-origin", want: 403},
		{name: "same site", method: "GET", path: "/state", auth: "ok", site: "same-site", want: 403},
		{name: "get", method: "GET", path: "/state", auth: "ok", want: 200},
		{name: "head", method: "HEAD", path: "/state", auth: "ok", site: "none", want: 200},
	}
	client := &http.Client{Timeout: 3 * time.Second}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req, err := http.NewRequest(c.method, strings.TrimSuffix(s.url, "/state")+c.path, nil)
			if err != nil {
				t.Fatal(err)
			}
			if c.auth == "ok" {
				req.Header.Set("Authorization", "Bearer "+s.credential)
			} else if c.auth != "" {
				req.Header.Set("Authorization", c.auth)
			}
			if c.host != "" {
				req.Host = c.host
			}
			if c.origin != "" {
				req.Header.Set("Origin", c.origin)
			}
			if c.site != "" {
				req.Header.Set("Sec-Fetch-Site", c.site)
			}
			res, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer res.Body.Close()
			b, err := io.ReadAll(res.Body)
			if err != nil {
				t.Fatal(err)
			}
			if res.StatusCode != c.want {
				t.Fatalf("want %d got %d", c.want, res.StatusCode)
			}
			if res.Header.Get("Access-Control-Allow-Origin") != "" {
				t.Fatal("CORS enabled")
			}
			if c.want == 405 && res.Header.Get("Allow") != "GET, HEAD" {
				t.Fatal("Allow")
			}
			if c.want == 200 {
				if res.Header.Get("Content-Type") != "application/json" || res.Header.Get("Cache-Control") != "no-store" || res.Header.Get("X-Content-Type-Options") != "nosniff" {
					t.Fatal("headers")
				}
				if c.method == "HEAD" {
					if len(b) != 0 || res.Header.Get("ETag") == "" {
						t.Fatal("HEAD")
					}
					return
				}
				if err := validateStateBytes(b); err != nil {
					t.Fatal(err)
				}
				hash := sha256.Sum256(b)
				if res.Header.Get("ETag") != `"sha256-`+hex.EncodeToString(hash[:])+`"` {
					t.Fatal("etag")
				}
				if regexp.MustCompile(`(?i)token|password|secret|/home|/tmp`).Match(b) {
					t.Fatal("sensitive body")
				}
			}
		})
	}
	// Both explicitly allowed Host spellings, with the bound port.
	req, _ := http.NewRequest("GET", s.url, nil)
	_, port, _ := net.SplitHostPort(s.listener.Addr().String())
	req.Host = "localhost:" + port
	req.Header.Set("Authorization", "Bearer "+s.credential)
	res, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = res.Body.Close()
	if res.StatusCode != 200 {
		t.Fatal("localhost refused")
	}
	// Invalid source state is a 500, never a guessed geometry.
	s.mu.Lock()
	s.source = func() (stateInput, error) {
		in := stateTestInput()
		in.Capsules = append(in.Capsules, in.Capsules[0])
		return in, nil
	}
	s.mu.Unlock()
	res, err = client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = res.Body.Close()
	if res.StatusCode != 500 {
		t.Fatal("invalid state must fail closed")
	}
}
func TestStateRuntimeLifecycle(t *testing.T) {
	a, s := stateTestServer(t)
	info, err := os.Stat(s.discovery)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatal("discovery mode")
	}
	dir, err := os.Stat(filepath.Dir(s.discovery))
	if err != nil {
		t.Fatal(err)
	}
	if dir.Mode().Perm() != 0700 {
		t.Fatal("directory mode")
	}
	b, err := os.ReadFile(s.discovery)
	if err != nil {
		t.Fatal(err)
	}
	var discovery struct {
		PID        int `json:"pid"`
		Token, URL string
	}
	if err = json.Unmarshal(b, &discovery); err != nil {
		t.Fatal(err)
	}
	if discovery.PID != os.Getpid() || len(discovery.Token) != 32 || discovery.URL != s.url || a.StateURL() != s.url || strings.Contains(a.StateURL(), discovery.Token) {
		t.Fatal("discovery")
	}
	if _, err := startStateServer(a); err == nil {
		t.Fatal("duplicate listener")
	}
	if _, err := startStateServer(&App{}); err == nil {
		t.Fatal("live discovery replaced")
	}
	StopState(a)
	if _, err := os.Stat(s.discovery); !os.IsNotExist(err) {
		t.Fatal("discovery not removed")
	}
	if a.StateURL() != "" || a.EstadoCanonico() != "" || a.BloqueSlot("srf-ng-0") != "" {
		t.Fatal("stopped bindings")
	}
	StopState(a)
}
func TestStateStartRefusals(t *testing.T) {
	for _, address := range []string{"0.0.0.0:0", "192.0.2.1:0", "example.com:0", "[::1]:0", "127.0.0.2:0", "bad"} {
		t.Run(address, func(t *testing.T) {
			t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
			t.Setenv("OSCU_STATE_ADDR", address)
			if _, err := startStateServer(&App{}); err == nil {
				t.Fatal("unsafe bind")
			}
		})
	}
	t.Setenv("XDG_RUNTIME_DIR", "")
	t.Setenv("OSCU_STATE_ADDR", "")
	if _, err := startStateServer(&App{}); err == nil {
		t.Fatal("runtime fallback")
	}
	t.Setenv("XDG_RUNTIME_DIR", "relative")
	if _, err := startStateServer(&App{}); err == nil {
		t.Fatal("relative runtime")
	}
}
func TestStateUnsafeDiscovery(t *testing.T) {
	for _, kind := range []string{"live", "invalid", "foreign-mode", "symlink", "directory", "directory-mode"} {
		t.Run(kind, func(t *testing.T) {
			root := t.TempDir()
			t.Setenv("XDG_RUNTIME_DIR", root)
			t.Setenv("OSCU_STATE_ADDR", "")
			dir := filepath.Join(root, "os-cu")
			if err := os.Mkdir(dir, 0700); err != nil {
				t.Fatal(err)
			}
			p := filepath.Join(dir, "cockpit-state.json")
			switch kind {
			case "live":
				_ = os.WriteFile(p, []byte(fmt.Sprintf(`{"pid":%d}`, os.Getpid())), 0600)
			case "invalid":
				_ = os.WriteFile(p, []byte(`{}`), 0600)
			case "foreign-mode":
				_ = os.WriteFile(p, []byte(`{"pid":2147483647}`), 0644)
			case "symlink":
				_ = os.Symlink(filepath.Join(root, "absent"), p)
			case "directory":
				_ = os.Mkdir(p, 0700)
			case "directory-mode":
				_ = os.Chmod(dir, 0755)
			}
			if _, err := startStateServer(&App{}); err == nil {
				t.Fatal("unsafe discovery accepted")
			}
		})
	}
}
func TestStateStaleDiscovery(t *testing.T) {
	root := t.TempDir()
	t.Setenv("XDG_RUNTIME_DIR", root)
	t.Setenv("OSCU_STATE_ADDR", "")
	dir := filepath.Join(root, "os-cu")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	// This positive PID is outside Linux pid_max; ESRCH is verified before using the fixture.
	deadPID := 2147483647
	if syscall.Kill(deadPID, 0) != syscall.ESRCH {
		t.Fatal("dead PID precondition")
	}
	if err := os.WriteFile(filepath.Join(dir, "cockpit-state.json"), []byte(fmt.Sprintf(`{"pid":%d}`, deadPID)), 0600); err != nil {
		t.Fatal(err)
	}
	stop, err := startStateServer(&App{})
	if err != nil {
		t.Fatal(err)
	}
	stop()
}
func TestStateBindingsAndSequence(t *testing.T) {
	a, s := stateTestServer(t)
	raw := a.EstadoCanonico()
	var d StateDocument
	if err := json.Unmarshal([]byte(raw), &d); err != nil {
		t.Fatal(err)
	}
	for _, c := range d.Capsules {
		block := a.BloqueSlot(c.SlotID)
		if block == "" || !strings.Contains(raw, block) {
			t.Fatal("block is not an exact substring")
		}
	}
	if a.BloqueSlot("srf-ng-79") != "" {
		t.Fatal("unknown slot")
	}
	if _, ok := reflect.TypeOf(a).MethodByName("PublicarDisposicion"); ok {
		t.Fatal("write binding")
	}
	for i := 0; i < 3; i++ {
		var next StateDocument
		b, err := s.snapshot()
		if err != nil {
			t.Fatal(err)
		}
		if err = json.Unmarshal(b, &next); err != nil {
			t.Fatal(err)
		}
		if next.Seq != d.Seq {
			t.Fatal("idle seq changed")
		}
	}
	in := stateTestInput()
	in.Capsules[0].Lifecycle = "busy"
	s.mu.Lock()
	s.source = func() (stateInput, error) { return in, nil }
	s.mu.Unlock()
	var changed StateDocument
	b, err := s.snapshot()
	if err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(b, &changed)
	if changed.Seq != d.Seq+1 {
		t.Fatal("event seq did not change")
	}
	// A telemetry-only tick does not change seq.
	s.mu.Lock()
	in.Capsules[0].Telemetry.CPU = &StateMetric{Source: "srfd", Timestamp: max(1, time.Since(s.epoch).Milliseconds()), Value: 1}
	s.mu.Unlock()
	b, err = s.snapshot()
	if err != nil {
		t.Fatal(err)
	}
	var tick StateDocument
	_ = json.Unmarshal(b, &tick)
	if tick.Seq != changed.Seq {
		t.Fatal("telemetry bumped seq")
	}
}
func TestStateBuilderModesOverflowAndFreshness(t *testing.T) {
	for _, mode := range []string{"tile_1_6", "column_1_3_tall", "column_1_3_dual"} {
		for _, cap := range []int{1, 2, 4} {
			t.Run(fmt.Sprintf("%s/%d", mode, cap), func(t *testing.T) {
				in := stateTestInput()
				in.WindowMode = mode
				in.Cap = cap
				in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: 2, Archetype: "terminal", Lifecycle: "ready", Health: "healthy", Mode: "read_only"})
				d, err := buildStateSnapshot(in)
				if err != nil {
					t.Fatal(err)
				}
				want := 3
				if mode == "column_1_3_dual" && cap >= 2 {
					want = 2
				}
				if len(d.Workspaces) != want {
					t.Fatal("workspace overflow")
				}
				if mode == "column_1_3_tall" && d.Capsules[0].Geometry.H != 2080 {
					t.Fatal("tall canvas")
				}
			})
		}
	}
	in := stateTestInput()
	in.Capsules[0].Telemetry.CPU = &StateMetric{Source: "srfd", Timestamp: 2999, Value: 10}
	d, err := buildStateSnapshot(in)
	if err != nil {
		t.Fatal(err)
	}
	if d.Capsules[0].Telemetry.CPU != nil {
		t.Fatal("stale not nulled")
	}
	in.Capsules[0].Telemetry.CPU.Timestamp = 3000
	d, err = buildStateSnapshot(in)
	if err != nil || d.Capsules[0].Telemetry.CPU == nil {
		t.Fatal("fresh boundary rejected")
	}
	in.Capsules[0].Telemetry.CPU.Timestamp = 6001
	d, err = buildStateSnapshot(in)
	if err != nil || d.Capsules[0].Telemetry.CPU != nil {
		t.Fatal("future metric")
	}
	in.Capsules = nil
	if _, err := buildStateSnapshot(in); err == nil {
		t.Fatal("empty census fabricated")
	}
	for _, cap := range []int{0, -1, 81} {
		in := stateTestInput()
		in.Cap = cap
		if _, err := buildStateSnapshot(in); err == nil {
			t.Fatal("invalid cap")
		}
	}
}
func TestStateGoldenCanonical(t *testing.T) {
	var d StateDocument
	if err := json.Unmarshal([]byte(stateGoodFixture), &d); err != nil {
		t.Fatal(err)
	}
	b, err := canonicalState(d)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != stateGoodFixture {
		t.Fatal("canonical golden differs")
	}
	if len(b) != 1296 {
		t.Fatalf("golden bytes %d", len(b))
	}
}

// A bounded in-process srfd double; no daemon, display, recorder or cockpit is launched.
func stateReply(t *testing.T, payload string) {
	t.Helper()
	address := fmt.Sprintf("@BS-state-%d-%d", os.Getpid(), time.Now().UnixNano())
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: address, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	if err = listener.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SRF_SOCKET", address)
	done := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		if err = conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
			done <- err
			return
		}
		var request map[string]any
		if err = json.NewDecoder(conn).Decode(&request); err != nil {
			done <- err
			return
		}
		if request["comando"] != "listar" {
			done <- fmt.Errorf("unexpected read command")
			return
		}
		_, err = io.WriteString(conn, payload+"\n")
		done <- err
	}()
	t.Cleanup(func() {
		_ = listener.Close()
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(4 * time.Second):
			t.Error("srfd double did not stop")
		}
	})
}
func TestStateSrfdProjection(t *testing.T) {
	for _, tc := range []struct{ lifecycle, want, health, mode string }{
		{"listo", "ready", "healthy", "operator_controlled"}, {"iniciando", "booting", "attaching", "read_only"}, {"deteniendo", "stopping", "attaching", "read_only"}, {"huerfana", "failed", "zombie", "read_only"},
	} {
		t.Run(tc.lifecycle, func(t *testing.T) {
			stateReply(t, fmt.Sprintf(`{"ok":true,"rc":0,"datos":[{"slot":5,"clase":"browser","estado":%q,"id":"ignored-srfd-id","lease_epoca":8}]}`, tc.lifecycle))
			t.Setenv("OSCU_WINDOW_MODE", "")
			t.Setenv("max_capsulas_por_espacio", "4")
			s := &stateServer{app: &App{}}
			in, err := s.readInputs()
			if err != nil {
				t.Fatal(err)
			}
			in.Timestamp = 6000
			d, err := buildStateSnapshot(in)
			if err != nil {
				t.Fatal(err)
			}
			c := d.Capsules[0]
			if c.SlotID != "srf-ng-5" || c.Lifecycle != tc.want || c.Health != tc.health || c.Mode != tc.mode || c.LeaseEpoch != 8 || c.Telemetry.CPU != nil || c.Telemetry.Memory != nil || c.LeaseHolder != nil {
				t.Fatal("untruthful projection")
			}
		})
	}
	t.Run("explicit input lease", func(t *testing.T) {
		stateReply(t, `{"ok":true,"rc":0,"datos":[{"slot":0,"clase":"browser","estado":"listo","mode":"agent_locked","lease_holder":"agent-a","lease_epoca":3}]}`)
		t.Setenv("OSCU_WINDOW_MODE", "column_1_3_dual")
		t.Setenv("max_capsulas_por_espacio", "1")
		s := &stateServer{app: &App{}}
		in, err := s.readInputs()
		if err != nil {
			t.Fatal(err)
		}
		in.Timestamp = 1
		d, err := buildStateSnapshot(in)
		if err != nil {
			t.Fatal(err)
		}
		if d.Capsules[0].Mode != "agent_locked" || d.Capsules[0].LeaseHolder == nil || *d.Capsules[0].LeaseHolder != "agent-a" {
			t.Fatal("explicit lease lost")
		}
	})
}
func TestStateDiskRecorderFailsClosed(t *testing.T) {
	stateReply(t, `{"ok":true,"rc":0,"datos":[{"slot":0,"clase":"browser","estado":"listo"}]}`)
	t.Setenv("max_capsulas_por_espacio", "4")
	// A supervisor value only; the recorder is never started.
	s := &stateServer{app: &App{grab: &grabacion{fase: "recording", slot: 0, dir: "private-recording-directory"}}}
	in, err := s.readInputs()
	if err != nil {
		t.Fatal(err)
	}
	in.Timestamp = 1
	if _, err = buildStateSnapshot(in); err == nil {
		t.Fatal("legacy live disk recorder accepted")
	}
}

func TestStateFrozenSchemaCopy(t *testing.T) {
	b, err := os.ReadFile("contracts/os.cu.cockpit-state.v1.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(b)
	if hex.EncodeToString(hash[:]) != "75c5ea3d8e97a6ba725cbb4933bb1bd40094d4827e86e6176a0d554dc0b08711" {
		t.Fatal("frozen schema changed")
	}
}
func TestStateRejectsAllAbsentAndExtraFields(t *testing.T) {
	paths := [][]any{{}, {"capsules", 0}, {"capsules", 0, "geometry"}, {"capsules", 0, "rec_state"}, {"capsules", 0, "telemetry"}, {"capsules", 0, "telemetry", "mem_bytes"}, {"workspaces", 0}, {"workspaces", 0, "tree"}, {"workspaces", 0, "tree", "left"}}
	walk := func(raw any, path []any) map[string]any {
		for _, k := range path {
			switch v := k.(type) {
			case string:
				raw = raw.(map[string]any)[v]
			case int:
				raw = raw.([]any)[v]
			}
		}
		return raw.(map[string]any)
	}
	for _, path := range paths {
		var base any
		_ = json.Unmarshal([]byte(stateGoodFixture), &base)
		for key := range walk(base, path) {
			var d any
			_ = json.Unmarshal([]byte(stateGoodFixture), &d)
			delete(walk(d, path), key)
			b, _ := json.Marshal(d)
			if validateStateBytes(b) == nil {
				t.Fatalf("missing field accepted at %v/%s", path, key)
			}
		}
		var d any
		_ = json.Unmarshal([]byte(stateGoodFixture), &d)
		walk(d, path)["unexpected"] = 0
		b, _ := json.Marshal(d)
		if validateStateBytes(b) == nil {
			t.Fatalf("extra field accepted at %v", path)
		}
	}
}
func TestStateAdditionalCrossFields(t *testing.T) {
	var baseline StateDocument
	if err := json.Unmarshal([]byte(stateGoodFixture), &baseline); err != nil {
		t.Fatal(err)
	}
	mutations := []struct {
		name   string
		mutate func(*StateDocument)
	}{
		{"capsule seq", func(d *StateDocument) { d.Capsules[0].Seq++ }},
		{"identity", func(d *StateDocument) { d.Capsules[0].SlotID = "srf-ng-9" }},
		{"operator holder", func(d *StateDocument) { holder := "agent"; d.Capsules[0].LeaseHolder = &holder }},
		{"switching holder", func(d *StateDocument) {
			holder := "agent"
			d.Capsules[0].Mode = "switching"
			d.Capsules[0].LeaseHolder = &holder
		}},
		{"negative ttl", func(d *StateDocument) {
			ram := "ram"
			ttl := int64(-1)
			d.Capsules[0].Recording = StateRecording{Phase: "recording", Store: &ram, TTL: &ttl}
		}},
	}
	for _, m := range mutations {
		t.Run(m.name, func(t *testing.T) {
			var d StateDocument
			_ = json.Unmarshal([]byte(stateGoodFixture), &d)
			m.mutate(&d)
			if validateState(d) == nil {
				t.Fatal("cross-field violation accepted")
			}
		})
	}
}

func TestStateGeometryAndRevokeAreEvents(t *testing.T) {
	_, s := stateTestServer(t)
	in := stateTestInput()
	s.mu.Lock()
	s.source = func() (stateInput, error) { return in, nil }
	s.mu.Unlock()
	read := func() StateDocument {
		t.Helper()
		b, err := s.snapshot()
		if err != nil {
			t.Fatal(err)
		}
		var d StateDocument
		if err = json.Unmarshal(b, &d); err != nil {
			t.Fatal(err)
		}
		return d
	}
	initial := read()
	in.Capsules[0].LeaseEpoch++
	revoked := read()
	if revoked.Seq != initial.Seq+1 {
		t.Fatal("epoch event lost")
	}
	in.WindowMode = "tile_1_6"
	geometry := read()
	if geometry.Seq != revoked.Seq+1 || len(geometry.Workspaces) != 2 || geometry.Capsules[1].Geometry.Y != 0 {
		t.Fatal("geometry changed without event")
	}
}

func TestStateMirrorAndHTTPBytesMatchAtRest(t *testing.T) {
	a, s := stateTestServer(t)
	mirror := a.EstadoCanonico()
	// Advancing the clock alone must not produce a different document/ETag.
	s.mu.Lock()
	s.epoch = s.epoch.Add(-time.Second)
	s.mu.Unlock()
	req, err := http.NewRequest("GET", s.url, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+s.credential)
	client := &http.Client{Timeout: 3 * time.Second}
	res, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	if mirror != string(body) {
		t.Fatal("idle mirror and GET /state bytes diverged")
	}
}

func TestStateV2Wall(t *testing.T) {
	for _, mode := range []string{"tile_1_6", "column_1_3_tall", "column_1_3_dual"} {
		in := stateTestInput()
		in.WindowMode = mode
		in.Cap = 4
		for slot := 2; slot < 5; slot++ {
			in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: slot, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "read_only"})
		}
		d, err := buildStateSnapshot(in)
		if err != nil {
			t.Fatal(err)
		}
		if d.Schema != "os.cu.cockpit-state/v2" || d.Workspaces[0].Layout != "2x2" || len(d.Workspaces) != 2 {
			t.Fatalf("missing v2 wall: %s %+v", d.Schema, d.Workspaces)
		}
		for i, c := range d.Capsules[:4] {
			if c.Geometry != (StateRect{X: (i % 2) * 632, Y: (i / 2) * 512, W: 632, H: 512}) {
				t.Fatalf("wall geometry: %+v", c.Geometry)
			}
		}
		b, err := canonicalState(d)
		if err != nil {
			t.Fatal(err)
		}
		if err = validateStateBytes(b); err != nil {
			t.Fatal(err)
		}
		var raw map[string]any
		_ = json.Unmarshal(b, &raw)
		tree := raw["workspaces"].([]any)[0].(map[string]any)["tree"].(map[string]any)
		leaf := tree["left"].(map[string]any)["left"].(map[string]any)
		if leaf["scale_q16"] != float64(32358) {
			t.Fatalf("missing framebuffer fit scale: %v", leaf)
		}
		leaf["scale_q16"] = 65536
		b, _ = json.Marshal(raw)
		if validateStateBytes(b) == nil {
			t.Fatal("inconsistent scale accepted")
		}
	}
}
