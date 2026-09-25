# SSH public keys for setup0.sh

Put one OpenSSH **public** key per user here, named `<username>.pub`:

```
keys/lincolnb.pub
keys/tloizou.pub
```

`setup0.sh` refuses to run until every user in its `USERS` list has a
non-empty key file here.

This directory is only for the admin accounts that `setup0.sh` creates.
Initial keys for the base login (`vm.ssh_user`, e.g. `rocky`) go in
`config/base_authorized_keys` instead, which the generator injects via
instance metadata.

Public keys are safe to commit. Never put private keys in this
repository.
