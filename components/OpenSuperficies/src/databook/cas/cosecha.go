package cas

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"html"
	"regexp"
	"strings"
)

var mutanteSinLimpieza = false

// ResultadoLimpieza holds the parsed, purified harvest and its telemetry metrics.
type ResultadoLimpieza struct {
	Cuerpo            string   `json:"cuerpo"`
	Markdown          string   `json:"markdown"`
	Fuentes           []string `json:"fuentes"`
	Caracteres        int      `json:"caracteres"`
	FuentesTotal      int      `json:"fuentes_total"`
	FuentesUnicas     int      `json:"fuentes_unicas"`
	FuentesOriginales int      `json:"fuentes_originales"`
	CitasEliminadas   int      `json:"citas_eliminadas"`
	SHA256            string   `json:"sha256"`
}

var (
	reURL               = regexp.MustCompile(`https?://[^\s<>"']+`)
	reCiteTagWithInner  = regexp.MustCompile(`(?is)<cite[^>]*>.*?</cite>`)
	reCiteTagStandalone = regexp.MustCompile(`(?i)</?cite[^>]*>`)
	reCiteBracket      = regexp.MustCompile(`(?i)\[cite(?:[^\]\r\n<]*\]|:[^\]\r\n<]*|[^\]\r\n<]*)`)
	reCiteUnicode      = regexp.MustCompile(`\x{e200}cite[^\x{e201}]*\x{e201}`)
	reUnicodeMarker    = regexp.MustCompile(`[\x{e200}\x{e201}\x{e202}]`)

	reScript           = regexp.MustCompile(`(?is)<script[^>]*>.*?</script>`)
	reStyle            = regexp.MustCompile(`(?is)<style[^>]*>.*?</style>`)
	reHead             = regexp.MustCompile(`(?is)<head[^>]*>.*?</head>`)
	reHeading          = regexp.MustCompile(`(?is)<h([1-6])[^>]*>(.*?)</h[1-6]>`)
	reParagraph        = regexp.MustCompile(`(?is)<p[^>]*>(.*?)</p>`)
	reListItem         = regexp.MustCompile(`(?is)<li[^>]*>(.*?)</li>`)
	reBreak            = regexp.MustCompile(`(?i)<br\s*/?>`)
	reRule             = regexp.MustCompile(`(?i)<hr\s*/?>`)
	reAllTags          = regexp.MustCompile(`<[^>]+>`)
	reMultiNL          = regexp.MustCompile(`\n{3,}`)
	reEmittedEntry     = regexp.MustCompile(`(?m)^\s*\d+\.\s+\S+`)
)

// LimpiarCosecha parses a raw harvest (HTML or Markdown), purges all citation debris,
// deduplicates cited sources, and renders canonical, content-addressable markdown.
// When compiled under the mutante_sin_limpieza build tag, cite purging and deduplication are bypassed.
func LimpiarCosecha(contenido string) (*ResultadoLimpieza, error) {
	// 1. Separate sources section from harvest body
	cuerpoRaw, fuentesRaw := separarSecciones(contenido)

	// 2. Extract raw source URLs
	urlsEncontradas := reURL.FindAllString(fuentesRaw, -1)
	fuentesOriginales := len(urlsEncontradas)

	var fuentesFinales []string

	if mutanteSinLimpieza {
		// Mutator: bypass source deduplication
		fuentesFinales = urlsEncontradas
	} else {
		// Nominal: deduplicate sources preserving first-seen order
		seen := make(map[string]bool)
		for _, u := range urlsEncontradas {
			u = strings.TrimSpace(u)
			u = strings.TrimRight(u, ".,;:)")
			if u == "" {
				continue
			}
			if !seen[u] {
				seen[u] = true
				fuentesFinales = append(fuentesFinales, u)
			}
		}
	}

	// 3. Process harvest body and cite purge
	citasEliminadas := 0
	cuerpoLimpio := cuerpoRaw

	if !mutanteSinLimpieza {
		var inicial int
		cuerpoLimpio, inicial = purgarCitas(cuerpoLimpio)
		citasEliminadas += inicial
	}

	// 4. HTML to Markdown normalization
	cuerpoMarkdown, citasExtra := normalizarMarkdown(cuerpoLimpio, mutanteSinLimpieza)
	citasEliminadas += citasExtra

	// 5. Construct canonical Markdown document
	cuerpoLimpioMD := strings.TrimRight(cuerpoMarkdown, "\r\n- \t")
	var mdBuilder strings.Builder
	mdBuilder.WriteString(cuerpoLimpioMD)

	if len(fuentesFinales) > 0 {
		mdBuilder.WriteString("\n\n---\n\n## FUENTES\n\n")
		for i, f := range fuentesFinales {
			mdBuilder.WriteString(fmt.Sprintf("%d. %s\n", i+1, f))
		}
	}

	finalMarkdown := strings.TrimSpace(mdBuilder.String()) + "\n"
	charCount := len([]rune(finalMarkdown))

	// 6. Independent computation of emitted source metrics from finalMarkdown
	_, fuentesMD := separarSecciones(finalMarkdown)

	// fuentes_total: count of source entries actually emitted in the output document
	fuentesTotal := len(reEmittedEntry.FindAllString(fuentesMD, -1))

	// fuentes_unicas: count of distinct sources among the entries actually emitted
	emittedSeen := make(map[string]struct{})
	for _, u := range reURL.FindAllString(fuentesMD, -1) {
		u = strings.TrimSpace(u)
		u = strings.TrimRight(u, ".,;:)")
		if u != "" {
			emittedSeen[u] = struct{}{}
		}
	}
	fuentesUnicas := len(emittedSeen)

	sum := sha256.Sum256([]byte(finalMarkdown))
	hashHex := hex.EncodeToString(sum[:])

	return &ResultadoLimpieza{
		Cuerpo:            cuerpoMarkdown,
		Markdown:          finalMarkdown,
		Fuentes:           fuentesFinales,
		Caracteres:        charCount,
		FuentesTotal:      fuentesTotal,
		FuentesUnicas:     fuentesUnicas,
		FuentesOriginales: fuentesOriginales,
		CitasEliminadas:   citasEliminadas,
		SHA256:            hashHex,
	}, nil
}

