// Height index of the multislot stack (spec 2.2): a Fenwick (binary indexed) tree over the height of every card.
//
// The stack never virtualizes the live surface (spec 2.1), but the cards change height all the time: a folded card is
// 0 px tall, a card of another virtual screen is 0 px tall, the layout of a window mode fixes the rest. Paging, the
// roving current index and the pager all need two answers about that column of heights:
//
//   offsetDe(i)          where card i starts (the sum of the heights before it)     O(log n)
//   indicePorOffset(y)   which card contains the scroll offset y                     O(log n)
//
// and every height change must cost O(log n) too, so `fijar` touches only the nodes above the changed card.
// Heights are non-negative finite numbers (pixels in practice); a zero-height card owns no offset, so
// `indicePorOffset` never answers a folded card. Nothing here reads the DOM: the component compares this index with
// the layout the browser computes, and scripts/ng/medir-scroll.mjs measures that agreement from outside the page.

/** A height that cannot be summed: negative, NaN or infinite. */
function comprobarAltura(altura: number, donde: string): void {
  if (!Number.isFinite(altura) || altura < 0) {
    throw new RangeError(`${donde}: a height must be a finite number >= 0, got ${String(altura)}`);
  }
}

export class FenwickArbol {
  /** 1-based tree: `arbol[j]` is the sum of the heights of the cards in (j - lowbit(j), j]. */
  private arbol: Float64Array;
  /** Height of every card, 0-based, so that `altura(i)` and the delta of `fijar` are O(1). */
  private valores: Float64Array;
  private n: number;
  /** Largest power of two not above n: the first step of the descent of `indicePorOffset`. */
  private tope: number;
  private generacionActual = 0;
  /** Tree nodes touched by the last `offsetDe`, `indicePorOffset` or `fijar`: the O(log n) claim, observable. */
  ultimoCoste = 0;

  private constructor(valores: Float64Array) {
    this.n = valores.length;
    this.valores = valores;
    this.arbol = new Float64Array(this.n + 1);
    this.tope = 0;
    this.reconstruir();
  }

  /** Builds the tree of `alturas` in O(n). */
  static desdeAlturas(alturas: readonly number[]): FenwickArbol {
    const valores = new Float64Array(alturas.length);
    alturas.forEach((h, i) => {
      comprobarAltura(h, `alturas[${i}]`);
      valores[i] = h;
    });
    return new FenwickArbol(valores);
  }

  private reconstruir(): void {
    this.arbol = new Float64Array(this.n + 1);
    for (let i = 1; i <= this.n; i++) {
      this.arbol[i] += this.valores[i - 1];
      const padre = i + (i & -i);
      if (padre <= this.n) this.arbol[padre] += this.arbol[i];
    }
    this.tope = 0;
    if (this.n > 0) {
      this.tope = 1;
      while (this.tope * 2 <= this.n) this.tope *= 2;
    }
    this.generacionActual++;
  }

  /** Number of cards. */
  get longitud(): number {
    return this.n;
  }

  /** Bumped by every change of any height (and every rebuild): a reactive reader compares it to know the index moved. */
  get generacion(): number {
    return this.generacionActual;
  }

  /** Height of card i. */
  altura(i: number): number {
    this.comprobarIndice(i, this.n - 1, 'altura');
    return this.valores[i];
  }

  /** Sets the height of card i in O(log n). */
  fijar(i: number, altura: number): void {
    this.comprobarIndice(i, this.n - 1, 'fijar');
    comprobarAltura(altura, 'fijar');
    const delta = altura - this.valores[i];
    this.ultimoCoste = 0;
    if (delta === 0) return;
    this.valores[i] = altura;
    for (let j = i + 1; j <= this.n; j += j & -j) {
      this.arbol[j] += delta;
      this.ultimoCoste++;
    }
    this.generacionActual++;
  }

  /** Where card i starts: the sum of the heights of cards [0, i). Accepts i = n (the total height). O(log n). */
  offsetDe(i: number): number {
    this.comprobarIndice(i, this.n, 'offsetDe');
    let suma = 0;
    this.ultimoCoste = 0;
    for (let j = i; j > 0; j -= j & -j) {
      suma += this.arbol[j];
      this.ultimoCoste++;
    }
    return suma;
  }

  /** Total height of the column. */
  total(): number {
    return this.offsetDe(this.n);
  }

  /**
   * The card that contains the offset `y` (0 <= y < total): the largest i with offsetDe(i) <= y. A card of height 0
   * contains no offset, so it is never answered. Returns `longitud` when `y` is at or past the end. O(log n).
   */
  indicePorOffset(y: number): number {
    if (Number.isNaN(y) || y < 0) throw new RangeError(`indicePorOffset: the offset must be a number >= 0, got ${String(y)}`);
    let pos = 0;
    let resto = y;
    this.ultimoCoste = 0;
    for (let paso = this.tope; paso > 0; paso = Math.floor(paso / 2)) {
      const siguiente = pos + paso;
      if (siguiente > this.n) continue;
      this.ultimoCoste++;
      if (this.arbol[siguiente] <= resto) {
        pos = siguiente;
        resto -= this.arbol[siguiente];
      }
    }
    return pos;
  }

  /**
   * Makes the tree hold `alturas`: O(k log n) for the k heights that differ, a rebuild when the number of cards
   * changed. Returns the number of heights that changed (a rebuild counts every card).
   */
  sincronizar(alturas: readonly number[]): number {
    // Validate everything before touching anything: a bad height must not leave the tree half updated.
    alturas.forEach((h, i) => comprobarAltura(h, `alturas[${i}]`));
    if (alturas.length !== this.n) {
      this.n = alturas.length;
      this.valores = Float64Array.from(alturas);
      this.reconstruir();
      return this.n;
    }
    let cambiadas = 0;
    for (let i = 0; i < this.n; i++) {
      if (this.valores[i] !== alturas[i]) {
        this.fijar(i, alturas[i]);
        cambiadas++;
      }
    }
    return cambiadas;
  }

  private comprobarIndice(i: number, maximo: number, donde: string): void {
    if (!Number.isInteger(i) || i < 0 || i > maximo) {
      throw new RangeError(`${donde}: index ${String(i)} is outside [0, ${maximo}]`);
    }
  }
}
