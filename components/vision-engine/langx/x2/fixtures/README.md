# LANG-X Experiment X2 Fixtures

Shared HTML UI fixtures and execution step scenarios for LANG-X experiment X2.

## Fixture Pages & Seed Contract Coverage

1. **`p1_form.html` (`p1_form.steps.json`)**
   - **Seed Contract:** Form controls interaction including text input `#q`, checkbox `#agree`, select `#color` with 3 options (`red`, `green`, `blue`), and submit button `#go` writing `#q` value into `div #out`.
   - **Operations Exercised:** `type`, `click`.
   - **Expected State Count:** 4 states (Initial state -> after typing into `#q` -> after toggling `#agree` -> after clicking `#go` and output rendering).

2. **`p2_list.html` (`p2_list.steps.json`)**
   - **Seed Contract:** Scroll container `#list` with 60 initial items (`a.row-N` with href fragment `#row-N`) and append button `#more` appending 5 additional items.
   - **Operations Exercised:** `click`, `wait`.
   - **Expected State Count:** 3 states (Initial state with 60 rows -> after clicking `#more` -> after waiting for `.row-64` to exist in DOM).

3. **`p3_dialog.html` (`p3_dialog.steps.json`)**
   - **Seed Contract:** Accessible modal dialog element `div #dialog` with `role="dialog"` and `aria-modal="true"`, heading `h2`, button `#open` to set `display: block`, and button `#close` to reset `display: none`.
   - **Operations Exercised:** `click`.
   - **Expected State Count:** 3 states (Initial state hidden -> modal open state -> modal closed state).

4. **`p4_late.html` (`p4_late.steps.json`)**
   - **Seed Contract:** Asynchronous DOM insertion and property state change. Element `div #late` inserted via `setTimeout` after 1500 ms, button `#enable` loses `disabled` attribute after 1800 ms.
   - **Operations Exercised:** `wait` (with `timeout_ms: 4000`), `click`.
   - **Expected State Count:** 3 states (Initial state disabled -> post-insertion state after `#late` appears -> post-activation state after clicking `#enable`).

5. **`p5_text.html` (`p5_text.steps.json`)**
   - **Seed Contract:** Complex text and layout edge cases: non-ASCII labels in Spanish (`¿Cómo estás?`) and Japanese (`こんにちは`), emoji (`🚀`), button `#aria-btn` where `aria-label` differs from visible text, hidden element with `visibility: hidden`, zero-size element (`width: 0; height: 0`), and element with long whitespace runs.
   - **Operations Exercised:** `click`.
   - **Expected State Count:** 2 states (Initial state -> state after clicking button `#aria-btn`).
