RG UPDATED V2 - Product Hide/Unhide + Admin Navigation Hardening

Changes in this revision:
1. Hidden products remain absent from the customer Store.
2. Hidden plans remain absent from the product's customer plan list.
3. Hidden products and hidden plans are excluded from the reseller/public API catalog, so their public Product ID / Variant(Validity) ID is not returned in catalog results.
4. Existing API ID mappings are preserved for future Unhide; they are not deleted.
5. Public API mapped-plan lookup now rejects hidden products/plans, so a hidden Product ID + Variant/Validity ID cannot be used through the normal reseller API purchase path.
6. Checkout re-checks visibility for stale Store/Buy buttons.
7. Wallet checkout re-checks visibility immediately before charging.
8. Coupon checkout rejects hidden products/plans before consuming a coupon.
9. Verified UPI payment that completes after an item was hidden is handled through the existing paid-delivery-failure compensation path and returned to the user's wallet once.
10. Existing Admin Back-button/navigation hardening remains intact; Admin input prompts receive a Back button and the universal Admin navigation stack is preserved.

No existing product, plan, payment, API, or user-panel feature was intentionally removed.

Latest Admin UI hardening:
- Fixed missing Back buttons on legacy Admin next-step prompts by resolving the Admin from the private chat ID instead of the outgoing bot message's from_user.
- Add Product / Plan flow keeps Back available throughout every input step and after a product/plan is created.
- The four User Panel Back buttons remain editable in Button Colors and All Button Emoji.
- All Button Emoji uses the requested two-column layout.
- Backup Center lists now show a dedicated Download Backup button beneath every archive.
