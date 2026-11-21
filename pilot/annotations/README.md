# Agent annotation contract

Each claim is one source-grounded assertion, not an automatically inferred causal relation.

Required JSONL fields: `claim_id`, `doc_id`, `statement`, `material_system`, `sample_id`, `stage`, `quantity`, `value_text`, `unit`, `conditions`, `evidence`, `claim_type`, `review_status`.

`evidence` is a list of objects with `pdf_page` (one-based physical PDF page), `quote` (exact source text, whitespace may be normalized), and optionally `evidence_id` from parsed elements. Use multiple evidence entries when conditions and results occur separately. The quote must contain the stated value and enough context to bind it to the experiment. Do not use invented quotes or paraphrases in this field.

`conditions` is an object. Unknown values are null, not guessed. `stage` should distinguish `surface_preparation`, `contact_bonding`, `post_bond_annealing`, `measurement`, `reliability_test`, `material_characterization`, or `background`. `claim_type`: `reported_experiment`, `reported_simulation`, `author_interpretation`, `review_summary`, `cited_prior_work`.

`review_status` initially `agent_extracted_pending_independent_review`. Full text collection and source location do not by themselves establish factual correctness. Do not treat cited prior work as the collecting paper's own experiment.
