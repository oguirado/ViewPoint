package cas

import (
	"bytes"
	"os"
	"strings"
	"testing"
)

const dirtyFixture = `<!DOCTYPE html>
<html>
<head><title>Harvest Probe</title></head>
<body>
<article class="prose">
<h1>Aislamiento de Recursos y Jerarquías de Control en Linux 6.x</h1>
<p>
En Linux 6.x, los namespaces y cgroup v2 resuelven problemas conceptualmente ortogonales dentro del diseño de aislamiento del kernel: los namespaces desacoplan la visión de identidades y recursos del sistema, mientras que cgroup v2 limita, distribuye y contabiliza el consumo de recursos físicos como memoria, CPU y descriptores de proceso. <cite class="citation">kernel.org</cite>
</p>
<p>
La combinación de user namespaces como base de autoridad no privilegiada junto con mount, pid y net namespaces permite confinar la superficie de ataque sin requerir capacidades reales en el anfitrión. <cite>1</cite> NoNewPrivs=1 impide que llamadas a execve() eleven privilegios de ejecución mediante bits setuid o file capabilities. [cite: pubs.opengroup.org]
</p>
<p>
Para procesos sin privilegios en el anfitrión, el montaje de procfs debe realizarse de forma asociada al PID namespace correspondiente; las opciones MS_NOSUID, MS_NODEV y MS_NOEXEC mitigan riesgos colaterales pero no alteran la visibilidad de PIDs. La gestión de grupos de control mediante cgroup.kill proporciona una terminación determinista de todos los procesos descendientes mediante SIGKILL, evitando fugas por forks concurrentes. <cite>kernel.org</cite>
</p>
<p>
El mapeo de UID/GID define identidades virtuales locales al contenedor, pero debe complementarse con límites de recursos estrictos mediante memory.max, pids.max y memory.high en el controlador cgroup v2. La referencia POSIX.1-2017 establece semántica sobre señales y descriptores, pero la arquitectura de aislamiento descansa íntegramente en las primitivas del kernel de Linux. [cite: posix-ref]
</p>
<p>
La jerarquía unificada de cgroup v2 elimina las discrepancias entre controladores que existían en v1: un proceso solo puede pertenecer a un único cgroup hoja, y los controladores se habilitan descendiendo por el árbol mediante subtree_control. Esta garantía estructural previene estados inconsistentes de contabilidad de memoria entre cgroups hermanos.
</p>
</article>

<section class="fuentes">
<h2>FUENTES</h2>
<ol>
  <li>https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html</li>
  <li>https://www.kernel.org/doc/html/latest/admin-guide/sysctl/user.html</li>
  <li>https://www.kernel.org/doc/html/latest/filesystems/proc.html</li>
  <li>https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html</li>
  <li>https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html</li>
  <li>https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap01.html</li>
  <li>https://www.kernel.org/doc/html/latest/filesystems/proc.html</li>
  <li>https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap01.html</li>
</ol>
</section>
</body>
</html>`

