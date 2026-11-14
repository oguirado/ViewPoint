package capture

import "opensuperficies.local/srf/src/quarantineoracle"

// QuarantineFromOracle maps the verdict of src/quarantineoracle (doors A-N) into vqa.capsule/v1.
func QuarantineFromOracle(r quarantineoracle.CuarentenaResultado) Quarantine {
	ds := make([]Door, len(r.Puertas))
	for i, p := range r.Puertas {
		ds[i] = Door{Puerta: p.Puerta, Verde: p.Verde}
	}
	q := QuarantineFromDoors(ds)
	if !r.Verde {
		q.RC = 5
	}
	return q
}
