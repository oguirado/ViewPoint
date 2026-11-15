package workerbridge

// ValidateDef validates an owned JSON value against a frozen contract definition.
// Decode JSON with Decode first, to preserve integer and duplicate-key checks.
func ValidateDef(value any, definition string) error { return validate(value, definition) }
