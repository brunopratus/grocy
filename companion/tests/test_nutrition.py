import pytest

from app.nutrition import (
    Per100,
    Totals,
    calories_per_stock_unit,
    nutrients_for,
    parse_off_nutriments,
    parse_off_product,
    parse_usda_food,
)

PASTA = Per100(kcal=359, protein=12.5, fat=1.5, satfat=0.3, carbs=71, sugar=3.5, fiber=3, salt=0.01)


def test_userfield_roundtrip():
    uf = PASTA.to_userfields()
    assert uf["kcal_100"] == 359 and uf["protein_100"] == 12.5
    assert Per100.from_userfields({**uf, "fat_100": ""}).fat is None
    assert Per100.from_userfields(uf) == PASTA


def test_grams_product():
    t = nutrients_for("Pasta", 250, "g", PASTA)
    assert t.kcal == pytest.approx(897.5)
    assert t.protein == pytest.approx(31.25)
    assert not t.missing


def test_kg_and_pieces():
    assert nutrients_for("Pasta", 0.5, "kg", PASTA).kcal == pytest.approx(1795)
    egg = Per100(kcal=143, protein=12.6, fat=9.5, carbs=0.7)
    # 2 eggs of 60 g
    assert nutrients_for("Eggs", 2, "piece", egg, 60, "g").kcal == pytest.approx(171.6)


def test_missing_data():
    assert nutrients_for("Mystery", 100, "g", Per100()).missing == ["Mystery"]
    assert nutrients_for("Eggs", 2, "piece", Per100(kcal=143)).missing == ["Eggs"]


def test_totals_math():
    a = nutrients_for("Pasta", 200, "g", PASTA)
    b = nutrients_for("X", 10, "g", Per100())
    total = a.add(b)
    assert total.kcal == pytest.approx(718)
    assert total.missing == ["X"]
    per = total.divided(2)
    assert per.kcal == pytest.approx(359)
    assert total.times(0.5).kcal == pytest.approx(359)
    assert Totals().divided(0).kcal == 0


def test_calories_per_stock_unit():
    assert calories_per_stock_unit(PASTA, "g") == pytest.approx(3.59)
    assert calories_per_stock_unit(PASTA, "pack", 500, "g") == pytest.approx(1795)
    assert calories_per_stock_unit(Per100(), "g") is None


def test_off_kj_and_sodium():
    p = parse_off_nutriments({"energy-kj_100g": 1506, "sodium_100g": 0.4, "proteins_100g": "5"})
    assert p.kcal == pytest.approx(359.9, abs=0.1)
    assert p.salt == pytest.approx(1.0)
    assert p.protein == 5


def test_off_product_quantity():
    prod = parse_off_product(
        "123",
        {
            "product_name": "Spaghetti",
            "brands": "Barilla, X",
            "quantity": "1 kg",
            "nutriments": {"energy-kcal_100g": 359},
        },
    )
    assert (prod.net_amount, prod.net_unit, prod.brand) == (1000, "g", "Barilla")
    assert prod.nutrition.kcal == 359 and not prod.nutrition.is_complete()


def test_usda_foundation_energy_fallback():
    food = {
        "fdcId": 1,
        "description": "Carrots, raw",
        "dataType": "Foundation",
        "foodNutrients": [
            {"nutrientNumber": "958", "unitName": "KCAL", "value": 48},
            {"nutrientNumber": "203", "unitName": "G", "value": 0.94},
            {"nutrientNumber": "307", "unitName": "MG", "value": 69},
            {"nutrientNumber": "205", "unitName": "G", "value": 10.3},
            {"nutrientNumber": "204", "unitName": "G", "value": 0.35},
        ],
    }
    f = parse_usda_food(food)
    assert f.nutrition.kcal == 48
    assert f.nutrition.salt == pytest.approx(0.1725)
    assert f.nutrition.is_complete()
