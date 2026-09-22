# SSH public keys for setup0.sh

Put one OpenSSH **public** key per user here, named `<username>.pub`:

```
keys/lincolnb.pub
keys/tloizou.pub
```

`setup0.sh` refuses to run until every user in its `USERS` list has a
non-empty key file here.

Public keys are safe to commit. Never put private keys in this
repository.
