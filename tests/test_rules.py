from scripts import validate


def test_outfit_and_shopping_rules() -> None:
    data = validate.load_data()
    validate.validate_outfit_rules(data)
    validate.validate_shopping_rules(data)
