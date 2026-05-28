use std::collections::HashMap;
use wake_diff::WitnessStep;
use wake_engine::{Db, SourceFile};
use wake_schema::{ConsumerKind, NodeId};

// ── Confidence ────────────────────────────────────────────────────────────────

/// Confidence in a shaped finding, based on how fully the witness is traceable.
///
/// High: every step is concrete (no Opaque gaps).
/// Medium: one Opaque step — source partially known.
/// Low: two or more Opaque steps — origin murky.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, salsa::Update)]
pub enum Confidence {
    Low,
    Medium,
    High,
}

impl Confidence {
    fn from_witness(witness: &[WitnessStep]) -> Self {
        let opaque = witness.iter().filter(|s| matches!(s, WitnessStep::Opaque { .. })).count();
        match opaque {
            0 => Confidence::High,
            1 => Confidence::Medium,
            _ => Confidence::Low,
        }
    }
}

// ── Root cause ────────────────────────────────────────────────────────────────

/// The origin of a None flow: the earliest concrete fact in the witness.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub enum RootCause {
    /// A direct `x = None` assignment.
    NoneAssignment { node: NodeId, symbol: String },
    /// A parameter with an Optional/nullable annotation.
    NullableParam { node: NodeId, symbol: String },
    /// Source could not be traced (depth limit or opaque expression).
    Opaque { description: String },
}

impl RootCause {
    fn from_witness(witness: &[WitnessStep]) -> Self {
        match witness.first() {
            Some(WitnessStep::NoneAssignment { node, symbol }) => {
                RootCause::NoneAssignment { node: *node, symbol: symbol.clone() }
            }
            Some(WitnessStep::NullableParam { node, symbol }) => {
                RootCause::NullableParam { node: *node, symbol: symbol.clone() }
            }
            Some(WitnessStep::Opaque { symbol }) => {
                RootCause::Opaque { description: symbol.clone() }
            }
            Some(_) => RootCause::Opaque { description: "unknown".to_string() },
            None => RootCause::Opaque { description: "empty witness".to_string() },
        }
    }

    fn dedup_key(&self) -> DedupeKey {
        match self {
            RootCause::NoneAssignment { node, .. } => DedupeKey::Node(*node),
            RootCause::NullableParam { node, .. } => DedupeKey::Node(*node),
            RootCause::Opaque { description } => DedupeKey::Opaque(description.clone()),
        }
    }

