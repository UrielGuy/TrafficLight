"""Mixer, voices and the streaming decoder, pulled directly (no sound card)."""
from __future__ import annotations

import time
from pathlib import Path

import av
import numpy as np
import pytest

from trafficlight.audio.mixer import Mixer
from trafficlight.render.base import AudioCue

RATE = 48000
# Sample i of the "index" file encodes i: left = (i % 50000) * 1e-5, right = (i // 50000) * 1e-3
# (every value stays below the limiter knee and well above float32 resolution).


def write_wav(path: Path, data: np.ndarray, rate: int = RATE, codec: str = "pcm_f32le", fmt: str = "flt") -> None:
    """data: (n, ch) float32."""
    ch = data.shape[1]
    with av.open(str(path), "w") as c:
        s = c.add_stream(codec, rate=rate)
        s.layout = "stereo" if ch == 2 else "mono"
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(data.reshape(1, -1)), format=fmt,
                                           layout="stereo" if ch == 2 else "mono")
        frame.sample_rate = rate
        frame.pts = 0
        for p in s.encode(frame):
            c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def index_file(path: Path, seconds: float) -> Path:
    n = int(seconds * RATE)
    i = np.arange(n, dtype=np.int64)
    write_wav(path, np.stack([(i % 50000) * 1e-5, (i // 50000) * 1e-3], axis=1).astype(np.float32))
    return path


def tone_file(path: Path, seconds: float, amp: float = 0.25, hz: float = 440.0) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    s = (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    write_wav(path, np.stack([s, s], axis=1))
    return path


def frame_index(block: np.ndarray) -> np.ndarray:
    return np.rint(block[:, 1] / 1e-3).astype(np.int64) * 50000 + np.rint(block[:, 0] / 1e-5).astype(np.int64)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def mixer():
    m = Mixer(RATE, 2)
    yield m
    m.close()


def pull(m: Mixer, seconds: float, block: int = 480) -> np.ndarray:
    """Render faster than real time, but never outrun the decoder (that would be an underrun, tested separately)."""
    blocks = []
    for _ in range(int(seconds * RATE / block)):
        m.wait_ready(5, seconds=2 * block / RATE)
        blocks.append(m.render(block))
    return np.concatenate(blocks)


def start(m: Mixer, cues, playing=True):
    m.sync(cues, playing)
    assert m.wait_ready()


def test_cue_starts_at_position_sample_accurate(mixer, tmp_path):
    f = index_file(tmp_path / "idx.wav", 5)
    start(mixer, [AudioCue("a", str(f), 1.5)])
    out = pull(mixer, 0.5)
    idx = frame_index(out)
    # after the start ramp the content is exactly the file's samples from 1.5 s on, in order
    expect = int(1.5 * RATE) + np.arange(len(out))
    ok = slice(2000, None)
    assert np.array_equal(idx[ok], expect[ok])
    # the very first sample is faded in (no click)
    assert np.abs(out[0]).max() < 0.01 * np.abs(out[2000]).max()


def test_output_latency_is_compensated(tmp_path):
    m = Mixer(RATE, 2)
    m.latency = 0.1
    try:
        f = index_file(tmp_path / "idx.wav", 5)
        start(m, [AudioCue("a", str(f), 1.0)])
        first = frame_index(pull(m, 0.2))[4000]
        # the sample handed over 4000 frames into the stream is heard 0.1 s later, when the cue is at 1.0 + (4000/48000) + ...
        assert first == int(1.1 * RATE) + 4000
        assert m.voice_position("a") == pytest.approx(1.0 + 0.2, abs=0.001)
    finally:
        m.close()


def test_small_drift_does_not_seek_but_a_jump_does(mixer, tmp_path):
    f = index_file(tmp_path / "idx.wav", 20)
    start(mixer, [AudioCue("a", str(f), 1.0)])
    pull(mixer, 0.5)                                   # voice is now at 1.5 s
    gen = mixer._live["a"].buf.gen
    for jitter in (0.04, -0.05, 0.07, -0.07):          # cue frame jitter below 80 ms
        mixer.sync([AudioCue("a", str(f), 1.5 + jitter)])
        assert mixer._live["a"].pending_seek is None
    out = pull(mixer, 0.1)
    assert np.array_equal(np.diff(frame_index(out)), np.ones(len(out) - 1))   # unbroken
    mixer.sync([AudioCue("a", str(f), 1.6 + 0.2)])     # a real jump (a seek on the timeline)
    assert mixer._live["a"].pending_seek is not None
    mixer.wait_ready()
    out = pull(mixer, 0.3)
    idx = frame_index(out)
    assert idx[-1] >= int(1.8 * RATE)
    assert np.array_equal(np.diff(idx[-2000:]), np.ones(1999))
    assert mixer._live["a"].buf.gen >= gen
    assert abs(out[0, 0]) < 0.5                         # faded around the jump


def test_seek_in_a_long_file(mixer, tmp_path):
    f = index_file(tmp_path / "long.wav", 600)         # 10 minutes
    start(mixer, [AudioCue("a", str(f), 0.0)])
    pull(mixer, 0.2)
    t0 = time.monotonic()
    mixer.sync([AudioCue("a", str(f), 500.0)])
    assert mixer.wait_ready(5)
    assert time.monotonic() - t0 < 2.0
    out = pull(mixer, 0.3)
    idx = frame_index(out)
    expect = int(500 * RATE) + np.arange(len(idx))
    good = idx[3000:] - expect[3000:]
    assert np.all(good == good[0]) and 0 <= good[0] < RATE // 2    # lands at the cue, plus the time the seek took


def test_a_voice_starting_deep_into_the_file(mixer, tmp_path):
    f = index_file(tmp_path / "long.wav", 600)
    start(mixer, [AudioCue("a", str(f), 400.0)])
    idx = frame_index(pull(mixer, 0.2))
    assert idx[5000] == 400 * RATE + 5000


def test_gain_is_smoothed(mixer, tmp_path):
    f = tone_file(tmp_path / "t.wav", 5, amp=0.5)
    start(mixer, [AudioCue("a", str(f), 0.0, gain=0.0)])
    pull(mixer, 0.1)
    mixer.sync([AudioCue("a", str(f), 0.1, gain=1.0)])
    out = pull(mixer, 0.1)
    env = np.abs(out[:, 0])
    # no step: the envelope rises over ~20 ms, and the sample-to-sample change stays small
    assert env[:48].max() < 0.2
    assert np.abs(np.diff(out[:, 0])).max() < 0.1
    assert env[-2000:].max() == pytest.approx(0.5, abs=0.02)


def test_dropped_key_fades_out_and_is_removed(tmp_path):
    clock = Clock()
    m = Mixer(RATE, 2, clock=clock)
    try:
        f = tone_file(tmp_path / "t.wav", 5)
        start(m, [AudioCue("a", str(f), 0.0)])
        pull(m, 0.1)
        m.sync([])
        assert m.voice_count() == 0
        out = pull(m, 0.05)
        assert np.abs(out[:5]).max() > 0.01                 # still sounding at the start of the fade
        assert np.abs(out[1000:]).max() == 0.0              # silent after ~10 ms
        assert np.abs(np.diff(out[:, 0])).max() < 0.1       # no click
        clock.t += 1
        m.sync([])
        assert len(m._voices) == 0
    finally:
        m.close()


def test_pause_silences_keeps_voice_and_resumes_in_place(mixer, tmp_path):
    f = index_file(tmp_path / "idx.wav", 5)
    start(mixer, [AudioCue("a", str(f), 1.0)])
    pull(mixer, 0.2)
    mixer.sync([AudioCue("a", str(f), 1.2)], playing=False)
    pull(mixer, 0.05)                                       # fade-out
    pos = mixer._live["a"].buf.position()
    out = pull(mixer, 0.5)
    assert np.abs(out).max() == 0.0
    assert mixer._live["a"].buf.position() == pos           # frozen, not running
    assert mixer.voice_count() == 1
    mixer.sync([AudioCue("a", str(f), 1.2)], playing=True)
    assert mixer._live["a"].pending_seek is None            # no drift => no seek
    out = pull(mixer, 0.2)
    assert np.abs(out).max() > 0
    idx = frame_index(out)
    assert idx[-1] > pos


def test_two_voices_mix_and_soft_limit(mixer, tmp_path):
    a = tone_file(tmp_path / "a.wav", 3, amp=0.3, hz=440)
    b = tone_file(tmp_path / "b.wav", 3, amp=0.3, hz=660)
    start(mixer, [AudioCue("a", str(a), 0.0)])
    solo = pull(mixer, 0.3)
    m2 = Mixer(RATE, 2)
    try:
        start(m2, [AudioCue("a", str(a), 0.0), AudioCue("b", str(b), 0.0)])
        both = pull(m2, 0.3)
    finally:
        m2.close()
    ref_b = Mixer(RATE, 2)
    try:
        start(ref_b, [AudioCue("b", str(b), 0.0)])
        only_b = pull(ref_b, 0.3)
    finally:
        ref_b.close()
    sl = slice(3000, None)
    assert np.allclose(both[sl], solo[sl] + only_b[sl], atol=1e-4)      # below the knee: a plain sum

    loud = tone_file(tmp_path / "l.wav", 3, amp=1.0)
    m3 = Mixer(RATE, 2)
    try:
        start(m3, [AudioCue(k, str(loud), 0.0) for k in "abc"])
        out = pull(m3, 0.3)
    finally:
        m3.close()
    assert np.abs(out).max() <= 1.0
    assert np.abs(out).max() > 0.9                                      # compressed, not crushed
    assert np.abs(np.diff(out[:, 0])).max() < 0.5


def test_master_volume(mixer, tmp_path):
    f = tone_file(tmp_path / "t.wav", 3, amp=0.5)
    mixer.set_volume(0.5)
    start(mixer, [AudioCue("a", str(f), 0.0)])
    out = pull(mixer, 0.3)
    assert np.abs(out[-4000:]).max() == pytest.approx(0.25, abs=0.02)


def test_mono_file_plays_at_full_level_on_both_channels(mixer, tmp_path):
    t = np.arange(RATE * 2) / RATE
    s = (0.4 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)[:, None]
    f = tmp_path / "m.wav"
    write_wav(f, s)
    start(mixer, [AudioCue("a", str(f), 0.0)])
    out = pull(mixer, 0.3)
    assert np.allclose(out[:, 0], out[:, 1])
    assert np.abs(out[-4000:]).max() == pytest.approx(0.4, abs=0.02)


def test_other_sample_rate_is_resampled(mixer, tmp_path):
    t = np.arange(44100 * 2) / 44100
    s = (0.4 * np.sin(2 * np.pi * 441 * t)).astype(np.float32)
    f = tmp_path / "r.wav"
    write_wav(f, np.stack([s, s], axis=1), rate=44100)
    start(mixer, [AudioCue("a", str(f), 0.0)])
    out = pull(mixer, 1.0)[:, 0]
    spec = np.abs(np.fft.rfft(out[RATE // 4:RATE // 4 + RATE // 2]))
    assert abs(np.argmax(spec) * 2 - 441) <= 2                          # 0.5 s window -> 2 Hz bins


def test_compressed_formats_decode(mixer, tmp_path):
    t = np.arange(RATE * 2) / RATE
    s = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    f = tmp_path / "x.mp3"
    with av.open(str(f), "w") as c:
        st = c.add_stream("libmp3lame", rate=RATE)
        st.layout = "stereo"
        fr = av.AudioFrame.from_ndarray(np.stack([s, s], 1).reshape(1, -1), format="flt", layout="stereo")
        fr.sample_rate = RATE
        for p in st.encode(fr):
            c.mux(p)
        for p in st.encode(None):
            c.mux(p)
    start(mixer, [AudioCue("a", str(f), 0.5)])
    out = pull(mixer, 0.5)
    assert np.abs(out[-4000:]).max() == pytest.approx(0.3, abs=0.05)


def test_missing_and_corrupt_files_are_silent(mixer, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not audio" * 100)
    good = tone_file(tmp_path / "t.wav", 3)
    mixer.sync([AudioCue("m", str(tmp_path / "nope.wav"), 0.0), AudioCue("b", str(bad), 0.0),
                AudioCue("g", str(good), 0.0)])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(v.buf.ready() for v in mixer._voices):
        time.sleep(0.01)
    assert mixer._live["m"].error and mixer._live["b"].error
    out = pull(mixer, 0.3)
    assert np.abs(out[-4000:]).max() == pytest.approx(0.25, abs=0.02)       # only the good one sounds
    mixer.sync([AudioCue("m", str(tmp_path / "nope.wav"), 5.0)])          # no seek storm on a broken voice
    assert mixer._live["m"].pending_seek is None


def test_watchdog_fades_to_silence_when_sync_stops(tmp_path):
    clock = Clock()
    m = Mixer(RATE, 2, clock=clock)
    try:
        f = tone_file(tmp_path / "t.wav", 5)
        start(m, [AudioCue("a", str(f), 0.0)])
        assert np.abs(pull(m, 0.1)).max() > 0.1
        clock.t += 0.3
        assert np.abs(pull(m, 0.1)).max() > 0.1             # within the grace period
        clock.t += 0.3                                      # 0.6 s without sync()
        out = pull(m, 0.1)
        assert np.abs(out[2000:]).max() == 0.0
        assert np.abs(np.diff(out[:, 0])).max() < 0.1       # faded, not cut
        m.sync([AudioCue("a", str(f), 0.8)])                # sync returns: sound returns
        assert np.abs(pull(m, 0.2)).max() > 0.1
    finally:
        m.close()


def test_underrun_plays_silence_and_recovers(tmp_path):
    m = Mixer(RATE, 2)
    try:
        f = index_file(tmp_path / "idx.wav", 10)
        m.sync([AudioCue("a", str(f), 0.0)])
        # pull immediately, before the decoder can have produced anything useful: must not raise or block
        for _ in range(3):
            m.render(480)
        assert m.wait_ready()
        out = pull(m, 0.3)
        idx = frame_index(out)
        assert np.all(np.isfinite(out))
        assert idx[-1] > 0
        assert np.array_equal(np.diff(idx[-2000:]), np.ones(1999))
    finally:
        m.close()


def test_end_of_file_goes_silent(mixer, tmp_path):
    f = tone_file(tmp_path / "t.wav", 0.5)
    start(mixer, [AudioCue("a", str(f), 0.0)])
    out = pull(mixer, 1.0)
    assert np.abs(out[:20000]).max() > 0.1
    assert np.abs(out[-4000:]).max() == 0.0
