# S12R real CPA image-provider evidence

- Captured: 2026-08-18 17:39 (Asia/Shanghai)
- Environment: `torch128`
- Command: `conda run --no-capture-output -n torch128 python reports/runtime/s12_real_image_provider_smoke.py`
- Result: `ok`, `image/jpeg`, `b64_json`, 228,841 bytes, 5,288.5 ms
- SHA-256: `83846098fecb05308fba9b1141f5d7274079025f171bf2bc76af08632016d629`

The provider fixed the request and transport model to `grok-imagine-image-quality` and verified a controlled CPA receipt. CPA did **not** report an actual model, so the authoritative result remains:

- `model_reported=false`
- `model_verified=false`
- `resolved_model=null`
- `verification_basis=exact_request_with_cpa_trace`

The report contains no raw CPA receipt, image body, prompt, API key, or authorization header. The generated smoke artifact remains under ignored `reports/runtime/` and is not release evidence by itself.
