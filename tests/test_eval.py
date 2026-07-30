from scripts import validate


def test_eval_buckets_and_manifest() -> None:
    data = validate.load_data()
    validate.validate_eval(data)
    validate.validate_manifest(data)
