//! #8: token-aware annotation parsing.

use wake_extract_py::null_extract::parse_annotation_text;
use wake_schema::NullabilityValue::{NonNull, Nullable, Unknown};

#[test]
fn nullable_forms() {
    for t in [
        "None",
        "NoneType",
        "Optional[str]",
        "Optional[Dict[str, int]]",
        "typing.Optional[int]",
        "Union[str, None]",
        "Union[None, int]",
        "str | None",
        "None | str",
        "int | None | str",
    ] {
        assert_eq!(parse_annotation_text(t), Nullable, "{t} should be Nullable");
    }
}

#[test]
fn nonnull_forms() {
    for t in ["str", "int", "list", "List[int]", "Dict[str, int]", "Tuple[int, str]"] {
        assert_eq!(parse_annotation_text(t), NonNull, "{t} should be NonNull");
    }
}

#[test]
fn substring_none_is_not_nullable() {
    // The precision leak the fix targets: a type whose name merely contains
    // "None" must not be classified Nullable.
    assert_eq!(parse_annotation_text("NoneCheck"), Unknown, "NoneCheck is an opaque type");
    assert_eq!(parse_annotation_text("MyNoneType"), Unknown, "MyNoneType is not None");
    assert_eq!(
        parse_annotation_text("Union[int, NoneCheck]"),
        Unknown,
        "union with a None-substring type but no real None is not Nullable"
    );
}

#[test]
fn unknown_forms() {
    assert_eq!(parse_annotation_text("SomeClass"), Unknown);
    assert_eq!(parse_annotation_text("Any"), Unknown);
}
