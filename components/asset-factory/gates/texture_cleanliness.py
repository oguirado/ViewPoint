"""Native protected-support and reference-residual checks on straight RGBA8."""
from collections import deque
from fractions import Fraction
from functools import lru_cache

from asset_factory import core, domains, math_acceptance
from tools.blender.uv_quality import checked_bytes, failure, native, raster_size
from tools.input.evidence import InputIssue, checked_record, closed, is_digest, point, require


_SRGB_SCALE = 10 ** 9
_LINEAR_TEXTURE_ROLES = frozenset(("orm", "roughness", "metallic", "normal"))
_COLOUR_TEXTURE_ROLES = frozenset(("albedo", "base_color", "emissive"))


@lru_cache(maxsize=1)
def _srgb_bounds():
    """Enclose decoded sRGB bytes in linear light using integer comparisons."""
    bounds = []
    for value in range(256):
        if value * 100000 <= 4045 * 255:
            lower, residual = divmod(value * 25 * _SRGB_SCALE, 255 * 323)
        else:
            # Decode the IEC transfer's high segment by comparing fifth powers.
            numerator = (200 * value + 11 * 255) ** 12 * _SRGB_SCALE ** 5
            denominator = (211 * 255) ** 12
            lower, upper = 0, _SRGB_SCALE + 1
            while upper - lower > 1:
                middle = (lower + upper) // 2
                if middle ** 5 * denominator <= numerator:
                    lower = middle
                else:
                    upper = middle
            residual = numerator - lower ** 5 * denominator
        bounds.append((lower, lower + bool(residual)))
    return tuple(bounds)


def _rgb_error(reference, candidate, offset, bounds):
    errors = []
    for a, b in zip(reference[offset:offset + 3], candidate[offset:offset + 3]):
        if a != b and bounds is not None:
            lo, hi = sorted((a, b))
            errors.append(bounds[hi][1] - bounds[lo][0])
        else:
            errors.append(abs(a - b))
    return errors


def _cluster_sizes(pixels, width):
    pending = pixels
    sizes = []
    while pending:
        queue = deque([pending.pop()])
        count = 0
        while queue:
            i = queue.popleft()
            count += 1
            neighbors = [i - width, i + width]
            if i % width:
                neighbors.append(i - 1)
            if i % width != width - 1:
                neighbors.append(i + 1)
            for key in neighbors:
                if key in pending:
                    pending.remove(key)
                    queue.append(key)
        sizes.append(count)
    return sorted(sizes, reverse=True)