    /// The source location the fix should be applied at, if known.
    pub fn fix_locus(&self) -> Option<NodeId> {
        match self {
            RootCause::NoneAssignment { node, .. } => Some(*node),
            RootCause::NullableParam { node, .. } => Some(*node),
            RootCause::Opaque { .. } => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum DedupeKey {
    Node(NodeId),
    Opaque(String),
}

// ── Affected consumer ─────────────────────────────────────────────────────────

/// One None-dereference site that shares a root cause with other consumers.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub struct AffectedConsumer {
    /// The node at which the None-dereference occurs.
    pub node: NodeId,
    /// The dereferenced variable.
    pub symbol: String,
    /// The kind of dereference (attribute, subscript, call).
    pub kind: ConsumerKind,
    /// The full backward witness for this specific consumer.
    pub witness: Vec<WitnessStep>,
}

// ── Shaped feedback ───────────────────────────────────────────────────────────

/// A single agent-actionable finding: one root cause, all its affected consumers,
/// ranked by proximity and confidence.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub struct ShapedFeedback {
    /// The root cause of the None flow.
    pub root_cause: RootCause,
    /// All consumer sites that flow from this root cause.
    /// Sorted: shorter witness (= closer to root) first.
    pub consumers: Vec<AffectedConsumer>,
    /// Overall confidence, set to the best achievable across all consumers.
    pub confidence: Confidence,
}

impl ShapedFeedback {
    /// Source location where a fix should be applied.
    pub fn fix_locus(&self) -> Option<NodeId> {
        self.root_cause.fix_locus()
    }
}

// ── Core shaping logic ────────────────────────────────────────────────────────

/// Turn a list of `RegressionReport`s into deduplicated, ranked `ShapedFeedback` items.
///
/// **Deduplication**: reports whose witnesses share the same root-cause node are
/// merged into one `ShapedFeedback` with multiple `AffectedConsumer`s. This is the
/// Phase 5 gate: one message per root cause, not one per consumer.
///
/// **Ranking**: by `Confidence` descending, then by number of consumers descending
/// (broader blast radius = higher priority for the agent).
pub fn shape_feedback(reports: &[wake_diff::RegressionReport]) -> Vec<ShapedFeedback> {
    // Accumulate groups keyed by the root-cause dedup key.
    let mut groups: HashMap<DedupeKey, (RootCause, Vec<AffectedConsumer>)> = HashMap::new();

    for report in reports {
        let root_cause = RootCause::from_witness(&report.witness);
        let key = root_cause.dedup_key();

        // Extract the Consumer step (always the last in a well-formed witness).
        let consumer = extract_consumer(report);

        let entry = groups.entry(key).or_insert_with(|| (root_cause, Vec::new()));
        entry.1.push(consumer);
    }

    // Build and rank the output.
    let mut result: Vec<ShapedFeedback> = groups
        .into_values()
        .map(|(root_cause, mut consumers)| {
            // Within a group, sort consumers by witness length: closer (shorter) first.
            consumers.sort_by_key(|c| c.witness.len());

            // Confidence = highest achievable across all consumers for this root cause.
            let confidence = consumers
                .iter()
                .map(|c| Confidence::from_witness(&c.witness))
                .max()
                .unwrap_or(Confidence::Low);

            ShapedFeedback { root_cause, consumers, confidence }
        })
        .collect();

    // Sort: confidence desc, then consumer count desc.
    result.sort_by(|a, b| {
        b.confidence
            .cmp(&a.confidence)
            .then(b.consumers.len().cmp(&a.consumers.len()))
    });

    result
}

fn extract_consumer(report: &wake_diff::RegressionReport) -> AffectedConsumer {
    // The Consumer step is always the last step of a well-formed witness.
    if let Some(WitnessStep::Consumer { node, symbol, kind }) = report.witness.last() {
        return AffectedConsumer {
            node: *node,
            symbol: symbol.clone(),
            kind: *kind,
            witness: report.witness.clone(),
        };
    }
    // Fallback: pull info from the regression directly (witness was empty or malformed).
    AffectedConsumer {
        node: report.regression.consumer_node,
        symbol: report.regression.object_symbol.clone(),
        kind: report.regression.kind,
        witness: report.witness.clone(),
    }
}

// ── Token-budget witness trimming ─────────────────────────────────────────────

/// Trim a witness to at most `max_steps` steps.
///
/// The first step (root cause) and last step (Consumer) are always preserved.
/// Middle steps are truncated and replaced with an `Opaque` truncation marker,
/// so the agent sees where the flow starts and ends even under a token budget.
pub fn trim_witness(witness: &[WitnessStep], max_steps: usize) -> Vec<WitnessStep> {
    if witness.len() <= max_steps {
        return witness.to_vec();
    }
    if max_steps == 0 {
        return vec![];
    }
    if max_steps == 1 {
        return witness.last().cloned().into_iter().collect();
    }
    // Keep `max_steps - 2` leading steps + Opaque marker + last step.
    let keep_lead = max_steps.saturating_sub(2);
    let mut result: Vec<WitnessStep> = witness[..keep_lead].to_vec();
    result.push(WitnessStep::Opaque { symbol: "… (truncated)".to_string() });
    if let Some(last) = witness.last() {
        result.push(last.clone());
    }
    result
}

// ── Salsa-tracked pipeline entry ──────────────────────────────────────────────

/// Compute shaped feedback for `file` — the full pipeline from source to
/// deduplicated, ranked findings.
///
/// Memoized by salsa: only recomputes if the file content (or its transitive
/// dependencies) change.
#[salsa::tracked]
pub fn shaped_regressions(db: &dyn Db, file: SourceFile) -> Vec<ShapedFeedback> {
    let reports = wake_diff::regressions_with_witnesses(db, file);
    shape_feedback(&reports)
}
