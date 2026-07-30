from scripts import validate


def test_catalog_mock_contract() -> None:
    validate.validate_catalog(validate.load_data())
