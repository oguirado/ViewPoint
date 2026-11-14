//go:build mutante_restrict_off

package microvm

func init() {
	RestrictOption = "off"
}
