# ADR-0003: SEC filing authority and US semantic rule DSL

- Status: Accepted
- Date: 2026-09-06
- Supersedes: none; extends ADR-0001 and ADR-0002

## Context

US statement normalization previously started from SEC Company Facts, then used
the Financial Statement and Notes Data Set and a live edgartools company-facts
fallback. Primary 10-K/10-Q HTML was downloaded separately but was not a
normalization input. This erased accession-local XBRL contexts, dimensions,
extension-taxonomy relationships, accepted timestamps, and QTD/YTD distinctions.
The v1 US rules were also maintained as YAML tag lists, so the source, capture,
constraint, and evidence semantics were implicit.

## Decision

1. Every downloaded 10-K and 10-Q is stored as an accession-scoped SEC Filing
   Bundle under `data-lake/bronze/sec/fillings/{form}/{ticker}/{accession}/`.
   It contains the primary document, XBRL instance, extension schema, and all
   available label, presentation, definition, and calculation linkbases. A
   `filing.json` records hashes, filing metadata, source authority, and the
   edgartools version.
2. Local filing XBRL is the authoritative normalization input for its
   security-period. SEC Company Facts is eligible only when that filing bundle
   is absent; the Financial Statement and Notes Data Set is eligible only after
   both are absent. A present but unparsable filing blocks silent substitution
   for a deterministically identified period and emits a warning.
3. Company extension concepts are not canonicalized from label similarity
   alone. A label match requires presentation or calculation graph evidence
   linking it to a declared standard concept. Dimensions remain part of the
   evidence and consolidated, non-dimensioned facts are preferred.
4. 10-Q duration facts retain QTD/YTD semantics. Cash-flow and other duration
   selections prefer the current YTD context; derived standalone quarters must
   be represented separately rather than relabeling a YTD Reported Fact.
5. US v2 rules use the strict HMRB-style `semantic_us_v2.arcana` grammar:
   `applies`, `match fact`, `capture`, `constraint`, `emit`, and `legacy` blocks.
   All 104 v1 rules are migrated losslessly. Runtime consumers select the bundle
   through `semantic_us_rule_manifest.json`, which verifies its SHA-256 before
   parsing. Published bundles are immutable.
6. Live edgartools company-facts normalization is opt-in. edgartools remains the
   required downloader and local XBRL parser; the normal default path does not
   substitute a live API value for the declared local fallback chain.

## Rejected alternatives

- Keeping Company Facts as the primary source: it cannot preserve the complete
  accession-local DTS, context, dimension, and presentation evidence.
- Parsing only the rendered HTML table: layout is weaker semantic evidence than
  XBRL contexts and taxonomy graphs.
- Canonicalizing extension facts by English label similarity: it creates
  high-confidence false positives when company-specific concepts reuse generic
  financial wording.
- Combining QTD and YTD facts under one quarterly label: it mixes incompatible
  durations and corrupts downstream quarterly and TTM factors.
- Keeping v2 rules in YAML: it leaves rule algebra and evidence constraints
  implicit and does not satisfy the requested HMRB-like authoring model.
- Falling back when a filing exists but fails parsing: it hides source corruption
  and can replace authoritative facts with semantically narrower aggregates.

## Consequences

Initial backfills are larger because an accession includes XBRL support files,
and old HTML-only checkpoints are invalidated by bundle schema version 2.
Normalization is more conservative: a broken authoritative filing can produce
abstention rather than a lower-tier value. In exchange, every selected fact can
retain accession, accepted time, taxonomy version, unit, context, dimensions,
period semantic, and graph evidence, while the v1-to-v2 rule migration remains
machine-verifiable.
