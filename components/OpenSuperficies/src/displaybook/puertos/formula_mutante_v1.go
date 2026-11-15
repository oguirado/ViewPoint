//go:build mutante_formula_v1

package puertos

func init() {
	calcularCDP = func(cdpBase uint16, xpra uint16, slot uint16) uint16 {
		return xpra - 10000 + 9220 // R-15 v1 formula: maps slot 0 to 9240
	}
}
