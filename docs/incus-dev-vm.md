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
  --config=user.user-data="$(cat infra/incus/buzz-dev-cloud-init.yml)" \
  --config limits.cpu=2 \
  --config limits.memory=2GiB \
  --device root,size=20GiB
```

If that alias is unavailable on your Incus installation, use the current Ubuntu 24.04 alias on `images:` with the same `--config=user.user-data=...` value.

Wait for cloud-init to finish:

```sh
incus exec "$VM_NAME" -- cloud-init status --wait
```

## Copy The Repo

```sh
incus file push -r . "$VM_NAME"/home/dev/buzz
incus exec "$VM_NAME" -- chown -R dev:dev /home/dev/buzz
```

Copy or create `buzz.yml` in the VM before starting the stack.

## Run The Stack

```sh
incus exec "$VM_NAME" -- sudo -iu dev bash -lc '
  cd /home/dev/buzz &&
  docker compose up -d --build buzz rclone
'
```

## Verify The Mount

```sh
incus exec "$VM_NAME" -- sudo -iu dev bash -lc '
  cd /home/dev/buzz &&
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

## Modifying An Existing VM

If the VM is already running, you can update its resources. CPU and Memory changes require a VM restart.

### Update CPU and Memory

```sh
incus config set "$VM_NAME" limits.cpu 2
incus config set "$VM_NAME" limits.memory 2GiB
incus restart "$VM_NAME"
```

### Expand Root Volume

1. Increase the size in Incus:

```sh
incus config device override "$VM_NAME" root
incus config device set "$VM_NAME" root size=20GiB
```

2. If the VM is running, you may need to tell the guest OS to grow the partition and filesystem (though Ubuntu's cloud-init often handles this on reboot):

```sh
incus exec "$VM_NAME" -- growpart /dev/sda 1
incus exec "$VM_NAME" -- resize2fs /dev/sda1
```
