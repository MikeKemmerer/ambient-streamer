# Releases

Tagged releases publish source plus immutable runtime images so production never builds on the
streaming host. The first image set is deliberately limited to the control plane and audio
engine; composer, MediaMTX and Icecast remain on their existing deployment paths.

## Published artifacts

Each `vX.Y.Z` release contains:

- `ambient-streamer-vX.Y.Z.tar.gz` — source archive with `RELEASE.json`;
- `release-manifest.json` — release commit and image digest references;
- `release.env` — the same digest references in the format consumed by
  `scripts/deploy-release.sh`;
- an amd64/arm64 backend image identified by digest in both manifests;
- an amd64/arm64 Liquidsoap image identified by digest in both manifests.

Builds use unique `run-<run-id>-<attempt>` staging tags; no SemVer or floating `latest` image tag
is published. Production records and pulls only the `@sha256:...` references from `release.env`.
Both images carry BuildKit SBOM/provenance and source/revision labels.

GHCR packages may be private when first created. Make them public in the package settings, or
run `docker login ghcr.io` on the production host with a token that has `read:packages`.

## Creating a release

1. Update `VERSION` to the intended stable base version and merge that change.
2. Tag the exact commit and push the tag:

   ```bash
   git tag v1.2.0
   git push origin v1.2.0
   ```

3. Wait for **Publish Release** to finish both multi-architecture builds.
4. Confirm the GitHub release contains all three files and both GHCR packages show the expected
   digest.

`workflow_dispatch` is available for release candidates. Its version must still match `VERSION`
after removing the prerelease suffix, for example `VERSION=1.2.0` with `v1.2.0-rc.1`.

## Preparing production

The checkout and images are one release unit. The deploy helper refuses to combine a
`release.env` from one commit with source from another.

```bash
git fetch --tags origin
git checkout v1.2.0
gh release download v1.2.0 --pattern release.env

# Pull both digests and write only AMBIENT_BACKEND_IMAGE and
# AMBIENT_LIQUIDSOAP_IMAGE into the existing root .env. No container changes.
scripts/deploy-release.sh release.env
```

The helper parses the manifest rather than sourcing it, validates both GHCR digest references,
checks every requested channel before pulling, verifies each pulled image's source and revision
labels against the exact release commit, and preserves the `.env` file's mode and every secret.

## Rolling out

Recreate the backend independently. `--no-deps` prevents Compose from reconciling MediaMTX or
Icecast:

```bash
scripts/deploy-release.sh release.env --backend
```

Then canary one channel that is already running:

```bash
scripts/deploy-release.sh release.env --liquidsoap vibecoding
```

For each selected channel the helper:

1. refuses to proceed unless its Liquidsoap and composer containers are currently running;
2. asks the updated backend to render the channel Compose file with the image override;
3. recreates only `<channel>-liquidsoap` with `--no-deps`;
4. verifies that `<channel>-composer` has the same container ID afterwards.

Icecast's fallback mount absorbs the Liquidsoap restart. Verify the compositor's `out_time` and
`speed`, the Icecast fallback/live transition, and YouTube Studio before selecting another
channel. A stopped channel is never started by this script; it receives the release image on its
next normal start.

Multiple actions can be requested after a successful canary:

```bash
scripts/deploy-release.sh release.env --backend \
  --liquidsoap lofi --liquidsoap study
```

There is intentionally no composer option.

## Rollback

Retain the prior release's `release.env`. Check out its exact tag, then run its helper with the
same service flags. That pulls and records the prior digests before recreating anything:

```bash
git checkout v1.1.0
scripts/deploy-release.sh release-v1.1.0.env --backend --liquidsoap vibecoding
```

Do not roll back by changing a floating image tag. The digest manifest is the rollback record.