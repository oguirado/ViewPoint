//go:build mutante_demux

package dockerapi

func init() {
	// Mutator: reads a 4-byte instead of 8-byte multiplexing header.
	headerSize = 4
}
