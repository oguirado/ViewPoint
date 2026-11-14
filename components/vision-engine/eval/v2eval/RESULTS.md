# V2EVAL: sealed DEV

STATUS: measurements complete; lane BLOCKED by execution-boundary violations (REPORT_E.md).

Counts/rates [MEASURED]; costs DIAGNOSTIC. Baseline and sealed source hashes match. 815 scenes: 768 at 320x180, 47 at 1920x1080; 120 frames each. DEV only; no out-of-sample claim.

B=baseline, GM=global motion, FL=flicker, ST=subtile, ALL=combined. Independent baseline claims; priority FL > ST > GM. Percentages below; — means undefined.

|Pass|MOVED recall / mv exact %|MOVED precision %|FLICKER recall %|FLICKER precision %|CHANGED precision %|silent miss ppm|
|---|---:|---:|---:|---:|---:|---:|
|B|52.78|99.38|0.00|0.00|61.93|46275|
|GM|78.83|99.58|0.00|0.00|76.57|28588|
|FL|52.78|99.38|67.01|0.21|61.92|46275|
|ST|53.95|96.56|0.00|0.00|62.05|46275|
|ALL|80.00|97.66|67.01|0.43|77.19|28588|

Every candidate adds 0 static/jitter false relabels over 4,176,000 tile-frames: 0 ppm. Every pass still flags JITTER, CHANGED, FLICKER, FROZEN and DRIFT in class_honesty.

|Kind|MOVED recall %: B / GM / FL / ST / ALL|FL recall %|
|---|---|---:|
|aa_jitter|—|—|
|canvas_anim|—|—|
|caret_blink|—|67.01|
|caret_move|0.00 / 0.00 / 0.00 / 46.50 / 46.50|—|
|scroll_h|37.17 / 89.56 / 37.17 / 37.17 / 89.56|—|
|scroll_large|0.00 / 58.55 / 0.00 / 0.00 / 58.55|—|
|scroll_page|—|—|
|scroll_small|60.92 / 70.47 / 60.92 / 61.92 / 71.47|—|
|scroll_smooth|98.28 / 98.38 / 98.28 / 98.63 / 98.74|—|
|static_clean|—|—|
|typing|0.00 / 0.00 / 0.00 / 46.52 / 46.52|—|
|window_drag|23.16 / 23.16 / 23.16 / 44.85 / 44.85|—|

|Resolution|MOVED recall %: B / GM / FL / ST / ALL|
|---|---|
|320x180|58.51 / 90.53 / 58.51 / 59.60 / 91.62|
|1920x1080|42.19 / 57.21 / 42.19 / 43.50 / 58.52|

500 ms blink, FL: at 30 fps recall 66.67%, precision 6.56%; at 60 fps both 0% within the sealed window. Actual 12px carrier proxy: ST recall 0% -> 71.43%; narrow/textured caret MOVED support is 0. Actual 12px at 1080p: AUSENTE.

Confusion matrices: rows truth, columns prediction; order UNCHANGED,JITTER,CHANGED,MOVED,FLICKER,FROZEN,DRIFT.

```text
B [[18065302,0,0,0,0,0,30],[0,2070600,0,0,0,0,0],[0,341534,5152476,25752,137158,2507,27911],[0,282636,3165991,4116463,209296,1375,23805],[0,0,1164,0,0,0,0],[0,0,0,0,0,0,0],[0,0,0,0,0,0,0]]
GM [[18065302,0,0,0,0,0,30],[0,2070600,0,0,0,0,0],[0,341534,5152476,25752,137158,2507,27911],[0,44075,1575821,6148359,20324,572,10415],[0,0,1164,0,0,0,0],[0,0,0,0,0,0,0],[0,0,0,0,0,0,0]]
FL [[18045635,0,0,0,19667,0,30],[0,2070600,0,0,0,0,0],[0,341534,5149222,25752,140412,2507,27911],[0,282636,3165991,4116463,209296,1375,23805],[0,0,384,0,780,0,0],[0,0,0,0,0,0,0],[0,0,0,0,0,0,0]]
ST [[18065302,0,0,0,0,0,30],[0,2070600,0,0,0,0,0],[0,341534,5028469,149759,137158,2507,27911],[0,282636,3074856,4207598,209296,1375,23805],[0,0,1164,0,0,0,0],[0,0,0,0,0,0,0],[0,0,0,0,0,0,0]]
ALL [[18045635,0,0,0,19667,0,30],[0,2070600,0,0,0,0,0],[0,341534,5025215,149759,140412,2507,27911],[0,44075,1484686,6239494,20324,572,10415],[0,0,384,0,780,0,0],[0,0,0,0,0,0,0],[0,0,0,0,0,0,0]]
```

|1080p cost, DIAGNOSTIC ms/frame|DEV mean, exact batching + cache|Original Python, fixed scroll transition|
|---|---:|---:|
|GM|112.803|5224.305|
|FL|0.233|0.345|
|ST|17.088|72619.183|

Shared CPUs 2-4. Prototype work includes spatial luma conversion; excludes common output copies, source, engine and scorer. Original/accelerated outputs agree on the fixed diagnostics.

|Prototype|Recommendation and evidence|
|---|---|
|Global|ADOPT into a v2 card: +2,031,896 correct, +0 false MOVED. Fix beam pruning (`eval/v2proto_global_motion.py:119`) and distant-alias rejection (`:136`); recall still misses cases.|
|Flicker|REWORK: +780 correct / +22,921 false FLICKER. Held-phase emission (`eval/v2proto_flicker.py:239`) and startup (`:215`) affect these counts.|
|Subtile|REWORK: +91,135 correct / +124,007 false MOVED. Skipped partial blocks (`eval/v2proto_subtile.py:55`) let neighbors claim an exposed bottom tile; reproduced on DEV. Narrow-caret validation is absent.|

Global alias regression is proven separately; its individual DEV contribution is not isolated. Full per-class/per-kind metrics, honesty flags, resolution and target views, net deltas and provenance are in results.json. Scope: the supplied sealed gui_synth DEV corpus.
