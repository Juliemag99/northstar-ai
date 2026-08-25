# NorthStar AI — Carmeco Import Mapping

## Files
- `carmeco_companies_import.csv`: 402 company/prospect records
- `carmeco_contacts_import.csv`: 2,000 contact records
- Company/contact link key: `Record No.`

## Validation
- Unique company Record Nos.: 402
- Unique contact Record Nos.: 402
- Contact Record Nos. matched to companies: 402
- Unmatched contact rows: 0

## Company field mapping
- `Record No.` -> legacy_record_no / external_record_no
- `Company` -> company_name
- `Address`, `City`, `State`, `Zip` -> company address
- `Web Address` -> website
- `Phone`, `Alt Phone`, `Mobile`, `Email` -> legacy primary contact fields (preserve during first import)
- `Location Sales Volume Range` -> sales_volume_range
- `Location Employee Size Range` -> employee_size_range
- SIC / NAICS fields -> industry classification
- `Carmeco` -> client-specific Carmeco status
- `Sales Rep Comments/Notes` -> legacy Carmeco activity/notes history
- `Entered`, `Last Updated` -> source audit dates
- `Customer Campaign` -> legacy campaign/source tags

## Contact field mapping
- `Record No.` -> foreign key back to company
- `FirstName`, `LastName` -> contact name
- `Title` -> job title
- `Phone`, `Alt Phone`, `Email` -> contact communication fields

## Import rules
1. Create ONE company/prospect record per nonblank `Record No.`.
2. Do not deduplicate contacts just because names or phone numbers repeat; preserve source contacts first.
3. Attach every contact to its company using `Record No.`.
4. Preserve Carmeco status exactly as supplied.
5. Preserve the full legacy notes field; do not summarize or overwrite it during import.
6. Do not invent data for blank fields.
7. Use the Carmeco client relationship for client-specific status and notes.
