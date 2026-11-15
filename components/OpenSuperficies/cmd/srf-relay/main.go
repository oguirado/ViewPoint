package main

import (
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	"opensuperficies.local/srf/src/displaybook/relay"
)

func main() {
	var (
		listenFlag string
		targetFlag string
		publicFlag string
	)

	defaultListen := os.Getenv("RELAY_LISTEN")
	if defaultListen == "" {
		defaultListen = "0.0.0.0:9220"
	}

	defaultTarget := os.Getenv("RELAY_TARGET")
	if defaultTarget == "" {
		defaultTarget = "127.0.0.1:9222"
	}

	defaultPublic := os.Getenv("RELAY_PUBLIC")

	// U08-F5: Token is accepted exclusively via environment, never argv/flags
	token := os.Getenv("SRF_RELAY_TOKEN")
	if token == "" {
		token = os.Getenv("RELAY_TOKEN")
	}
	if token == "" {
		token = os.Getenv("SRF_TOKEN")
	}

	// U08-F1: Fail-closed if token is empty
	if token == "" {
		log.Fatalf("fatal: token is required (fail-closed)")
	}

	flag.StringVar(&listenFlag, "listen", defaultListen, "relay listen address (e.g. 0.0.0.0:9220)")
	flag.StringVar(&targetFlag, "target", defaultTarget, "target loopback CDP address (e.g. 127.0.0.1:9222)")
	flag.StringVar(&publicFlag, "public", defaultPublic, "relay public host:port for CDP rewrite")

	flag.Parse()

	if publicFlag == "" {
		host, port, err := net.SplitHostPort(listenFlag)
		if err == nil {
			if host == "" || host == "0.0.0.0" || host == "::" {
				host = "127.0.0.1"
			}
			publicFlag = net.JoinHostPort(host, port)
		} else {
			publicFlag = listenFlag
		}
	}

	cfg := relay.Config{
		ListenAddr: listenFlag,
		TargetAddr: targetFlag,
		PublicAddr: publicFlag,
		Token:      token,
	}

	r, err := relay.NewRelay(cfg)
	if err != nil {
		log.Fatalf("failed to initialize relay: %v", err)
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	go func() {
		<-sigCh
		_ = r.Close()
		os.Exit(0)
	}()

	fmt.Fprintf(os.Stderr, "srf-relay starting on %s -> %s (public=%s, auth=true)\n", cfg.ListenAddr, cfg.TargetAddr, cfg.PublicAddr)
	if err := r.Start(); err != nil {
		log.Fatalf("relay stopped with error: %v", err)
	}
}