func separarSecciones(contenido string) (string, string) {
	// Look for sources section delimiters (HTML section/div with fuentes, heading FUENTES, or markdown ## FUENTES)
	reSectionFuentes := regexp.MustCompile(`(?is)<(?:section|div)[^>]*class=["']?[^"']*fuentes[^"']*["']?[^>]*>(.*?)(?:</(?:section|div)>|$)`)
	if loc := reSectionFuentes.FindStringSubmatchIndex(contenido); loc != nil {
		cuerpo := contenido[:loc[0]]
		fuentes := contenido[loc[2]:loc[3]]
		return cuerpo, fuentes
	}

	reHeadingFuentes := regexp.MustCompile(`(?is)<h[1-6][^>]*>\s*FUENTES\s*</h[1-6]>([\s\S]*)$`)
	if loc := reHeadingFuentes.FindStringSubmatchIndex(contenido); loc != nil {
		cuerpo := contenido[:loc[0]]
		fuentes := contenido[loc[2]:loc[3]]
		return cuerpo, fuentes
	}

	reMarkdownFuentes := regexp.MustCompile(`(?i)(?:^|\n)##\s*FUENTES\s*\r?\n([\s\S]*)$`)
	if loc := reMarkdownFuentes.FindStringSubmatchIndex(contenido); loc != nil {
		cuerpo := contenido[:loc[0]]
		// Check for preceding horizontal rule
		cuerpo = strings.TrimRight(cuerpo, "\r\n- \t")
		fuentes := contenido[loc[2]:loc[3]]
		return cuerpo, fuentes
	}

	return contenido, ""
}

func purgarCitas(texto string) (string, int) {
	citasEliminadas := 0

	citasEliminadas += len(reCiteTagWithInner.FindAllString(texto, -1))
	texto = reCiteTagWithInner.ReplaceAllString(texto, "")

	citasEliminadas += len(reCiteTagStandalone.FindAllString(texto, -1))
	texto = reCiteTagStandalone.ReplaceAllString(texto, "")

	citasEliminadas += len(reCiteBracket.FindAllString(texto, -1))
	texto = reCiteBracket.ReplaceAllString(texto, "")

	citasEliminadas += len(reCiteUnicode.FindAllString(texto, -1))
	texto = reCiteUnicode.ReplaceAllString(texto, "")

	texto = reUnicodeMarker.ReplaceAllString(texto, "")

	return texto, citasEliminadas
}

func normalizarMarkdown(input string, preservarCites bool) (string, int) {
	s := input
	citasExtra := 0

	// Strip script, style, and head
	s = reScript.ReplaceAllString(s, "")
	s = reStyle.ReplaceAllString(s, "")
	s = reHead.ReplaceAllString(s, "")

	// Convert Headings
	s = reHeading.ReplaceAllStringFunc(s, func(m string) string {
		sub := reHeading.FindStringSubmatch(m)
		if len(sub) == 3 {
			level := sub[1][0] - '0'
			prefix := strings.Repeat("#", int(level))
			inner := strings.TrimSpace(reAllTags.ReplaceAllString(sub[2], ""))
			return fmt.Sprintf("\n\n%s %s\n\n", prefix, inner)
		}
		return m
	})

	// Convert paragraphs
	s = reParagraph.ReplaceAllString(s, "\n\n$1\n\n")

	// Convert list items
	s = reListItem.ReplaceAllString(s, "\n- $1\n")

	// Breaks and rules
	s = reBreak.ReplaceAllString(s, "\n")
	s = reRule.ReplaceAllString(s, "\n\n---\n\n")

	// Strip all remaining tags (preserving cite tags when mutant is enabled)
	if preservarCites {
		s = reAllTags.ReplaceAllStringFunc(s, func(tag string) string {
			lower := strings.ToLower(tag)
			if strings.HasPrefix(lower, "<cite") || strings.HasPrefix(lower, "</cite") {
				return tag
			}
			return ""
		})
	} else {
		s = reAllTags.ReplaceAllString(s, "")
	}

	// Unescape HTML entities iteratively to resolve multi-level escaping
	for i := 0; i < 5; i++ {
		unescaped := html.UnescapeString(s)
		if unescaped == s {
			break
		}
		s = unescaped
	}

	// Secondary cite purge after unescaping to eliminate resurrected or unescaped cite debris
	if !preservarCites {
		var extra int
		s, extra = purgarCitas(s)
		citasExtra += extra
		s = reAllTags.ReplaceAllString(s, "")
	}

	// Normalize whitespace
	lines := strings.Split(s, "\n")
	var cleanedLines []string
	for _, l := range lines {
		cleanedLines = append(cleanedLines, strings.TrimSpace(l))
	}
	s = strings.Join(cleanedLines, "\n")
	s = reMultiNL.ReplaceAllString(s, "\n\n")

	return strings.TrimSpace(s), citasExtra
}
