# Money currencies and historical proposal compatibility

The public Money contract supports AUD, CNY, EUR, GBP, HKD, JPY, MYR, SGD and
USD. MYR has two minor-unit decimal places. Its signed and zero values,
normalization, precision validation, arithmetic and canonical serialization use
the existing Money rules. For example, `Money.from_string("12.3", "myr")` has
currency `MYR`, and `canonical_money_str(value.amount, value.currency)` returns
`12.30`. Floats, non-finite values, unsupported codes, sub-minor-unit amounts
and cross-currency arithmetic retain their existing refusal rules. `RM` is not
a public Money currency code.

Currency recognition and admission belong to each parser's existing contract.
The receipt total parser v1/v2, independent AI v1/v2, existing proposal
validation, human completion, proposal hashes and old correction services keep
their original eight-currency admission policy: AUD, CNY, EUR, GBP, HKD, JPY,
SGD and USD. Their shared `parser_proposals.currency_policy_v1` freezes this
set and the original normalization errors. Precision rules for these eight
currencies still come from Money and are unchanged. Existing payloads, DTOs,
hash envelopes, migrations and parser identities gain no policy field.

Consequently, `TOTAL RM 12.30` and `TOTAL MYR 12.30` under either existing
receipt parser version still retain currency evidence `MYR`, an unresolved
amount and `unsupported_currency_for_amount`. Their original field evidence,
serialized results and reconstruction hashes remain stable. The independent
AI source grammar still treats MYR/RM as unsupported currency notation; neither
an interpretation default nor an old manual correction can introduce MYR into
those formats. The existing text parser's MYR/RM aliases and PDF currency
recognition retain their own original behavior.

The native lifecycle proposal path uses the existing independent AI validator
before validating Money. Its existing personal-expense domain guard also keeps
the original eight currencies for issued legacy imports and fact/audit
reconstruction. Import still requires an actual verifier-issued in-process
export receipt; arbitrary JSON carries no such provenance. Money support alone
therefore grants no native proposal, human confirmation, posting or finalization authority. New financial
input formats that admit MYR require their own explicit versioned contract and
guarded entry points. This addition does not introduce such an input format.

Focused synthetic golden tests pin original MYR/RM receipt results, AI source
partitions and proposal hashes. The package acceptance probes execute public
MYR Money behavior and historical-policy checks from both installed wheel and
sdist outside the source checkout.
