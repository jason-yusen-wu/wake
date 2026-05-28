/// Byte-range identity for a node within a single file.
/// Using the byte range (rather than a generated integer) keeps IDs stable
/// across re-parses of unchanged regions and avoids a separate interner in Phase 1.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, salsa::Update)]
pub struct NodeId {
    pub start_byte: u32,
    pub end_byte: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub enum DefKind {
    Parameter,
    Assign,
}

/// Confidence carried on an edge or fact.
/// `Unknown` means "we cannot rule this out but cannot confirm it either" —
/// it is first-class ignorance, never a false assertion.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub enum Confidence {
    Definite,
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct Def {
    pub node: NodeId,
    pub symbol: String,
    pub kind: DefKind,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct Use {
    pub node: NodeId,
    pub symbol: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct DefUseEdge {
    pub def_node: NodeId,
    pub use_node: NodeId,
    pub confidence: Confidence,
}

/// A region the extractor cannot reason about — declared ignorance.
/// Emitted for ERROR nodes, unsupported constructs, and dynamic dispatch sites.
#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct Unknown {
    pub node: NodeId,
    pub reason: String,
}

/// A single fact in execution order within a function body.
/// The ordered list is the primary representation: the IR crate walks it to
/// compute reaching-defs without needing a separate sequencing pass.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub enum Fact {
    Def(Def),
    Use(Use),
    /// An opaque region the extractor cannot decompose (control flow, error nodes).
    /// The IR treats this as a barrier: defs before it may or may not reach uses after it.
    Unknown(Unknown),
}

#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub struct FunctionFacts {
    pub func_node: NodeId,
    /// Facts in execution order (RHS uses precede LHS def for assignments).
    pub facts: Vec<Fact>,
}

#[derive(Clone, Debug, PartialEq, Eq, Default, salsa::Update)]
pub struct FileFacts {
    pub functions: Vec<FunctionFacts>,
    /// Parse errors or unsupported constructs at file / module level.
    pub file_unknowns: Vec<Unknown>,
}

// ── Nullability property types ────────────────────────────────────────────────

/// Three-valued nullability lattice (precision-over-soundness).
///
/// Join at merge points: any disagreement → Unknown (we lose certainty rather
/// than emit a spurious report).  Only Nullable triggers a regression report.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub enum NullabilityValue {
    NonNull,  // positive evidence: definitely not None
    Nullable, // positive evidence: can be None — a consumer is a regression
    Unknown,  // no positive evidence either way — do not report
}

impl NullabilityValue {
    /// Precision-over-soundness join: disagreement → Unknown, not Nullable.
    pub fn join(self, other: Self) -> Self {
        if self == other { self } else { NullabilityValue::Unknown }
    }
}

/// Nullability classification of the right-hand side of an assignment.
#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub enum RhsNullability {
    /// Constant: the literal `None`, a string literal, an integer, etc.
    Literal(NullabilityValue),
    /// Copied from another local: `x = y`
    FromVar(String),
    /// Anything we cannot classify: binary ops, attribute access, calls, etc.
    Unknown,
}

/// Kind of a consumer site — a place where a None value would cause a runtime error.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub enum ConsumerKind {
    Attribute, // x.attr  — AttributeError if x is None
    Subscript, // x[i]   — TypeError if x is None
    Call,      // x()    — TypeError if x is None
}

/// A site that would fail at runtime if the consumed variable is None.
#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct Consumer {
    /// The whole expression node (e.g., the `attribute` / `subscript` / `call` node).
    pub node: NodeId,
    /// The local variable being dereferenced / called / subscripted.
    pub object_symbol: String,
    pub kind: ConsumerKind,
}

/// Nullability information for one definition (parameter or assignment).
#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct NullDef {
    /// The node of the defined name.
    pub node: NodeId,
    pub symbol: String,
    /// Nullability from the type annotation (Unknown if absent or unrecognised).
    pub annotation: NullabilityValue,
    /// Nullability of the right-hand side (Unknown for parameters: they have no RHS).
    pub rhs: RhsNullability,
}

/// A single nullability-relevant fact in execution order within a function body.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub enum NullFact {
    /// A parameter with an optional type annotation.
    Param(NullDef),
    /// An assignment (plain or annotated).
    Assign(NullDef),
    /// A site that consumes a local variable (attribute access, subscript, call).
    Consumer(Consumer),
    /// A control-flow barrier or parse error: clears the entire reaching state.
    Unknown(NodeId),
}

/// All nullability-relevant facts for one function, in execution order.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub struct NullFunctionFacts {
    pub func_node: NodeId,
    pub facts: Vec<NullFact>,
}

/// All nullability facts for a file.
#[derive(Clone, Debug, PartialEq, Eq, Default, salsa::Update)]
pub struct NullFileFacts {
    pub functions: Vec<NullFunctionFacts>,
}

/// A confirmed potential None-dereference: the consumer variable is Nullable at this site.
#[derive(Clone, Debug, PartialEq, Eq, Hash, salsa::Update)]
pub struct NullRegression {
    pub func_node: NodeId,
    pub consumer_node: NodeId,
    pub object_symbol: String,
    pub kind: ConsumerKind,
}
