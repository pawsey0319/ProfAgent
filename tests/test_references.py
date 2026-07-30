from scripts import validate


def test_all_required_references_exist() -> None:
    validate.validate_references(validate.load_data())
