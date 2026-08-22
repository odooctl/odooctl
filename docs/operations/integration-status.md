# Integration test status

Latest run of the real-Odoo integration matrix (`tests/integration/`).

| Date | odooctl package / commit | Odoo versions | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-08-08 | `0.3.0b1` / `ea14df128ad4377f52ded427156e0b7383408f5e` | 17.0, 18.0, 19.0 | 21 passed (7/version) | The then-current lifecycle passed, but the gate did not render the staging login form. The tagged sanitizer can clear `database.secret`; rotation and the login-form assertion were added after this release. |
| 2026-07-19 | `0.2.0` / `1b482535f060054d98efc258ce4cc61384a465e4` | 17.0, 18.0, 19.0 | 21 passed (7/version) | Full lifecycle green: validate, doctor, status, backup verify, SQL-asserted sanitization, restore, API-enqueue → runner parity, and foreign-container isolation. |

Reproduce:

```bash
ODOOCTL_IT_VERSIONS=17.0,18.0,19.0 pytest -m integration tests/integration
```

See `docs/operations/integration-testing.md` for what the harness covers and
its isolation guarantees.
