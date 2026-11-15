//go:build cockpit

package main

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// SRF2 frame codec, the Go side of cockpit/frontend/src/lib/terminal/wsstream.ts (ESPEC section 10): a fixed
// little-endian header of 32 bytes, then `count` records of `stride` bytes. PTY bytes travel with stride 1.
//
//	0..3 magic "SRF2"   4..5 version u16 (1)   6..7 stride u16   8..11 count u32   12..15 seq u32   16..19 ms u32
//	20..31 reserved, zero
const (
	srf2Version   = 1
	srf2Cabecera  = 32
	srf2StridePty = 1
)

var srf2Magia = [4]byte{0x53, 0x52, 0x46, 0x32}

var errTramaSRF2 = errors.New("srf2: bad frame")

// codificarTrama builds one SRF2 frame of stride 1 around cuerpo.
func codificarTrama(cuerpo []byte, seq, ms uint32) []byte {
	salida := make([]byte, srf2Cabecera+len(cuerpo))
	copy(salida[0:4], srf2Magia[:])
	binary.LittleEndian.PutUint16(salida[4:6], srf2Version)
	binary.LittleEndian.PutUint16(salida[6:8], srf2StridePty)
	binary.LittleEndian.PutUint32(salida[8:12], uint32(len(cuerpo)))
	binary.LittleEndian.PutUint32(salida[12:16], seq)
	binary.LittleEndian.PutUint32(salida[16:20], ms)
	copy(salida[srf2Cabecera:], cuerpo)
	return salida
}

// tramaSRF2 is one decoded frame; Cuerpo aliases the input.
type tramaSRF2 struct {
	Stride uint16
	Cuenta uint32
	Seq    uint32
	Ms     uint32
	Cuerpo []byte
}

// decodificarTrama reads one SRF2 frame; anything that does not hold the contract is refused, never guessed.
func decodificarTrama(datos []byte) (tramaSRF2, error) {
	if len(datos) < srf2Cabecera {
		return tramaSRF2{}, fmt.Errorf("%w: %d bytes is shorter than the %d-byte header", errTramaSRF2, len(datos), srf2Cabecera)
	}
	if [4]byte(datos[0:4]) != srf2Magia {
		return tramaSRF2{}, fmt.Errorf("%w: bad magic", errTramaSRF2)
	}
	if v := binary.LittleEndian.Uint16(datos[4:6]); v != srf2Version {
		return tramaSRF2{}, fmt.Errorf("%w: unsupported version %d", errTramaSRF2, v)
	}
	stride := binary.LittleEndian.Uint16(datos[6:8])
	if stride == 0 {
		return tramaSRF2{}, fmt.Errorf("%w: stride 0", errTramaSRF2)
	}
	for i := 20; i < srf2Cabecera; i++ {
		if datos[i] != 0 {
			return tramaSRF2{}, fmt.Errorf("%w: reserved byte %d is not zero", errTramaSRF2, i)
		}
	}
	cuenta := binary.LittleEndian.Uint32(datos[8:12])
	largo := uint64(len(datos) - srf2Cabecera)
	if uint64(cuenta)*uint64(stride) != largo {
		return tramaSRF2{}, fmt.Errorf("%w: count %d x stride %d != %d body bytes", errTramaSRF2, cuenta, stride, largo)
	}
	return tramaSRF2{Stride: stride, Cuenta: cuenta, Seq: binary.LittleEndian.Uint32(datos[12:16]), Ms: binary.LittleEndian.Uint32(datos[16:20]), Cuerpo: datos[srf2Cabecera:]}, nil
}
