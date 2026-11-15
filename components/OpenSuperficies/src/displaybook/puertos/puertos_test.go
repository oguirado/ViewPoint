package puertos

import (
	"errors"
	"testing"
)

func TestPlanPuertosSlotCero(t *testing.T) {
	plan := NuevoPlanPuertos()
	par, err := plan.Par(0)
	if err != nil {
		t.Fatalf("Par(0) error inesperado: %v", err)
	}
	if par.Slot != 0 {
		t.Errorf("Slot 0: esperado 0, obtenido %d", par.Slot)
	}
	if par.Xpra != 10020 {
		t.Errorf("Slot 0 Xpra: esperado 10020, obtenido %d", par.Xpra)
	}
	if par.CDP != 9220 {
		t.Fatalf("Slot 0 CDP: esperado 9220, obtenido %d (mutante_formula_v1 activo si es 9240)", par.CDP)
	}
}

func TestPlanPuertosLimites(t *testing.T) {
	plan := NuevoPlanPuertos()

	// Slot 79 (límite superior de N=80)
	par79, err := plan.Par(79)
	if err != nil {
		t.Fatalf("Par(79) error inesperado: %v", err)
	}
	if par79.Slot != 79 {
		t.Errorf("Slot 79: esperado 79, obtenido %d", par79.Slot)
	}
	if par79.Xpra != 10099 {
		t.Errorf("Slot 79 Xpra: esperado 10099, obtenido %d", par79.Xpra)
	}
	if par79.CDP != 9299 {
		t.Errorf("Slot 79 CDP: esperado 9299, obtenido %d", par79.CDP)
	}

	// Slot 80 (fuera de límites)
	if _, err := plan.Par(80); err == nil {
		t.Fatalf("Par(80) debía retornar error por sobrepasar límite n=%d", plan.N)
	}

	// Slot 100 (fuera de límites)
	if _, err := plan.Par(100); err == nil {
		t.Fatalf("Par(100) debía retornar error por sobrepasar límite n=%d", plan.N)
	}
}

func TestReservarSlotExclusivo(t *testing.T) {
	tmpDir := t.TempDir()
	const slot = 42

	// 1. Primera reserva exclusiva
	lock1, err := ReservarSlot(tmpDir, slot)
	if err != nil {
		t.Fatalf("primera reserva falló inesperadamente: %v", err)
	}
	if lock1 == nil {
		t.Fatalf("primera reserva retornó lock nil")
	}

	// 2. Intento de reserva concurrente/duplicada sobre el mismo slot sin haber liberado lock1
	lock2, err2 := ReservarSlot(tmpDir, slot)
	if err2 == nil {
		if lock2 != nil {
			_ = lock2.Release()
		}
		_ = lock1.Release()
		t.Fatalf("segunda reserva debía fallar por colisión exclusiva, pero tuvo éxito")
	}
	if !IsLockCollision(err2) {
		_ = lock1.Release()
		t.Fatalf("error de segunda reserva no es colisión esperada (rc %d): %v", ExitCodeLockCollision, err2)
	}
	var collErr *LockCollisionError
	if errors.As(err2, &collErr) {
		if collErr.Slot != slot {
			t.Errorf("LockCollisionError.Slot esperado %d, obtenido %d", slot, collErr.Slot)
		}
	}

	// 3. Liberar la primera reserva
	if err := lock1.Release(); err != nil {
		t.Fatalf("Release de lock1 falló: %v", err)
	}

	// 4. Re-reservar el slot tras la liberación: debe tener éxito
	lock3, err3 := ReservarSlot(tmpDir, slot)
	if err3 != nil {
		t.Fatalf("re-reserva tras liberación falló: %v", err3)
	}
	if lock3 == nil {
		t.Fatalf("re-reserva tras liberación retornó lock nil")
	}

	if err := lock3.Release(); err != nil {
		t.Fatalf("Release de lock3 falló: %v", err)
	}
}
