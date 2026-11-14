package mcp

import (
	"context"
	"encoding/json"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/exec"
)

// L2Bridge runs an Action through the L2 executor. The action is first decoded
// by L2's strict decoder, so anything L3 emits is held to the frozen schema.
type L2Bridge struct{ E *exec.Executor }

func (b L2Bridge) Execute(_ context.Context, a Action) (Outcome, error) {
	raw, err := json.Marshal(a)
	if err != nil {
		return Outcome{}, err
	}
	la, err := action.DecodeAction(raw)
	if err != nil {
		return Outcome{}, err
	}
	st, err := b.E.Step(la)
	if err != nil {
		return Outcome{}, err
	}
	return Outcome{Step: st}, nil
}
