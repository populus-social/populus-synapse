# populus-synapse

A [Synapse](https://github.com/element-hq/synapse) module used by
[populus.social](https://populus.social) for its Chat service. Synapse itself
is not modified: we run the stock upstream image, and this module loads into it
through Synapse's module API.

The module runs inside Synapse's process and imports `synapse.module_api`, so
it is published here under the GNU Affero General Public License v3.0 (see
`LICENSE`).

## What it does

- **Storage allowance.** Each member has a storage allowance shared across
  populus.social's services. Before every media or avatar upload, the module
  asks the platform's account service how much the member has left. It refuses
  the upload if the member is already over the allowance or the file wouldn't
  fit. Text messages are never affected.
- **Read-only Chat at the end of a membership.** When a membership is in its
  closing period ("Sunset"), the member can still read rooms and export their
  keys, but can't send messages, create or join rooms, or invite anyone.
  Leaving a room still works. This part is optional and only switched on when
  `sunset_state_url` is set.

Both checks **fail open**: if the account service is unreachable or slow, the
upload or action is allowed. A limit should never take a member's chat down.
Answers are cached briefly per member, and only local users are looked up.

## Configuration

In `homeserver.yaml`, with this directory on Synapse's `PYTHONPATH`:

```yaml
modules:
  - module: populus_synapse.StorageAllowanceModule
    config:
      account_service_url: https://account-service.example/internal/storage-allowance  # required
      api_key_file: /secrets/storage-allowance/apiKey                                   # required
      cache_ttl_seconds: 5      # optional, default 5
      timeout_seconds: 2        # optional, default 2
      sunset_state_url: ""      # optional; empty leaves read-only mode off
```

The module sends the key from `api_key_file` as a bearer token. It expects:

- `GET <account_service_url>?service=chat&pseudonym=<localpart>` →
  `{"over": false, "remaining_bytes": 123456}` (`remaining_bytes: null` means
  no limit)
- `GET <sunset_state_url>?pseudonym=<localpart>` → `{"restricted": false}`

## About this repository

This repository is published from the populus.social platform's own source
tree, and its `main` matches the module that runs on populus.social. It exists
so that anyone can read and reuse the code we run, as the AGPL requires. We
don't take pull requests here.
