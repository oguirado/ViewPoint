package harvest

import (
	"context"
	"testing"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// Fixture amounts and counts are TARGET. This test covers recovery while no
// terminal record exists, so its usage evidence lives only in the existing WAL
// EN_DUDA outcomes. No paid attempt is replayed to recover that evidence.
func TestRecoveredDoubtfulUsageEvidenceIsPreserved(t *testing.T) {
	for _, mode := range []string{"recorded-conflict", "conflicting-records", "known-before-missing"} {
		t.Run(mode,func(t *testing.T) {
			h := newPipelineHarness(t,"a")
			c := h.fixture("a","a",5)
			h.gen.items = []Candidate{c}
			if _,err := h.journal.EscribirIntencion(h.f.Wave().WaveID,c.Attempt.Key,intentionDetails{WaveSHA256:h.f.SHA256(),Attempt:c.Attempt}); err != nil { t.Fatal(err) }
			seven,nine := uint64(7),uint64(9)
			knownSeven := Cost{Microunits:&seven,Unit:"microcredits"}
			knownNine := Cost{Microunits:&nine,Unit:"microcredits"}
			record := func(usage Cost, conflict bool) {
				t.Helper()
				_,err := h.journal.EscribirDesenlace(h.f.Wave().WaveID,c.Attempt.Key,wal.DesenlaceEnDuda,wal.SrfRCMotor(6),map[string]any{"wave_sha256":h.f.SHA256(),"usage":usage,"usage_conflict":conflict})
				if err != nil { t.Fatal(err) }
			}
			x := h.extract.extractions[c.Attempt.AttemptID]
			switch mode {
			case "recorded-conflict":
				record(MissingCost(),true)
				x.Usage = knownNine
			case "conflicting-records":
				record(knownSeven,false)
				record(knownNine,false)
				x.Usage = knownNine
			case "known-before-missing":
				record(knownSeven,false)
				x.Usage = MissingCost()
			}
			h.extract.extractions[c.Attempt.AttemptID] = x
			account,err := h.pipeline().Run(context.Background())
			if ExitCode(err) != 5 || !account.Closed || !account.Accounted || len(account.Attempts) != 1 || len(h.submit.calls) != 0 { t.Fatalf("recovered failed attempt was not collected without a send: %v %+v",err,account) }
			attempt := account.Attempts[0]
			if mode == "known-before-missing" {
				if attempt.UsageConflict || attempt.Usage.Microunits == nil || *attempt.Usage.Microunits != seven || account.TotalCost.Microunits == nil || *account.TotalCost.Microunits != seven { t.Fatalf("known doubtful usage was discarded: %+v",account) }
			} else {
				if !attempt.UsageConflict || attempt.Usage.Microunits != nil || attempt.Usage.Reason != "AUSENTE" || account.TotalCost.Microunits != nil || account.TotalCost.Reason != "AUSENTE" { t.Fatalf("doubtful cost conflict became a known amount: %+v",account) }
			}
		})
	}
}
