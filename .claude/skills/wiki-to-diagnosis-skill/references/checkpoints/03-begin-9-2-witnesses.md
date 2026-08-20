# Controlled checkpoint 03: begin 9.2 witnesses

- checkpoint_schema_version: 1
- checkpoint_id: begin_9_2_witnesses
- control_only: true
- must_not_enter_generation_spec: true

This checkpoint contains no source facts, business semantics, defaults, field values, or output content. Do not copy,
summarize, encode, cite, or otherwise place this file, its path, its id, or its instructions in the generated object.

The blueprint stages are complete. Do not restate them or materialize the compact IR root. Execute the already loaded section
9.1 check exactly once and then section 9.2 exactly once against the compact blueprints, retaining only compact internal
pass or failure states and never narrating the inventory or emitting witnesses. If both pass, end this stage with exactly one `Read` of the actual
Skill base directory joined with `references/checkpoints/04-write-now.md`. If it fails, stop without reading the next
checkpoint or submitting.
