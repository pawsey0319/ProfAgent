from scripts import validate


def test_vocab_and_schemas() -> None:
    data = validate.load_data()
    validate.validate_vocab()
    validate.validate_schemas(data)
    validate.validate_counts(data)
