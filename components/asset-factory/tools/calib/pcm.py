"""Native-rate signed PCM split check for the synthetic calibration profile."""

import sys
import wave
from array import array
from io import BytesIO

from .common import CalibrationError, require


MATCH_THRESHOLD_PPM = 995_000
MIN_MATCH_FRAMES = 3
MAX_DECODED_SAMPLES = 1_000_000
MAX_CORRELATION_TERMS = 2_000_000
_PPM = 1_000_000


def decode_pcm(raw, case, remaining):
    """Read the pinned timebase and retain signed, nonconstant channel data."""
    try:
        with wave.open(BytesIO(raw), "rb") as reader:
            rate, frames, channels = reader.getframerate(), reader.getnframes(), reader.getnchannels()
            require(reader.getsampwidth() == 2 and reader.getcomptype() == "NONE", 2,
                    "unsupported_audio_encoding")
            require((rate, frames) == (case["sample_rate"], case["sample_count"]), 4,
                    "audio_timebase_changed")
            count = frames * channels
            require(count <= remaining, 6, "pcm_split_decode_budget_exceeded")
            data = reader.readframes(frames)
            require(len(data) == count * 2, 2, "invalid_audio")
    except (EOFError, wave.Error) as exc:
        raise CalibrationError(2, "invalid_audio") from exc

    samples = array("h", data)
    if sys.byteorder != "little":
        samples.byteswap()
    signals = []
    for channel in range(channels):
        signal = samples[channel::channels]
        start, stop = 0, len(signal)
        # Discard digital silence, including a one-LSB change in quiet padding.
        while start < stop and abs(signal[start]) <= 1:
            start += 1
        while stop > start and abs(signal[stop - 1]) <= 1:
            stop -= 1
        if stop - start >= MIN_MATCH_FRAMES:
            active = signal[start:stop]
            if min(active) < max(active):
                signals.append(active)
    return signals, count


def _matching_windows(first, second, remaining):
    """Compare the entire shorter signal against each contiguous longer window."""
    if len(first) > len(second):
        first, second = second, first
    # Exact crops can be found without spending the quadratic correlation budget.
    needle, haystack = first.tobytes(), second.tobytes()
    position = haystack.find(needle)
    while position >= 0:
        if position % first.itemsize == 0:
            return True, remaining
        position = haystack.find(needle, position + 1)

    n = len(first)
    windows = len(second) - n + 1
    charge = n * windows
    require(remaining >= charge, 6, "pcm_split_correlation_budget_exceeded")
    remaining -= charge
    first_sum = sum(first)
    first_energy = n * sum(x * x for x in first) - first_sum ** 2
    scale = first_energy * MATCH_THRESHOLD_PPM ** 2
    second = memoryview(second)
    window = second[:n]
    window_sum = sum(window)
    window_squares = sum(x * x for x in window)
    for offset in range(windows):
        if offset:
            removed, added = second[offset - 1], second[offset + n - 1]
            window_sum += added - removed
            window_squares += added * added - removed * removed
            window = second[offset:offset + n]
        window_energy = n * window_squares - window_sum ** 2
        if window_energy:
            product = sum(x * y for x, y in zip(first, window, strict=True))
            covariance = n * product - first_sum * window_sum
            if covariance ** 2 * _PPM ** 2 >= scale * window_energy:
                return True, remaining
    return False, remaining


def compare_pcm(calibration, held_out, remaining):
    """Return a cross-split match and the remaining comparison budget."""
    cal_rate, cal_signals = calibration
    held_rate, held_signals = held_out
    require(cal_rate == held_rate, 3, "pcm_split_sample_rate_unqualified")
    require(cal_signals and held_signals, 3, "pcm_split_overlap_unavailable")
    for cal in cal_signals:
        for held in held_signals:
            matched, remaining = _matching_windows(cal, held, remaining)
            if matched:
                return True, remaining
    return False, remaining


def encode_fingerprint(fingerprint):
    """Retain the trimmed channel samples needed for crop and gain comparisons."""
    rate, channels = fingerprint
    encoded = []
    for channel in channels:
        little_endian = channel
        if sys.byteorder != "little":
            little_endian = channel[:]
            little_endian.byteswap()
        encoded.append(little_endian.tobytes().hex())
    return {"sample_rate": rate, "channels": encoded}


def decode_fingerprint(record, remaining):
    reason = "invalid_exposure_ledger"
    require(type(record) is dict and record.keys() == {"sample_rate", "channels"}, 3, reason)
    rate, channels = record["sample_rate"], record["channels"]
    require(type(rate) is int and rate > 0 and type(channels) is list and channels, 3, reason)
    decoded = []
    for channel in channels:
        require(type(channel) is str and len(channel) % 4 == 0
                and MIN_MATCH_FRAMES <= len(channel) // 4 <= remaining, 3, reason)
        try:
            raw = bytes.fromhex(channel)
        except ValueError as exc:
            raise CalibrationError(3, reason) from exc
        require(raw.hex() == channel, 3, reason)
        samples = array("h", raw)
        if sys.byteorder != "little":
            samples.byteswap()
        require(abs(samples[0]) > 1 and abs(samples[-1]) > 1 and min(samples) < max(samples), 3, reason)
        decoded.append(samples)
        remaining -= len(samples)
    return (rate, decoded), remaining
