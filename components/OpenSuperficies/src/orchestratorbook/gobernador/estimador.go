package gobernador

// EstimarBrowserMiB computes the memory reservation in MiB for a browser capsule
// with P pages according to the sealed formula (ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md §5.3):
//
//	m_browser(P) = 64 * ceil((733 + 183*P + 256) / 64) MiB
//
// For P = 6: 2,112 MiB (1,646 MiB measured footprint fits with 466 MiB margin).
// For P = 3 (ephemeral): 1,600 MiB.
func EstimarBrowserMiB(paginas int) uint64 {
	if paginas < 1 {
		paginas = 1
	}
	raw := 733 + 183*paginas + 256
	bloques := (raw + 63) / 64
	return uint64(bloques * 64)
}

// EstimarDesktopMiB returns the target reservation for a desktop capsule (2,048 MiB).
func EstimarDesktopMiB() uint64 {
	return 2048
}

// EstimarMicroVMMiB returns the target reservation for a microvm capsule:
// guest RAM + 256 MiB VMM overhead.
func EstimarMicroVMMiB(guestRAMMiB uint64) uint64 {
	if guestRAMMiB == 0 {
		guestRAMMiB = 4096
	}
	return guestRAMMiB + 256
}

// EstimarTerminalMiB returns the memory reservation for a terminal jail capsule.
func EstimarTerminalMiB(perfil string) uint64 {
	switch perfil {
	case "obrero":
		return 4096
	case "agente":
		return 2048
	default:
		return 512
	}
}

// EstimarReservaMiB calculates memory reservation in MiB for any capsule class.
func EstimarReservaMiB(clase string, paginas int) uint64 {
	switch clase {
	case "browser":
		return EstimarBrowserMiB(paginas)
	case "desktop":
		return EstimarDesktopMiB()
	case "microvm":
		return EstimarMicroVMMiB(4096)
	case "terminal":
		return EstimarTerminalMiB("agente")
	default:
		return EstimarBrowserMiB(paginas)
	}
}

// EstimarReservaBytes returns the memory reservation in bytes for a given class and page count.
func EstimarReservaBytes(clase string, paginas int) uint64 {
	return EstimarReservaMiB(clase, paginas) * 1024 * 1024
}
