# Acceptance scripts

Upstream keeps its test suites in a private repository since 1.0.0, so this
fork checks its patches, and the upstream fixes that replaced earlier patches,
with these scripts against a running server. They use the Python standard
library only. Each prints one line per check and exits non-zero if any fails.

| Script | Checks |
|--------|--------|
| `header_search.py` | `SEARCH HEADER Message-ID` matches the whole value, not any one token |
| `oauth_scoped_credentials.py` | an app password cannot authorize an OAuth client |
| `rcpt_fail_limit.py` | failed recipients of a login are limited per login, not per IP |

`rcpt_fail_limit.py` changes the server's abuse ban rate and recipient stage
settings, and every script creates accounts. Use a scratch server only.

## Start a server

Any build works, the published image or one built from `Dockerfile.itsh`. The
data store setting is the only configuration needed, and the recovery
administrator stands in for an admin account:

```sh
mkdir -p etc data && chmod 777 etc data
echo '{"@type":"RocksDb","path":"/var/lib/stalwart/"}' > etc/config.json
docker run -d --name stalwart-acceptance \
  -e STALWART_RECOVERY_ADMIN=admin:admin-secret \
  -v "$PWD/etc:/etc/stalwart" -v "$PWD/data:/var/lib/stalwart" \
  <image>
```

The server creates its default listeners on the first start.

## Run

Run the scripts in the server's network namespace. The management API is then
reached on loopback, which the server always allowlists, and SMTP on the
container's own address, which it does not. `rcpt_fail_limit.py` needs both: it
gets the client address banned and must still reach the API to undo the ban.

```sh
docker run --rm --network container:stalwart-acceptance \
  -v "$PWD/tests/itsh:/t:ro" -e STALWART_URL=http://127.0.0.1:8080 \
  python:3.13-alpine sh -c 'STALWART_HOST=$(hostname -i) python /t/<script>.py'
```

`common.py` lists the environment variables. The defaults match the command
above and the server's default listeners. A script ends with `PASS` when every
check holds.

Remove the container and the `etc` and `data` directories afterwards.