func TestCosechaLimpia(t *testing.T) {
	res, err := LimpiarCosecha(dirtyFixture)
	if err != nil {
		t.Fatalf("LimpiarCosecha error: %v", err)
	}

	// 1. Mutator / Contrast Check: cite debris must be purged
	if strings.Contains(res.Markdown, "<cite") || strings.Contains(res.Markdown, "[cite") {
		t.Fatalf("CONTRAST DISCRIMINATION: cite debris was not purged from markdown: %s", res.Markdown)
	}

	// 2. Mutator / Contrast Check: source deduplication
	if res.FuentesOriginales <= res.FuentesUnicas {
		t.Fatalf("test fixture must have duplicates: orig=%d, unicas=%d", res.FuentesOriginales, res.FuentesUnicas)
	}
	if len(res.Fuentes) != res.FuentesUnicas {
		t.Fatalf("CONTRAST DISCRIMINATION: sources not deduplicated: output count=%d != unicas=%d", len(res.Fuentes), res.FuentesUnicas)
	}
	if res.FuentesTotal != res.FuentesUnicas {
		t.Fatalf("CONTRAST DISCRIMINATION: emitted duplicates survived: fuentes_total=%d != fuentes_unicas=%d", res.FuentesTotal, res.FuentesUnicas)
	}
	if res.FuentesOriginales <= res.FuentesTotal {
		t.Fatalf("test fixture must have duplicates in input: orig=%d <= total=%d", res.FuentesOriginales, res.FuentesTotal)
	}

	// 3. Minimum characters requirement
	if res.Caracteres < 800 {
		t.Fatalf("caracteres %d < 800", res.Caracteres)
	}

	// 4. Content-addressed store integration
	tmpDir := t.TempDir()
	almacen, err := NuevoAlmacenCAS(tmpDir)
	if err != nil {
		t.Fatalf("NuevoAlmacenCAS error: %v", err)
	}

	hashHex, p, err := almacen.Almacenar([]byte(res.Markdown))
	if err != nil {
		t.Fatalf("Almacenar error: %v", err)
	}

	if hashHex != res.SHA256 {
		t.Fatalf("hash mismatch: almacen=%s, res=%s", hashHex, res.SHA256)
	}

	if !almacen.Existe(hashHex) {
		t.Fatalf("object %s reported not existing in CAS path %s", hashHex, p)
	}

	recuperado, err := almacen.Recuperar(hashHex)
	if err != nil {
		t.Fatalf("Recuperar error: %v", err)
	}

	if !bytes.Equal(recuperado, []byte(res.Markdown)) {
		t.Fatalf("recovered bytes do not match original")
	}
}

func TestAlmacenCAS_Basico(t *testing.T) {
	dir := t.TempDir()
	almacen, err := NuevoAlmacenCAS(dir)
	if err != nil {
		t.Fatalf("NuevoAlmacenCAS: %v", err)
	}

	data := []byte("# Documento de prueba CAS\nContenido verificado criptográficamente.\n")
	hashHex, ruta, err := almacen.Almacenar(data)
	if err != nil {
		t.Fatalf("Almacenar failed: %v", err)
	}

	if !strings.HasSuffix(ruta, ".md") {
		t.Errorf("expected .md suffix on %s", ruta)
	}

	ok, err := almacen.Verificar(hashHex)
	if err != nil || !ok {
		t.Fatalf("Verificar failed: ok=%v, err=%v", ok, err)
	}

	readBack, err := almacen.Recuperar(hashHex)
	if err != nil {
		t.Fatalf("Recuperar failed: %v", err)
	}

	if !bytes.Equal(readBack, data) {
		t.Fatalf("content mismatch")
	}
}

func TestAlmacenCAS_Corrupcion(t *testing.T) {
	dir := t.TempDir()
	almacen, err := NuevoAlmacenCAS(dir)
	if err != nil {
		t.Fatalf("NuevoAlmacenCAS: %v", err)
	}

	data := []byte("Contenido inmutable que será corrompido.")
	hashHex, ruta, err := almacen.Almacenar(data)
	if err != nil {
		t.Fatalf("Almacenar failed: %v", err)
	}

	// Corrupt content on disk
	corrupted := []byte("Contenido inmutable que HA SIDO ALTERADO.")
	if err := os.WriteFile(ruta, corrupted, 0644); err != nil {
		t.Fatalf("failed to corrupt file: %v", err)
	}

	// Cold verification must detect corruption
	ok, err := almacen.Verificar(hashHex)
	if ok || err == nil {
		t.Fatalf("expected verification failure on corrupted file, got ok=%v err=%v", ok, err)
	}

	// Recovery must fail with ErrCorrupcionCAS
	_, err = almacen.Recuperar(hashHex)
	if err == nil {
		t.Fatalf("expected error on recovering corrupted file, got nil")
	}
}

func TestDeduplicacionFuentes(t *testing.T) {
	input := `## FUENTES
1. https://kernel.org/a
2. https://kernel.org/b
3. https://kernel.org/a
4. https://kernel.org/c
5. https://kernel.org/b
`
	res, err := LimpiarCosecha(input)
	if err != nil {
		t.Fatalf("LimpiarCosecha failed: %v", err)
	}

	if !mutanteSinLimpieza {
		if res.FuentesOriginales != 5 {
			t.Errorf("expected 5 original sources before dedup, got %d", res.FuentesOriginales)
		}
		if res.FuentesTotal != 3 {
			t.Errorf("expected 3 emitted sources, got %d", res.FuentesTotal)
		}
		if res.FuentesUnicas != 3 {
			t.Errorf("expected 3 unique sources, got %d", res.FuentesUnicas)
		}
		expected := []string{"https://kernel.org/a", "https://kernel.org/b", "https://kernel.org/c"}
		if len(res.Fuentes) != len(expected) {
			t.Fatalf("expected %d sources, got %d", len(expected), len(res.Fuentes))
		}
		for i, exp := range expected {
			if res.Fuentes[i] != exp {
				t.Errorf("expected source %d to be %s, got %s", i, exp, res.Fuentes[i])
			}
		}
	}
}

