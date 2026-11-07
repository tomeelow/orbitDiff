import numpy as np
import pytest

from orbitdiff.radiometry import dark_target, offset_dn, to_reflectance


@pytest.mark.parametrize(
    ("baseline", "offset_removed", "expected"),
    [
        # Cases checked against pixel values in docs/spikes.md, section 2.
        ("00.01", False, 0),  # Earth Search l2a, 2017 (non-ESA processor)
        ("03.01", False, 0),  # Earth Search l2a, before the offset existed
        # Earth Search l2a, Feb 2022: offset still in the pixels. From March 2022 some
        # unflagged items lack it; masking.offset_suspect catches those (spike 5).
        ("04.00", False, -1000),
        ("05.00", True, 0),  # Earth Search l2a, reprocessed: offset already removed
        ("05.11", True, 0),  # Earth Search l2a, current
        ("05.00", False, -1000),  # Earth Search c1 (no flag): offset in the pixels
        ("05.11", False, -1000),  # Planetary Computer (no flag): offset in the pixels
    ],
)
def test_offset_rule(baseline, offset_removed, expected):
    assert offset_dn(baseline, offset_removed) == expected


@pytest.mark.parametrize("baseline", ["N05.11", "5.11", "", "05.1"])
def test_unexpected_baseline_is_rejected(baseline):
    with pytest.raises(ValueError):
        offset_dn(baseline, False)


def test_reflectance_keeps_nodata_as_nan():
    dn = np.array([0, 1000, 1678], np.uint16)
    refl = to_reflectance(dn, -1000)
    assert np.isnan(refl[0])
    np.testing.assert_allclose(refl[1:], [0.0, 0.0678], atol=1e-6)


def test_same_pass_reads_the_same_after_harmonizing():
    # Red median of one pass, 2021-08-15: Earth Search 05.00 (offset removed) vs c1 05.00.
    es = to_reflectance(np.array([678], np.uint16), offset_dn("05.00", True))
    c1 = to_reflectance(np.array([1678], np.uint16), offset_dn("05.00", False))
    np.testing.assert_allclose(es, c1)


def test_dark_target_is_a_low_percentile_of_the_darker_band_over_reference_pixels():
    blue = np.array([[[0.02, 0.30, 0.05, 0.01, 0.9]]], np.float32)
    red = np.array([[[0.04, 0.01, 0.05, np.nan, 0.0]]], np.float32)
    reference = np.array([[[True, True, True, True, False]]])  # no data and non-reference out
    assert dark_target(blue, red, reference, percentile=0, min_pixels=3).tolist() == [
        pytest.approx(0.01)
    ]
    assert np.isnan(dark_target(blue, red, reference, percentile=0, min_pixels=4)).all()
