# Reviewer training-data boundary

This package implements the safe, provider-neutral preparation boundary for a
future fine-tuned Reviewer. It validates human- or audit-supplied real-catch
JSONL records, rejects malformed and duplicate examples, and exports chat-style
JSONL. It does **not** invent examples, call a model provider, upload data, or
claim that fine-tuning has occurred.

The current repository contains a 25-example behavioral seed set for retrieval,
not a sufficient battle-tested fine-tuning corpus. Fine-tuning should begin
only after the retrieval store has grown substantially, repository-context
signals have been evaluated across a wider repository corpus, and the training
split is independently audited for real catches, leakage, and reviewer/patcher
disjointness.

## Prepare a dataset

The retrieval seed intentionally does **not** pass this boundary because it has
no repository, review, commit, or executable before/after evidence fields. Run
the CLI only on a separately collected and audited real-catch JSONL file:

```bash
python scripts/prepare_reviewer_dataset.py \
  path/to/audited_real_catches.jsonl \
  build/reviewer-sft.jsonl
```

The output is provider-neutral. A future training adapter must add provider-
specific authentication and job submission outside this package, with explicit
human approval and dataset lineage. The runtime Reviewer integration point
remains `core/agents.py`; until a validated fine-tuned model exists, Janus uses
behavioral retrieval plus repository-context retrieval and requires executable
proof for `ISSUE_FOUND` verdicts.