def check_texture_cleanliness(candidate, *, witness_digest, read_checked, candidate_lineage_digest=None):
    """Check support against an independently authorized reference and fixed regions.

    INT must resolve candidate_lineage_digest from admitted provenance bound to
    the candidate bytes. The protected reader authorizes the reference lineage,
    texture role, encoding, spatial policy, and base texels read by the sampler.
    """
    receipt = native("uv.cleanliness", candidate, witness_digest)
    try:
        witness = checked_record(read_checked, "uv-cleanliness-witness", witness_digest)
        require("texture_role" in witness, 3, "texture_role_missing")
        require("filter_footprint_digest" in witness, 3, "texture_filter_footprint_missing")
        closed(witness, "af.texture-cleanliness-witness/v1", "reference_digest", "size",
               "protected_rects", "tile_size", "color_space", "max_global_mae_ppm",
               "max_local_channel_ppm", "max_tile_mae_ppm", "outlier_channel_ppm", "max_outlier_ppm",
               "reference_lineage_digest", "allowed_stripe_rects", "max_outlier_clusters",
               "max_outlier_cluster_px", "texture_role", "filter_footprint_digest")
        width, height = raster_size(witness["size"])
        pixel_count = width * height
        require(type(candidate) is bytes and len(candidate) == 4 * pixel_count,
                2, "texture_rgba_invalid")
        reference = checked_bytes(read_checked, "uv-cleanliness-reference", witness["reference_digest"],
                                  len(candidate))
        require(candidate_lineage_digest is not None, 3, "texture_candidate_lineage_missing")
        reference_lineage = witness["reference_lineage_digest"]
        require(is_digest(candidate_lineage_digest) and is_digest(reference_lineage),
                2, "texture_lineage_invalid")
        receipt["input_refs"].append(candidate_lineage_digest)
        receipt["reference_refs"].extend((witness["reference_digest"], reference_lineage))
        require(witness["reference_digest"] != receipt["subject_digest"]
                and reference_lineage != candidate_lineage_digest, 5, "texture_self_witness")
        limits = ("max_global_mae_ppm", "max_local_channel_ppm", "max_tile_mae_ppm",
                  "outlier_channel_ppm", "max_outlier_ppm")
        cluster_limits = ("max_outlier_clusters", "max_outlier_cluster_px")
        require(all(type(witness[k]) is int and 0 <= witness[k] <= domains.PPM for k in limits)
                and all(type(witness[k]) is int and witness[k] >= 0 for k in cluster_limits)
                and witness["color_space"] in domains.COLOR_SPACES, 2, "texture_policy_invalid")
        role = witness["texture_role"]
        require(type(role) is str and (role in _LINEAR_TEXTURE_ROLES or role in _COLOUR_TEXTURE_ROLES),
                2, "texture_role_invalid")
        srgb = witness["color_space"] == "srgb"
        receipt.update(texture_role=role, color_space=witness["color_space"], residual_color_space="linear")
        require(not srgb or role in _COLOUR_TEXTURE_ROLES, 5, "texture_role_requires_linear")
        footprint_pin = witness["filter_footprint_digest"]
        footprint = checked_bytes(read_checked, "uv-cleanliness-filter-footprint", footprint_pin, pixel_count)
        require(footprint.count(b"\0") + footprint.count(b"\x01") == pixel_count,
                2, "texture_filter_footprint_invalid")
        receipt["reference_refs"].append(footprint_pin)
        rects, tile, stripes = witness["protected_rects"], witness["tile_size"], witness["allowed_stripe_rects"]
        require(type(rects) is dict and rects, 3, "texture_protected_regions_missing")
        require(type(tile) is int and tile > 0, 2, "texture_tile_invalid")
        require(type(stripes) is list, 2, "texture_stripe_regions_invalid")
        require(len(rects) + len(stripes) <= 32, 6, "texture_region_budget")
        stripe_mask = bytearray(pixel_count) if stripes else None
        for rect in stripes:
            x0, y0, x1, y1 = domains.rectangle(rect, (width, height))
            row = b"\x01" * (x1 - x0)
            for y in range(y0, y1):
                offset = y * width
                stripe_mask[offset + x0:offset + x1] = row
        bounds, scale = (_srgb_bounds(), _SRGB_SCALE) if srgb else (None, 255)
        mismatch = sum(a != b for a, b in zip(reference[3::4], candidate[3::4]))
        receipt["metrics"]["alpha_support_mismatch_count"] = point(mismatch)
        require(mismatch == 0, 5, "texture_support_changed")
        alpha_weight, absolute, outliers = 0, 0, 0
        speckles = set()
        for i, in_footprint in enumerate(footprint):
            offset = 4 * i
            alpha = reference[offset + 3]
            if not in_footprint:
                require(alpha == 0, 5, "texture_filter_support_missing")
                continue
            errors = _rgb_error(reference, candidate, offset, bounds)
            alpha_weight += alpha
            absolute += sum(errors) * alpha
            if max(errors) * domains.PPM > witness["outlier_channel_ppm"] * scale:
                outliers += 1
                if stripe_mask is None or not stripe_mask[i]:
                    speckles.add(i)
        require(alpha_weight, 3, "texture_reference_support_missing")
        global_mae = Fraction(absolute, alpha_weight * 3 * scale)
        metrics = {"global_mae_upper_ppm": global_mae,
                   "texture_outlier_upper_ppm": Fraction(outliers, footprint.count(b"\x01"))}
        clusters = _cluster_sizes(speckles, width)
        cluster_size = max(clusters, default=0)
        receipt["outlier_clusters"] = clusters
        receipt["metrics"].update(outlier_cluster_count=point(len(clusters)),
                                  outlier_cluster_max_px=point(cluster_size))
        protected_max, tile_max = 0, 0
        for name, rect in sorted(rects.items()):
            x0, y0, x1, y1 = domains.rectangle(rect, (width, height))
            region_max, region_tile = 0, 0
            region_support = False
            for top in range(y0, y1, tile):
                for left in range(x0, x1, tile):
                    covered, total = 0, 0
                    for y in range(top, min(top + tile, y1)):
                        for x in range(left, min(left + tile, x1)):
                            i = 4 * (y * width + x)
                            a = reference[i+3]
                            if a:
                                errors = _rgb_error(reference, candidate, i, bounds)
                                region_max = max(region_max, max(errors))
                                covered += 1
                                total += sum(errors)
                    if covered:
                        region_support = True
                        region_tile = max(region_tile, Fraction(total, covered * 3 * scale))
            require(region_support, 3, "texture_protected_support_missing")
            region_channel = Fraction(region_max, scale)
            metrics["max_channel_error_upper_ppm." + name] = region_channel
            metrics["worst_tile_error_upper_ppm." + name] = region_tile
            protected_max = max(protected_max, region_channel)
            tile_max = max(tile_max, region_tile)
        metrics["max_channel_error_upper_ppm"] = protected_max
        metrics["worst_tile_error_upper_ppm"] = tile_max
        receipt["metrics"].update({key: point(value) for key, value in metrics.items()})
        upper = {key: math_acceptance.outward_bounds(*point(value), scale=domains.PPM)[1]
                 for key, value in metrics.items()}
        receipt["measurements"] = upper
        for key, limit, reason in (
            ("global_mae_upper_ppm", "max_global_mae_ppm", "texture_global_colour_limit"),
            ("texture_outlier_upper_ppm", "max_outlier_ppm", "texture_outlier_limit"),
            ("max_channel_error_upper_ppm", "max_local_channel_ppm", "texture_protected_channel_limit"),
            ("worst_tile_error_upper_ppm", "max_tile_mae_ppm", "texture_protected_tile_limit"),
        ):
            if upper[key] > witness[limit]:
                receipt["reason_codes"].append(reason)
        if len(clusters) > witness["max_outlier_clusters"] or cluster_size > witness["max_outlier_cluster_px"]:
            receipt["reason_codes"].append("texture_speckle_cluster")
        if receipt["reason_codes"]:
            receipt["source_rc"] = 5
        return receipt
    except (InputIssue, core.ContractError, ValueError, TypeError, KeyError, IndexError,
            MemoryError, OSError, OverflowError, RecursionError) as error:
        return failure(receipt, error)
