# Password compatibility

kaito supports password-protected archives through the bundled 7-Zip backend, but password character support depends on the archive encryption scheme.

- **AES-256 ZIP / 7z:** Unicode passwords are supported by the modern encryption path.
- **Legacy ZipCrypto ZIP:** use **ASCII-only passwords** for interoperable creation and extraction.
- **non-ASCII ZipCrypto passwords:** not supported as a guaranteed compatibility contract. The legacy scheme does not define a portable password encoding, and bundled 7-Zip 26.02 on the Windows acceptance runner rejects a Japanese ZipCrypto creation password with `System ERROR: The parameter is incorrect.`

This limitation is specific to legacy ZipCrypto. It must not be generalized to AES-256 or 7z encryption.

## 7-Zip CLI password transport

kaito currently pins the bundled 7-Zip CLI to **26.02** and supplies an archive password with 7-Zip's documented `-p{password}` command-line switch when a CLI-backed encrypted archive operation requires it.

The application redacts that argument from returned command metadata, captured stdout/stderr, logs, and user-facing errors. It also does not persist the password in kaito settings or repository files. However, while the child `7z.exe` process is running, an operating-system tool with permission to inspect process command lines may be able to observe the raw `-p<password>` argument.

As of the bundled 26.02 command-line interface, 7-Zip does not document a separate file-descriptor/stdin switch for supplying only the password. `-si` is for archive input data and cannot be repurposed as a password channel. The upstream 7-Zip project also has an open request for a separate password file-descriptor mechanism because `-pPassword` is visible through process command-line inspection. Until upstream exposes and documents a safer non-command-line password transport that works for kaito's unattended create/test operations, kaito must treat this as an upstream CLI limitation rather than inventing an unsupported workaround.

Do not work around this limitation by writing the password to a temporary file, environment variable, settings store, log, or crash-report field. If a future bundled 7-Zip release adds an officially supported safer password channel, migrate the backend together with encrypted ZIP/7z round-trip, cancellation, timeout, and redaction regression coverage.

The CI suite contains a strict ASCII ZipCrypto round-trip regression. If support for non-ASCII ZipCrypto becomes deterministic across the bundled backend and supported Windows environments, this compatibility boundary can be widened together with a strict regression fixture.
