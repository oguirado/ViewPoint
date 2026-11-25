# Retention transport

Build the shared adapter with OpenSSL 3 and a C compiler inside the campaign resource scope. The server and client load the same shared library.

```sh
systemd-run --user --scope -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- \
  cc -std=c11 -O2 -Wall -Wextra -Werror -shared -fPIC -Wl,-z,relro,-z,now \
  -o "$ADAPTER_OUTPUT" retention_service/transport/native.c -lssl -lcrypto
```

Construct a `TLSContext` with the approved adapter and public-chain digests, a public PEM CA/CRL bundle, the expected server DNS SAN, and a `Signer` for the approved Unix socket, UID and handle. Protect the adapter and public-chain files from modification. Replace the CRL bundle atomically when rotating trust. The TLS context verifies its local key against the leaf certificate at startup and verifies peer certificates against current trust during each request. The signer protocol uses one request per connection. Each peer closes its write side after its frame so the reader can reject trailing bytes.

Construct `Server` with the effective contract configuration, independent named `Approval`, frozen `ContractValidator`, a callable returning the current immutable `ACL`, and the durable `Backend`. Keep the TLS context open for the server's lifetime. For each request the backend transaction resolves resource ownership and scope in the selected tenant using index metadata. After authorization it verifies and opens bytes under the same transaction. On uploads it calls `Upload.receive` with its private staging writer and publishes only on verified completion. It rechecks access under that transaction before publication. For comparisons it resolves the durable ordered source scopes, authorizes both sources, and verifies their graphs before publication. For recovery it binds cursor and item lookup to the authenticated principal, tenant and current snapshot.

Bind a `Client` to exactly one approved Unix or TCP endpoint and one credential. Use a session for a single tenant on one connection. The client verifies readiness against the expected identity, policy and ACL revisions. For paged readback, pin each page to the expected reference, whole-object size and SHA-256, then reassemble and independently verify the complete byte count and digest.

Conformance uses generated in-memory signer keys and temporary loopback and Unix endpoints. Set `RETENTION_FROZEN_DIR` to the frozen receipt directory and run `retention_service.transport.tests.test_tls` and `retention_service.transport.tests.test_transport` with `python3 -B -m unittest` in the campaign resource scope. The route fixture exercises the transport boundary. Durable-store and live REA qualification have their own gates.
