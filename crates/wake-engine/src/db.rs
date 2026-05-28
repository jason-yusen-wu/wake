#[salsa::db]
pub trait Db: salsa::Database {}

#[salsa::input]
pub struct SourceFile {
    #[returns(ref)]
    pub contents: String,
}

/// A set of source files analyzed together (cross-file value flow). The path is
/// the stable identity used to attribute regressions to a file across edits.
#[salsa::input]
pub struct Workspace {
    #[returns(ref)]
    pub files: Vec<(String, SourceFile)>,
}

#[salsa::db]
#[derive(Default)]
pub struct Database {
    storage: salsa::Storage<Self>,
}

#[salsa::db]
impl salsa::Database for Database {}

#[salsa::db]
impl Db for Database {}
