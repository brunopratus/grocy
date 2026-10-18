import pytest

from app.units import UnitError, convert, fmt_amount, normalize_unit, parse_quantity


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("g", "g"),
        ("Gram", "g"),
        ("KG", "kg"),
        ("Litre", "l"),
        ("ml", "ml"),
        ("Piece", "piece"),
        ("pcs", "piece"),
        ("vnt.", "piece"),
        ("szt", "piece"),
        ("Pack", "pack"),
        ("packs", "pack"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_unit(raw) == expected


def test_normalize_unknown():
    with pytest.raises(UnitError):
        normalize_unit("bushel")


def test_mass_and_volume():
    assert convert(1.5, "kg", "g") == 1500
    assert convert(250, "g", "kg") == 0.25
    assert convert(2, "l", "ml") == 2000
    assert convert(5, "dl", "ml") == 500


def test_pack_to_grams_and_back():
    # 1 kg pasta pack: "500 g of a 1 kg pack"
    assert convert(1, "pack", "g", net_amount_per_pack=1, net_unit="kg") == 1000
    assert convert(500, "g", "pack", net_amount_per_pack=1000, net_unit="g") == 0.5
    assert convert(2, "piece", "g", net_amount_per_pack=60, net_unit="g") == 120


def test_count_without_pack_size_fails():
    with pytest.raises(UnitError):
        convert(1, "pack", "g")


def test_mass_to_volume_fails():
    with pytest.raises(UnitError):
        convert(100, "g", "ml")
    with pytest.raises(UnitError):
        convert(1, "pack", "ml", net_amount_per_pack=500, net_unit="g")


def test_piece_and_pack_are_items():
    assert convert(3, "piece", "pack") == 3


@pytest.mark.parametrize(
    "text,expected",
    [
        ("500 g", (500, "g")),
        ("1 kg", (1000, "g")),
        ("1,5 l", (1500, "ml")),
        ("4 x 125 g", (500, "g")),
        ("330ml", (330, "ml")),
        ("6 vnt", (6, "piece")),
        ("", None),
        (None, None),
        ("big", None),
    ],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


def test_fmt_amount():
    assert fmt_amount(1500, "g") == "1.5 kg"
    assert fmt_amount(500, "g") == "500 g"
    assert fmt_amount(1, "piece") == "1 piece"
    assert fmt_amount(2, "pack") == "2 packs"
