"""Qualification observations, never semantic quality acceptance thresholds."""
from importlib import metadata

from providers.birefnet import offline


def environment_freeze():
    """Installed distribution versions; not an artifact lock or rebuild proof."""
    return sorted(
        ({'distribution': d.metadata['Name'], 'version': d.version}
         for d in metadata.distributions() if d.metadata['Name']),
        key=lambda row: (row['distribution'].lower(), row['version']))


def alpha_observations(source, candidate):
    metrics = offline.measurements(source, candidate, 'birefnet', 1)
    alpha = candidate.getchannel('A')
    hist = alpha.histogram()
    total = candidate.width * candidate.height
    # The midpoint partitions the byte range for diagnostics only. It is not a
    # calibrated alpha threshold and never determines production admission.
    midpoint = 128
    pixels = alpha.tobytes()
    width, height = candidate.size
    border = [value for i, value in enumerate(pixels)
              if i < width or i >= width * (height - 1)
              or i % width in (0, width - 1)]
    metrics.update(
        diagnostic_alpha_midpoint=midpoint,
        foreground_at_midpoint_ppm=sum(hist[midpoint:]) * 1000000 // total,
        border_at_midpoint_ppm=sum(v >= midpoint for v in border) * 1000000 // len(border),
        soft_alpha_ppm=sum(hist[1:255]) * 1000000 // total,
        zero_alpha_ppm=hist[0] * 1000000 // total,
        opaque_alpha_ppm=hist[255] * 1000000 // total,
        alpha_mae_ppm=None, alpha_iou_ppm=None)
    return {'metrics': metrics, 'quality_verdict': 'CANNOT_SEE',
            'absence_reasons': {'alpha_mae_ppm': 'golden_alpha_absent',
                                'alpha_iou_ppm': 'golden_alpha_absent'}}
