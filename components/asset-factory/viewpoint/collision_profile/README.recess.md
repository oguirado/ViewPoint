# Offline recess replay

Run from the asset-factory root using the local MuJoCo interpreter.

```bash
python -B -m viewpoint.collision_profile.recess \
  --fixture /path/to/fixture.json --fixture-sha256 SHA256 --mode adaptive
```

Use `exhaustive` for the quality-matched reference arm. The fixture binds the native backend version, integer micrometre world coordinates, positive X travel, prescribed tick count and duration, tolerances, convex box proxies and the relative filename and SHA-256 of independently authored orthogonal polygons extruded along Z. Polygon strips must cover each declared body. Proxy and reference share body IDs and clock. Supply both files together. The command yields one JSON receipt on standard output. Exit status zero denotes agreement, one denotes a measured discrepancy and two denotes invalid input or an unavailable runtime.

Clearance is the minimum signed L-infinity box-pair separation over a body's proxy boxes or independently reconstructed strips. Positive values denote separation, zero denotes touch and negative values denote overlap. Penetration is the maximum observed negative box-pair clearance. Overlapping strips of a composed obstacle can have a different escape distance as a whole. The receipt defines its own pair order by sample tick then obstacle ID, with the peg first and multiple native contact points collapsed to maximum depth per pair.

Both modes replay the same prescribed peg pose through a MuJoCo step at every tick. Pose reset isolates geometric onset and order from solver response. Adaptive traversal prunes fine strips whose bounding clearance exceeds the best exact value. Near-contact pairs receive full-strip verification when the traversal pruned a strip. Untrusted or missing geometry causes a refusal. Receipt metrics cover the completed fixture, measured native step time and process peak RSS on Linux. The included tests qualify this command on the bounded, discrete orthogonal fixture.
