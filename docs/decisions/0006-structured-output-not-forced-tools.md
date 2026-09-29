# ADR 6: JSON-schema structured output, validated locally

**Choice.** Ask for `output_config.format` JSON schema, then validate with Pydantic ourselves and allow one
repair attempt that feeds the validation error back. Forced `tool_choice` is avoided because current models
reject it. A provider-neutral base class owns retries, repair and usage tracking; adapters are ~60 lines.
**Tradeoff.** Schema constraints the provider cannot enforce (ranges, lengths) are enforced locally.
