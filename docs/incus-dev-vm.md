# Incus Dev VM

This repo includes a reusable cloud-init definition for a disposable Ubuntu 24.04 VM that can run and debug the Buzz stack.

## Create The VM

Pick a VM name:

```sh
VM_NAME=buzz-dev-ubuntu2404
```

Launch an Ubuntu 24.04 VM from the `images:` remote and apply the repo cloud-init:

```sh
incus launch images:ubuntu/24.04/cloud "$VM_NAME" --vm \
  --config=user.user-data="$(cat infra/incus/buzz-dev-cloud-init.yml)"
```

If that alias is unavailable on your Incus installation, use the current Ubuntu 24.04 alias on `images:` with the same `--config=user.user-data=...` value.

Wait for cloud-init to finish:

```sh
incus exec "$VM_NAME" -- cloud-init status --wait
```

## Copy The Repo

```sh
incus file push -r . "$VM_NAME"/home/dev/plex-zurg
incus exec "$VM_NAME" -- chown -R dev:dev /home/dev/plex-zurg
```

Copy or create `buzz.yml` in the VM before starting the stack.

## Run The Stack

```sh
incus exec "$VM_NAME" -- sudo -iu dev bash -lc '
  cd /home/dev/plex-zurg &&
  docker compose up -d --build buzz rclone
'
```

## Verify The Mount

```sh
incus exec "$VM_NAME" -- sudo -iu dev bash -lc '
  cd /home/dev/plex-zurg &&
  docker compose ps &&
  curl -fsS http://127.0.0.1:9999/readyz &&
  ls /mnt/buzz &&
  ls /mnt/buzz/movies | grep Little
'
```

## Remove The VM

```sh
incus delete -f "$VM_NAME"
```
