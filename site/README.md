# ViewPoint · Gemini in Blender

Static showcase site for the 3D pieces that Gemini agents built in a live Blender with ViewPoint (Google DeepMind × Berkeley hackathon, 14 Nov 2026).

- Plain HTML, CSS and JavaScript. No build step, no trackers.
- `data/pieces.json`: every piece and every number, each with the log it comes from.
- `data/videos.json`: paste YouTube video ids here (the 11 characters after `watch?v=`). Empty entries hide the player.
- `data/copy.json`: section copy.
- `PROVENANCE.md`: who made what.

Run locally:

```bash
python3 -m http.server 8000
```

Then open http://127.0.0.1:8000/.
