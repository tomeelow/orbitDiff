"""The building model's data and its champion, without PyTorch: OSCD's download and
layout, which model the product runs, and the refusal to promote without verified labels."""

import hashlib
import zipfile

import numpy as np
import pytest
import rasterio

from orbitdiff import buildings, cli, oscd
from orbitdiff.config import OscdConfig

ZIP = "Onera Satellite Change Detection dataset - Images.zip"


def write_tif(path, data: np.ndarray) -> None:
    """A plain GeoTIFF with no CRS, as OSCD ships them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {"driver": "GTiff", "width": data.shape[1], "height": data.shape[0], "count": 1}
    with rasterio.open(path, "w", dtype=data.dtype, **profile) as dst:
        dst.write(data, 1)


def zipped(tmp_path, name: str = ZIP) -> tuple[OscdConfig, bytes]:
    """A one-file mirror of OSCD's images zip, served from disk."""
    source = tmp_path / "mirror"
    source.mkdir()
    with zipfile.ZipFile(source / name, "w") as z:
        z.writestr(f"{name[:-4]}/train.txt", "paris,rennes")
    data = (source / name).read_bytes()
    url = source.as_uri() + "/"
    return OscdConfig(url=url, files={name: hashlib.sha256(data).hexdigest()}), data


def test_oscd_is_downloaded_once_and_unpacked(tmp_path):
    cfg, _ = zipped(tmp_path)
    folder = oscd.download(cfg, tmp_path / "oscd")
    assert oscd.cities(folder, "train") == ["paris", "rennes"]
    assert not (folder / ZIP).exists()  # the zip goes once unpacked

    (tmp_path / "mirror" / ZIP).unlink()  # a second call fetches nothing
    assert oscd.download(cfg, tmp_path / "oscd") == folder


def test_an_oscd_zip_that_doesnt_match_its_checksum_is_refused_and_deleted(tmp_path):
    cfg, _ = zipped(tmp_path)
    tampered = cfg.model_copy(update={"files": {ZIP: "0" * 64}})
    with pytest.raises(ValueError, match="doesn't match its pinned SHA-256"):
        oscd.download(tampered, tmp_path / "oscd")
    assert not (tmp_path / "oscd" / ZIP).exists()


def test_an_oscd_city_reads_our_four_bands_as_reflectance_and_its_change_labels(tmp_path):
    city = tmp_path / oscd.IMAGES / "rennes"
    for date, dn in ((1, 1000), (2, 2000)):
        for i, band in enumerate(("B01", "B02", "B03", "B04", "B08", "B8A")):
            write_tif(city / f"imgs_{date}_rect" / f"{band}.tif", np.full((4, 6), dn + i, "uint16"))
    (city / "dates.txt").write_text("date_1: 20150821\ndate_2: 20170621\n")
    labels = np.ones((4, 6), "uint8")
    labels[1:3, 2:4] = oscd.CHANGED
    write_tif(tmp_path / oscd.LABELS["test"] / "rennes" / "cm" / "rennes-cm.tif", labels)

    c = oscd.load_city(tmp_path, "rennes", "test")

    assert c.dates == ("20150821", "20170621")
    # blue, green, red, nir = B02, B03, B04, B08: written as DN 1001..1004, read /10000
    np.testing.assert_allclose(c.before[:, 0, 0], [0.1001, 0.1002, 0.1003, 0.1004])
    np.testing.assert_allclose(c.after[:, 0, 0], [0.2001, 0.2002, 0.2003, 0.2004])
    assert c.changed.sum() == 4 and c.changed[1:3, 2:4].all()


def test_without_a_promotion_there_is_no_champion(tmp_path):
    assert buildings.champion_info(tmp_path) is None
    assert buildings.champion_named(tmp_path) is None
    assert buildings.load_champion(tmp_path) is None


def test_a_champion_is_named_without_loading_it(tmp_path):
    buildings.write_champion(tmp_path, "orbitdiff_buildings", 3, "run-1")
    info = buildings.champion_info(tmp_path)
    assert (info["version"], info["weights"], info["run_id"]) == (3, "v3.pt", "run-1")
    named = buildings.champion_named(tmp_path)
    assert named.version == "orbitdiff_buildings/3"
    with pytest.raises(RuntimeError, match="isn't loaded here"):
        named(np.zeros((4, 2, 2)), np.zeros((4, 2, 2)))


@pytest.mark.parametrize("labelled", [False, True])
def test_promotion_refuses_without_a_verified_evaluation_set(tmp_path, labelled):
    if labelled:  # a candidate file: labelled, not verified
        (tmp_path / "site.geojson").write_text(
            '{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": '
            '{"role": "aoi", "first_month": "2020-01", "last_month": "2022-12", '
            '"hard_cases": []}, "geometry": {"type": "Polygon", "coordinates": '
            "[[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]}}]}"
        )
    with pytest.raises(SystemExit, match="Refusing: promotion needs a verified evaluation set"):
        cli.main(["promote", "--version", "1", "--eval-dir", str(tmp_path)])
