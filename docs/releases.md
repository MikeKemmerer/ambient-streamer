# Releases

Tagged releases publish source plus immutable runtime images so production never builds on the
streaming host. Backend, Liquidsoap, and the composer/visualizer runtime are published; MediaMTX
and Icecast remain on their existing deployment paths.

## Published artifacts

Each `vX.Y.Z` release contains:

- `ambient-streamer-vX.Y.Z.tar.gz` — source archive with `RELEASE.json`;
- `release-manifest.json` — release commit and image digest references;
- `release.env` — the same digest references in the format consumed by
  `scripts/deploy-release.sh`;
- an amd64/arm64 backend image identified by digest in both manifests;
- an amd64/arm64 Liquidsoap image identified by digest in both manifests;
- an amd64/arm64 composer image used by both stable compositor and restartable visualizer.

Builds use unique `run-<run-id>-<attempt>` staging tags; no SemVer or floating `latest` image tag
is published. Production records and pulls only the `@sha256:...` references from `release.env`.
All images carry BuildKit SBOM/provenance and source/revision labels.
Public repositories also publish GitHub build-provenance attestations for every image digest.

GHCR packages may be private when first created. Make them public in the package settings, or
run `docker login ghcr.io` on the production host with a token that has `read:packages`.
Production also needs an authenticated GitHub CLI version that provides `gh attestation verify`.
Install it from <https://github.com/cli/cli/blob/trunk/docs/install_linux.md>, then run
`gh auth login` before the first deployment.

## Creating a release

1. Update `VERSION` to the intended stable base version and merge that change.
2. Tag the exact commit and push the tag:

   ```bash
   git tag v1.2.0
   git push origin v1.2.0
   ```

3. Wait for **Publish Release** to finish all multi-architecture builds and attestations.
4. Confirm the GitHub release contains all three files and all GHCR packages show the expected
   digest.

`workflow_dispatch` is available for release candidates. Its version must still match `VERSION`
after removing the prerelease suffix, for example `VERSION=1.2.0` with `v1.2.0-rc.1`.

## Upgrading an archive installation

An installation copied to a host without `.git` stays in place so its absolute bind-mount paths
do not change. Download the source archive and manifest into a temporary directory, extract only
the upgrader, then overlay the validated release source:

```bash
install -d -m 700 /tmp/ambient-v0.1.3
cd /tmp/ambient-v0.1.3
wget https://github.com/MikeKemmerer/ambient-streamer/releases/download/v0.1.3/ambient-streamer-v0.1.3.tar.gz
wget https://github.com/MikeKemmerer/ambient-streamer/releases/download/v0.1.3/release.env
tar -xzf ambient-streamer-v0.1.3.tar.gz \
  ambient-streamer-v0.1.3/scripts/upgrade-release.py
INSTALL_ROOT=/path/to/ambient-streamer
python3 ambient-streamer-v0.1.3/scripts/upgrade-release.py \
  ambient-streamer-v0.1.3.tar.gz release.env "$INSTALL_ROOT"
```

The upgrader rejects unsafe archive entries and mismatched release metadata before writing. It
atomically replaces release-owned files, installs `release.env` with mode `600`, preserves
untracked `.env`, `ambient.yaml`, channel media, and logs, and saves replaced source files under
`.release-backups/`. It intentionally leaves obsolete files in place.

## Preparing production

The source and images are one release unit. The deploy helper refuses to combine `release.env`
with a different Git checkout or extracted `RELEASE.json`. Archive installations first complete
the overlay procedure above; Git installations use the matching tag:

```bash
git fetch --tags origin
git checkout v1.2.0
gh release download v1.2.0 --pattern release.env

# Pull all digests and record AMBIENT_BACKEND_IMAGE, AMBIENT_LIQUIDSOAP_IMAGE,
# and AMBIENT_COMPOSER_IMAGE in the existing root .env. No container changes.
scripts/deploy-release.sh release.env
```

The helper parses the manifest rather than sourcing it, validates all GHCR digest references,
checks every requested channel before pulling, verifies each pulled image's source and revision
labels and GitHub attestation against the exact release commit, and preserves the `.env` file's
mode and every secret.

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

1. refuses to proceed unless its Liquidsoap and exactly one composer slot are currently running;
2. asks the updated backend to render the channel Compose file with the image override;
3. recreates only `<channel>-liquidsoap` with `--no-deps`;
4. verifies that the active `<channel>-composer` or `<channel>-composer-next` has the same
  container ID afterwards.

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

Releases that change the stable compositor architecture require a separately reviewed,
make-before-break migration after image preparation. The release helper never performs that
high-impact step implicitly. Once the new composer is stable, visualization changes recreate only
the capped visualizer child and leave the compositor and YouTube ingest session untouched.

## Rollback

Retain the prior release's `release.env`. Check out its exact tag, then run its helper with the
same service flags. That pulls and records the prior digests before recreating anything:

```bash
git checkout v1.1.0
scripts/deploy-release.sh release-v1.1.0.env --backend --liquidsoap vibecoding
```

Do not roll back by changing a floating image tag. The digest manifest is the rollback record.