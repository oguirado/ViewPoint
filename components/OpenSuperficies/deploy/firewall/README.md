# Open-mode egress firewall (draft; operator installs)

`srf-identity-surface start --kind browser --net open` refuses (code 3, "open egress firewall
not installed") unless `/run/srf-open-egress/rules.sha256` is a root-owned file that is not
group- or other-writable, inside a root-owned directory with the same restriction, and holds the
sha256 of `srf-open-egress.rules` as compiled into the binary. `apply.sh` writes that witness
after it has applied the rules.

What the rules do, for traffic from the open pool 172.29.0.0/16 (bridges `srfo+`):
- `SRF-OPEN-EGRESS` (jumped from `DOCKER-USER`): DNS to 100.90.1.1 and 100.100.1.1 is accepted.
  Private, loopback, link-local, CGNAT, benchmark, multicast and reserved ranges are dropped.
  Everything else returns to Docker's own rules.
- `SRF-OPEN-INPUT` (jumped from `INPUT -i srfo+`): only established and related traffic is
  accepted. This blocks host services on the bridge gateway IP, which FORWARD never sees.
- IPv6: FORWARD and INPUT from `srfo+` are dropped.

## Install (run each line with sudo)

```
install -d -m 0755 -o root -g root /usr/local/libexec/srf-open-egress
install -m 0755 -o root -g root deploy/firewall/apply.sh /usr/local/libexec/srf-open-egress/apply.sh
install -m 0644 -o root -g root deploy/firewall/srf-open-egress.rules /usr/local/libexec/srf-open-egress/srf-open-egress.rules
install -m 0644 -o root -g root deploy/firewall/srf-open-egress6.rules /usr/local/libexec/srf-open-egress/srf-open-egress6.rules
install -m 0644 -o root -g root deploy/firewall/srf-open-egress.service /etc/systemd/system/srf-open-egress.service
systemctl daemon-reload
systemctl enable --now srf-open-egress.service
cat /run/srf-open-egress/rules.sha256
```

Uninstall: `systemctl disable --now srf-open-egress.service` (runs `apply.sh remove`, which deletes
the jumps, its two chains and the witness).

## Residual risk

A firewall reload that flushes these rules (`iptables -F`, `nft flush ruleset`, a firewalld or ufw
reload) leaves a stale witness, so open starts are admitted without the rules until
`systemctl restart srf-open-egress.service` runs. A Docker restart is covered: the unit is
`PartOf=docker.service`. Recheck with `iptables -S SRF-OPEN-EGRESS` after any firewall change.

## Live checks (operator or a later drill, from inside an open capsule)

- `curl -sS -o /dev/null -w '%{http_code}' https://example.org/` succeeds.
- `curl -m 5 http://192.168.1.1/` fails (LAN dropped).
- `curl -m 5 http://<gateway IP of srf-open-<capsule>>/` fails (host input dropped).
- `curl -m 5 http://100.90.1.1:80/` fails (only port 53 to the resolvers is accepted).
- `getent hosts example.org` resolves.

Open mode also needs the rebuilt identity image (entrypoint with SRF_NET_MODE=open, branch unit/net-modes-image,
operator build with builder_approval.json). Until the commit that pins that image lands, `start --net open` refuses
with code 3 even when the witness is installed.
