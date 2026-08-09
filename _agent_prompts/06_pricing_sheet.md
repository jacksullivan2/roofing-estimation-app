# Step 6 — Pricing sheet assembly

Assemble the pricing sheet: line items with quantities, unit rates, material
and labour costs, preliminaries, then apply the project profit markup. Carry
every item qualification captured during the draft review into a
Qualifications column against its line item.

## Output target

**If a client pricing sheet is flagged** (`client_pricing_sheet.enabled =
true` in the project context, with `filename` identifying the uploaded
document): populate THAT sheet rather than creating a new workbook.

- Preserve the client's existing structure — sheet names, row order, headings,
  item descriptions and any pre-filled cells stay untouched.
- Add the estimation data by filling the client's empty columns and, where
  needed, appending new columns (e.g. Qty, Unit rate, Material, Labour,
  Line total, Qualifications) to the right of their existing ones.
- Map each derived line item onto the client's corresponding row; append any
  items the client's sheet is missing at the end of the relevant section and
  mark them as additions.
- Return the populated client workbook as the pricing sheet output, keeping
  its original filename with a "Priced" suffix.

**Otherwise**: produce a new structured pricing workbook as the output.