func TestPurgaCitas(t *testing.T) {
	input := `<p>Prueba con <cite class="x">debris</cite> y <cite>otra</cite> y [cite: 3] y [cite].</p>`
	res, err := LimpiarCosecha(input)
	if err != nil {
		t.Fatalf("LimpiarCosecha: %v", err)
	}

	if !mutanteSinLimpieza {
		if strings.Contains(res.Markdown, "cite") {
			t.Errorf("markdown still contains cite: %s", res.Markdown)
		}
		if res.CitasEliminadas < 4 {
			t.Errorf("expected at least 4 cites eliminated, got %d", res.CitasEliminadas)
		}
	}
}

func TestPurgaCitas_EntidadesEscapadas(t *testing.T) {
	// Tests U12-F2: single-escaped and double-escaped cite tags must be completely purged
	input := `<p>Paragraph with &lt;cite class="x"&gt;escaped debris&lt;/cite&gt; and &amp;lt;cite class="y"&amp;gt;double escaped debris&amp;lt;/cite&amp;gt; and standalone &lt;cite&gt;.</p>`
	res, err := LimpiarCosecha(input)
	if err != nil {
		t.Fatalf("LimpiarCosecha failed: %v", err)
	}

	if !mutanteSinLimpieza {
		if strings.Contains(res.Markdown, "<cite") || strings.Contains(res.Markdown, "&lt;cite") {
			t.Fatalf("escaped cite was not purged: %s", res.Markdown)
		}
		if strings.Contains(res.Markdown, "debris") {
			t.Fatalf("escaped cite content was not purged: %s", res.Markdown)
		}
	}
}

func TestPurgaCitas_MarcadorTruncado(t *testing.T) {
	// Tests U12-F3: truncated cite bracket markers without closing bracket must be purged
	input := `<p>Paragraph with truncated cite marker [cite: posix-ref and text continuing.</p>`
	res, err := LimpiarCosecha(input)
	if err != nil {
		t.Fatalf("LimpiarCosecha failed: %v", err)
	}

	if !mutanteSinLimpieza {
		if strings.Contains(res.Markdown, "[cite") {
			t.Fatalf("truncated cite marker was not purged: %s", res.Markdown)
		}
	}
}

func TestFuentes_TresCamposIndependientes(t *testing.T) {
	// Tests U12-F1 / contract: fuentes_originales (pre-dedup input), fuentes_total (emitted), fuentes_unicas (distinct emitted)
	input := `## FUENTES
1. https://kernel.org/dup
2. https://kernel.org/dup
3. https://kernel.org/unique
`
	res, err := LimpiarCosecha(input)
	if err != nil {
		t.Fatalf("LimpiarCosecha failed: %v", err)
	}

	if !mutanteSinLimpieza {
		if res.FuentesOriginales != 3 {
			t.Fatalf("fuentes_originales must be 3, got %d", res.FuentesOriginales)
		}
		if res.FuentesTotal != 2 {
			t.Fatalf("fuentes_total must be emitted count 2, got %d", res.FuentesTotal)
		}
		if res.FuentesUnicas != 2 {
			t.Fatalf("fuentes_unicas must be distinct count 2, got %d", res.FuentesUnicas)
		}
		if res.FuentesTotal != res.FuentesUnicas {
			t.Fatalf("clause .fuentes_unicas==.fuentes_total must hold on nominal clean harvest: total=%d, unicas=%d", res.FuentesTotal, res.FuentesUnicas)
		}
	} else {
		if res.FuentesTotal <= res.FuentesUnicas {
			t.Fatalf("mutant must allow duplicates to survive into output: total=%d <= unicas=%d", res.FuentesTotal, res.FuentesUnicas)
		}
	}
}
