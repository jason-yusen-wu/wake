use proptest::prelude::*;
use wake_schema::{LatticeDomain, NullabilityValue};

fn any_value() -> impl Strategy<Value = NullabilityValue> {
    prop_oneof![
        Just(NullabilityValue::NonNull),
        Just(NullabilityValue::Nullable),
        Just(NullabilityValue::Unknown),
    ]
}

proptest! {
    /// join is commutative: a ⊔ b == b ⊔ a
    #[test]
    fn join_commutative(a in any_value(), b in any_value()) {
        prop_assert_eq!(a.clone().join(b.clone()), b.join(a));
    }

    /// join is idempotent: a ⊔ a == a
    #[test]
    fn join_idempotent(a in any_value()) {
        prop_assert_eq!(a.clone().join(a.clone()), a);
    }

    /// join is associative: (a ⊔ b) ⊔ c == a ⊔ (b ⊔ c)
    #[test]
    fn join_associative(a in any_value(), b in any_value(), c in any_value()) {
        let lhs = a.clone().join(b.clone()).join(c.clone());
        let rhs = a.join(b.join(c));
        prop_assert_eq!(lhs, rhs);
    }

    /// bottom joined with anything is Unknown (Unknown is ⊤ in this lattice):
    /// NonNull ⊔ Nullable = Unknown; Unknown ⊔ x = Unknown.
    #[test]
    fn bottom_is_unknown(a in any_value()) {
        let bottom = NullabilityValue::bottom();
        // Unknown ⊔ a: if a is Unknown, stays Unknown; otherwise Unknown (top absorbs all).
        let result = bottom.clone().join(a.clone());
        prop_assert_eq!(result, NullabilityValue::Unknown);
    }

    /// NonNull and Nullable are incomparable: their join is Unknown.
    #[test]
    fn nonnull_nullable_join_is_unknown(
        a in prop_oneof![Just(NullabilityValue::NonNull), Just(NullabilityValue::Nullable)],
        b in prop_oneof![Just(NullabilityValue::NonNull), Just(NullabilityValue::Nullable)],
    ) {
        if a != b {
            prop_assert_eq!(a.join(b), NullabilityValue::Unknown);
        }
    }
}
