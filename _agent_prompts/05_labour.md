# Step 5 — Labour estimation

Estimate labour per package using the relevant rate card and gang make-up.
Account for access, height and site constraints captured in the context.

## Labour rates source

Use rates in this order of precedence (the context's `labour_rates.source`
field tells you which applies):

1. **Project upload** (`source = "project"`): a labour-rates document was
   uploaded with the project — use those rates.
2. **Shared library fallback** (`source = "library"`): no project rates were
   uploaded, so the most recently uploaded document from the contractor's
   shared labour-rates library has been included among the documents — its
   filename is prefixed "[Shared labour rates] ". Use those rates, and state
   in the assumptions that shared/general rates were applied rather than
   project-specific ones.
3. **None** (`source = "none"`): no rates are available — estimate labour from
   reasonable internal assumptions and flag every assumed rate clearly in the
   assumptions section for the estimator to review.

Whichever source is used, record it (with the document name) in the estimate's
assumptions so the client-facing output is traceable.
