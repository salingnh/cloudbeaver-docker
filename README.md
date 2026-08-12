# CloudBeaver Full JDBC Docker

Custom CloudBeaver Community image builder that keeps the standard CloudBeaver CE drivers and automatically pre-bundles additional JDBC drivers already defined by DBeaver Community when their runtime JARs can be resolved from Maven.

## Why this repository exists

CloudBeaver Community does **not** dynamically download JDBC drivers like DBeaver Desktop. A driver must:

1. already have a driver definition in the DBeaver platform;
2. have its runtime JARs available in CloudBeaver `driversLocation`;
3. be registered in CloudBeaver as a resource, bundle, and enabled driver.

This repository automates steps 2 and 3 for Maven-backed JDBC drivers and then builds CloudBeaver from source into a Docker image.

## What "full" means

The generated image contains:

- every driver already pre-bundled by CloudBeaver Community;
- additional DBeaver Community drivers whose connection URL is JDBC, whose driver definition exposes a CloudBeaver-compatible `drivers/...` bundle path, and whose required JARs are available from Maven;
- a build report at `/opt/cloudbeaver/full-drivers-report.json` showing what was added and what was skipped.

It intentionally does **not** pretend that every DBeaver driver can be redistributed. Drivers that require vendor-local/proprietary files, have no Maven artifact, or are not JDBC are skipped. Review vendor licenses before publishing the resulting image to a public registry.

## Build

The Dockerfile is self-contained: it installs the documented CloudBeaver build toolchain in a builder stage, clones CloudBeaver/DBeaver sources, generates the driver pack, builds CloudBeaver, and packages the final runtime image.

```bash
docker build \
  --build-arg CLOUDBEAVER_REF=devel \
  --build-arg DBEAVER_REF=devel \
  -t cloudbeaver-full:local \
  .
```

For production, pin `CLOUDBEAVER_REF`, `DBEAVER_REF`, and `DBEAVER_COMMON_REF` to compatible commits/tags instead of tracking `devel`.

The build is large because it compiles CloudBeaver and DBeaver. BuildKit cache is strongly recommended.

## Run

```bash
docker compose up -d
```

Open:

```text
http://localhost:8978
```

The Compose file enables anonymous access and private/custom connections for the discovery-service POC. Review these settings before exposing CloudBeaver outside a trusted network.

## Inspect installed drivers

```bash
docker exec cloudbeaver-full \
  find /opt/cloudbeaver/drivers -maxdepth 2 -type f | sort
```

View the generated report:

```bash
docker exec cloudbeaver-full \
  cat /opt/cloudbeaver/full-drivers-report.json
```

## Driver generation policy

Edit [`config/driver-policy.json`](config/driver-policy.json).

The default policy:

- scans DBeaver `plugins/**/plugin.xml`;
- selects network JDBC drivers (`jdbc:` URL);
- skips embedded/file drivers unless they are already part of stock CloudBeaver;
- requires a `drivers/<name>` prebundle mapping in the DBeaver definition;
- requires Maven-backed runtime JARs;
- skips drivers that require unresolved `${...}` Maven versions;
- keeps stock CloudBeaver registrations untouched;
- aggregates dependencies by CloudBeaver driver bundle to avoid duplicate modules.

You can explicitly exclude a driver with its full DBeaver ID, e.g. `provider:driver-id`.

## Project layout

```text
.
├── Dockerfile
├── docker-compose.yml
├── config/
│   └── driver-policy.json
├── scripts/
│   └── generate_full_driver_pack.py
├── .github/workflows/
│   └── build.yml
└── Makefile
```

## Useful commands

```bash
make build
make up
make logs
make report
make down
```

## Important limitations

- This is a **JDBC-focused** full pack. Non-JDBC/NoSQL drivers are not automatically added.
- A Maven artifact being downloadable does not automatically mean you are allowed to redistribute it publicly. Check the database vendor's license.
- A driver may be present but still require database-side configuration, TLS certificates, native libraries, or vendor authentication components.
- CloudBeaver and DBeaver branches must be compatible. Pin compatible commits for repeatable production builds.
- The goal here is broad connection/discovery support. Database-specific UI features may differ from DBeaver Desktop for drivers that run through generic JDBC metadata.

## Security

The supplied Compose configuration is optimized for an internal POC. Do not expose anonymous private connections directly to the Internet. Put CloudBeaver behind an authenticated reverse proxy/gateway and restrict network egress to approved database networks for production.
