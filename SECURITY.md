# Security policy

Do not open a public issue containing a credential, real financial record,
receipt, statement, attachment, database, backup, or private runtime log.

If a secret is accidentally committed, revoke it immediately and report the
affected commit privately to the repository owner. Git history deletion is not
a substitute for credential rotation.

Pull requests from forks run with read-only permissions and receive no secrets.
