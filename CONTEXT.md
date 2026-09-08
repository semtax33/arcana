# Arcana Financial Semantics

Korean and international disclosure facts are normalized without erasing their
source meaning, publication timing, or comparability limits.

## Language

**Reported Fact**:
A value exactly as disclosed, together with its label, period, unit, scope, accounting regime, and provenance.
_Avoid_: raw value, parsed number

**Canonical Fact**:
A Reported Fact that passed deterministic context constraints and is linked to one canonical account.
_Avoid_: mapped number, standardized value

**Harmonized Fact**:
A Canonical Fact made comparable across accounting regimes by an explicit bridge rule that records any semantic loss.
_Avoid_: Canonical Fact, normalized value

**Financial Availability Date**:
The earliest evidenced publication date on which a disclosed fact may be used in a point-in-time calculation.
_Avoid_: fiscal period end, report year

**Abstention**:
A deliberate absence of a fact or factor because required evidence is missing or contradictory; it is never represented as zero.
_Avoid_: missing zero, default value

**Market-applicable Factor**:
A factor whose contract is meaningful and whose required source class is available for the named market.
_Avoid_: global factor count

**Factor Cell Coverage**:
The share of eligible security-date-factor opportunities that contain finite materialized values under one declared market, period, and basis.
_Avoid_: processing completion, factor-ID coverage

**Invariant Evidence**:
The PASS, REVIEW, or NOT_TESTABLE result of an accounting identity over semantically comparable facts; it cannot change a mapping by itself.
_Avoid_: auto-correction, mapping oracle

**Source Completeness**:
Evidence that every target security and requested source interval was checked, including explicit terminal no-data outcomes.
_Avoid_: non-empty cache, successful sample

**SEC Filing Bundle**:
An accession-scoped 10-K or 10-Q package containing the primary filing, XBRL instance, extension schema, available label/presentation/definition/calculation linkbases, and immutable filing metadata.
_Avoid_: filing HTML, company facts response

**Filing Source Authority**:
The evidence precedence `SEC Filing Bundle > SEC Company Facts > Financial Statement and Notes Data Set`; a lower tier is eligible only when the filing bundle for that security-period is absent.
_Avoid_: merge priority, best available number

**Reported Period Semantic**:
The filing-context meaning `INSTANT`, `QTD`, `YTD`, or `FY` retained with a Canonical Fact; fiscal quarter labels alone do not determine it.
_Avoid_: quarter, period end

**US Semantic Rule Bundle**:
The SHA-256-pinned HMRB-style `.arcana` ruleset that expresses source applicability, fact matching, captures, constraints, and canonical emission for SEC facts.
_Avoid_: US mapping YAML, tag dictionary
