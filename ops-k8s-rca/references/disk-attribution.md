# Disk usage attribution

Method for "this node's disk is full, whose is it" without double counting or
false orphan claims.

1. **Cheap first.** `df` with local filesystems only (exclude `tmpfs`,
   `overlay`, `nsfs`) plus a **shallow** `du` of the suspect mount. Go deeper
   only into the large items it shows. Bound every command.
2. **Non-overlapping leaves.** Sum only non-overlapping directories. Dedupe by
   device and mount: bind mounts expose the same bytes under several paths
   (runtime, kubelet, CSI), and a parent plus its child counts twice. Read the
   filesystem root from `mountinfo`.
3. **Reconcile gaps honestly.** When `df` and `du` differ, check read-only for
   deleted-but-open files, reserved blocks, inode exhaustion and unscanned
   directories. An unexplained remainder stays unknown; do not assign it to
   images or any favourite consumer. Record actual bytes, not rounded units.
4. **Attribute upward.** directory -> volume ID and path -> `mountinfo` ->
   current Pod UID, deletion timestamp and phase -> owner chain (workload,
   job, application). Take labels from the Pod's own metadata; do not assume a
   naming scheme.
5. **Label each attribution:** `UID-verified` (volume resolves to a live Pod
   UID), `name-only` (matched by a name, UID unverified), `unattributed`.
   Never promote name-only to verified.
6. **Not running is not deletable.** A directory with no running Pod, several
   cache generations or a matching name is not evidence of a leak. An empty
   "expired" list does not prove there are no leftovers. Deletion is an owner
   decision.
