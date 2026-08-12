#!/usr/bin/env python3
"""Generate a broad CloudBeaver CE JDBC driver pack from DBeaver definitions.

The script does not invent database definitions. It scans DBeaver Community
plugin.xml files, finds JDBC drivers that already have a pre-bundle mapping and
Maven-backed runtime JARs, then:

* generates CloudBeaver server/drivers Maven modules for missing bundles;
* registers driver resources/bundles in CloudBeaver;
* enables the full DBeaver driver ID in CloudBeaver;
* writes a JSON report with added/skipped drivers.

This follows CloudBeaver CE's source-build driver model and deliberately skips
vendor-local drivers that cannot be resolved from Maven.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


MAVEN_PREFIX = "maven:/"
DRIVERS_PREFIX = "drivers/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloudbeaver", required=True, type=Path)
    parser.add_argument("--dbeaver", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def load_policy(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def xml_parser() -> ET.XMLParser:
    # Keep comments so the upstream files remain easy to diff/debug.
    return ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))


def parse_maven_coordinate(path: str, allow_floating: bool) -> tuple[str, str, str] | None:
    if not path.startswith(MAVEN_PREFIX):
        return None

    raw = path[len(MAVEN_PREFIX):]
    parts = raw.split(":")
    if len(parts) != 3:
        return None

    group_id, artifact_id, raw_version = parts
    if not group_id or not artifact_id or not raw_version or "${" in raw_version:
        return None

    version = raw_version.strip()

    # DBeaver commonly uses RELEASE[minimum-known-version]. For a reproducible
    # CloudBeaver build, use the bracketed version rather than floating RELEASE.
    match = re.fullmatch(r"(?:RELEASE|LATEST)\[([^\]]+)\]", version)
    if match:
        version = match.group(1)
    elif version in {"RELEASE", "LATEST"}:
        if not allow_floating:
            return None
        version = "LATEST"
    elif version.startswith("[") or version.startswith("("):
        # Best-effort range handling: choose the lower bound when present.
        lower = version[1:].split(",", 1)[0].strip()
        if not lower:
            return None
        version = lower

    return group_id, artifact_id, version


def split_categories(value: str | None) -> set[str]:
    if not value:
        return set()
    return {x.strip().lower() for x in re.split(r"[,;]", value) if x.strip()}


def discover_drivers(dbeaver_root: Path, policy: dict) -> tuple[list[dict], list[dict]]:
    selected: list[dict] = []
    skipped: list[dict] = []

    exclude_ids = set(policy.get("exclude_full_driver_ids", []))
    exclude_providers = set(policy.get("exclude_providers", []))
    exclude_categories = {x.lower() for x in policy.get("exclude_categories", [])}
    include_embedded = bool(policy.get("include_embedded", False))
    require_jdbc_url = bool(policy.get("require_jdbc_url", True))
    require_prebundle = bool(policy.get("require_prebundle_mapping", True))
    require_maven = bool(policy.get("require_maven_runtime", True))
    allow_floating = bool(policy.get("allow_floating_versions", True))

    for plugin_path in sorted(dbeaver_root.glob("plugins/**/plugin.xml")):
        try:
            tree = ET.parse(plugin_path, parser=xml_parser())
        except ET.ParseError as exc:
            skipped.append({"source": str(plugin_path), "reason": f"xml-parse-error: {exc}"})
            continue

        root = tree.getroot()
        for extension in root.findall(".//extension"):
            if extension.get("point") != "org.jkiss.dbeaver.dataSourceProvider":
                continue

            for datasource in extension.findall("./datasource"):
                provider_id = datasource.get("id")
                if not provider_id:
                    continue

                for drivers_node in datasource.findall("./drivers"):
                    for driver in drivers_node.findall("./driver"):
                        driver_id = driver.get("id")
                        if not driver_id:
                            continue

                        full_id = f"{provider_id}:{driver_id}"
                        base_info = {
                            "full_driver_id": full_id,
                            "provider": provider_id,
                            "driver_id": driver_id,
                            "label": driver.get("label") or full_id,
                            "source": str(plugin_path.relative_to(dbeaver_root)),
                        }

                        def reject(reason: str) -> None:
                            skipped.append({**base_info, "reason": reason})

                        if full_id in exclude_ids:
                            reject("policy-excluded-driver")
                            continue
                        if provider_id in exclude_providers:
                            reject("policy-excluded-provider")
                            continue

                        sample_url = (driver.get("sampleURL") or "").strip()
                        if require_jdbc_url and not sample_url.lower().startswith("jdbc:"):
                            reject("not-jdbc-url")
                            continue

                        embedded = (driver.get("embedded") or "false").lower() == "true"
                        if embedded and not include_embedded:
                            reject("embedded-driver")
                            continue

                        categories = split_categories(driver.get("categories"))
                        if categories & exclude_categories:
                            reject("excluded-category")
                            continue

                        jar_files = [f for f in driver.findall("./file") if f.get("type") == "jar"]

                        prebundle_file = next(
                            (
                                f
                                for f in jar_files
                                if (f.get("path") or "").startswith(DRIVERS_PREFIX)
                                and (f.get("bundle") or "").startswith("drivers.")
                            ),
                            None,
                        )

                        if require_prebundle and prebundle_file is None:
                            reject("no-prebundle-mapping")
                            continue

                        if prebundle_file is not None:
                            runtime_dir = (prebundle_file.get("path") or "")[len(DRIVERS_PREFIX):]
                            bundle_id = prebundle_file.get("bundle") or f"drivers.{runtime_dir.replace('/', '.')}"
                        else:
                            runtime_dir = re.sub(r"[^a-zA-Z0-9_.-]+", "-", full_id).lower()
                            bundle_id = f"drivers.{runtime_dir.replace('/', '.')}"

                        dependencies: list[tuple[str, str, str]] = []
                        bad_maven_paths: list[str] = []
                        for file_node in jar_files:
                            file_path = file_node.get("path") or ""
                            if not file_path.startswith(MAVEN_PREFIX):
                                continue

                            file_bundle = file_node.get("bundle") or ""
                            if file_bundle and file_bundle not in {bundle_id, f"!{bundle_id}"}:
                                continue

                            coordinate = parse_maven_coordinate(file_path, allow_floating)
                            if coordinate is None:
                                bad_maven_paths.append(file_path)
                            else:
                                dependencies.append(coordinate)

                        # Stable dedupe while keeping definition order.
                        dependencies = list(dict.fromkeys(dependencies))

                        if require_maven and not dependencies:
                            reject("no-resolvable-maven-runtime")
                            continue
                        if bad_maven_paths:
                            reject("unresolved-maven-coordinate")
                            continue

                        selected.append(
                            {
                                **base_info,
                                "sample_url": sample_url,
                                "runtime_dir": runtime_dir,
                                "bundle_id": bundle_id,
                                "dependencies": [
                                    {"group_id": g, "artifact_id": a, "version": v}
                                    for g, a, v in dependencies
                                ],
                            }
                        )

    return selected, skipped


def read_cloudbeaver_registration(plugin_xml: Path) -> tuple[ET.ElementTree, ET.Element, ET.Element, ET.Element]:
    tree = ET.parse(plugin_xml, parser=xml_parser())
    root = tree.getroot()

    resources_ext = None
    bundles_ext = None
    drivers_ext = None

    for extension in root.findall("./extension"):
        point = extension.get("point")
        if point == "org.jkiss.dbeaver.resources":
            resources_ext = extension
        elif point == "org.jkiss.dbeaver.product.bundles":
            bundles_ext = extension
        elif point == "io.cloudbeaver.driver":
            drivers_ext = extension

    if resources_ext is None or bundles_ext is None or drivers_ext is None:
        raise RuntimeError(f"Unexpected CloudBeaver driver plugin structure: {plugin_xml}")

    return tree, resources_ext, bundles_ext, drivers_ext


def module_pom(module_artifact: str, runtime_dir: str, deps: list[dict]) -> str:
    dependencies = "\n".join(
        f"""        <dependency>\n            <groupId>{escape(dep['group_id'])}</groupId>\n            <artifactId>{escape(dep['artifact_id'])}</artifactId>\n            <version>{escape(dep['version'])}</version>\n        </dependency>"""
        for dep in deps
    )

    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<project xmlns=\"http://maven.apache.org/POM/4.0.0\"
         xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
         xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd\">
    <modelVersion>4.0.0</modelVersion>
    <artifactId>{escape(module_artifact)}</artifactId>
    <version>1.0.0</version>
    <parent>
        <groupId>io.cloudbeaver</groupId>
        <artifactId>drivers</artifactId>
        <version>1.0.0</version>
        <relativePath>../</relativePath>
    </parent>
    <properties>
        <deps.output.dir>{escape(runtime_dir)}</deps.output.dir>
    </properties>
    <dependencies>
{dependencies}
    </dependencies>
</project>
"""


def patch_cloudbeaver(cloudbeaver_root: Path, selected: list[dict]) -> list[dict]:
    drivers_pom_path = cloudbeaver_root / "server" / "drivers" / "pom.xml"
    registration_path = (
        cloudbeaver_root
        / "server"
        / "bundles"
        / "io.cloudbeaver.resources.drivers.base"
        / "plugin.xml"
    )

    pom_tree = ET.parse(drivers_pom_path, parser=xml_parser())
    pom_root = pom_tree.getroot()
    ns_match = re.match(r"\{(.+)\}", pom_root.tag)
    ns = ns_match.group(1) if ns_match else ""
    prefix = f"{{{ns}}}" if ns else ""
    modules_node = pom_root.find(f"{prefix}modules")
    if modules_node is None:
        raise RuntimeError(f"No <modules> in {drivers_pom_path}")

    existing_modules = {m.text.strip() for m in modules_node.findall(f"{prefix}module") if m.text}

    reg_tree, resources_ext, bundles_ext, drivers_ext = read_cloudbeaver_registration(registration_path)
    existing_resources = {x.get("name") for x in resources_ext.findall("./resource")}
    existing_bundles = {x.get("id") for x in bundles_ext.findall("./bundle")}
    existing_driver_ids = {x.get("id") for x in drivers_ext.findall("./driver")}

    bundles: dict[str, dict] = {}
    for driver in selected:
        item = bundles.setdefault(
            driver["bundle_id"],
            {
                "bundle_id": driver["bundle_id"],
                "runtime_dir": driver["runtime_dir"],
                "dependencies": {},
                "drivers": [],
            },
        )
        item["drivers"].append(driver)
        for dep in driver["dependencies"]:
            key = (dep["group_id"], dep["artifact_id"])
            item["dependencies"].setdefault(key, dep)

    added: list[dict] = []
    drivers_dir = cloudbeaver_root / "server" / "drivers"

    for bundle_id, bundle in sorted(bundles.items()):
        runtime_dir = bundle["runtime_dir"]
        resource_name = f"drivers/{runtime_dir}"
        module_name = "autopack-" + re.sub(r"[^a-zA-Z0-9_.-]+", "-", runtime_dir).replace("/", "-")
        module_dir = drivers_dir / module_name

        bundle_already_packaged = bundle_id in existing_bundles or resource_name in existing_resources

        if not bundle_already_packaged:
            module_dir.mkdir(parents=True, exist_ok=True)
            deps = sorted(bundle["dependencies"].values(), key=lambda d: (d["group_id"], d["artifact_id"]))
            (module_dir / "pom.xml").write_text(
                module_pom(f"drivers.{module_name}", runtime_dir, deps),
                encoding="utf-8",
            )

            if module_name not in existing_modules:
                node = ET.SubElement(modules_node, f"{prefix}module")
                node.text = module_name
                existing_modules.add(module_name)

            if resource_name not in existing_resources:
                ET.SubElement(resources_ext, "resource", {"name": resource_name})
                existing_resources.add(resource_name)

            if bundle_id not in existing_bundles:
                ET.SubElement(bundles_ext, "bundle", {"id": bundle_id, "label": f"Auto-packaged {runtime_dir} JDBC drivers"})
                existing_bundles.add(bundle_id)

        for driver in bundle["drivers"]:
            full_id = driver["full_driver_id"]
            if full_id not in existing_driver_ids:
                ET.SubElement(drivers_ext, "driver", {"id": full_id})
                existing_driver_ids.add(full_id)
                added.append(
                    {
                        **driver,
                        "module": None if bundle_already_packaged else module_name,
                        "reused_existing_bundle": bundle_already_packaged,
                    }
                )

    ET.register_namespace("", ns) if ns else None
    pom_tree.write(drivers_pom_path, encoding="utf-8", xml_declaration=True)
    reg_tree.write(registration_path, encoding="utf-8", xml_declaration=True)

    return added


def main() -> int:
    args = parse_args()
    policy = load_policy(args.policy)

    if not args.cloudbeaver.exists():
        raise SystemExit(f"CloudBeaver source not found: {args.cloudbeaver}")
    if not args.dbeaver.exists():
        raise SystemExit(f"DBeaver source not found: {args.dbeaver}")

    selected, skipped = discover_drivers(args.dbeaver, policy)
    added = patch_cloudbeaver(args.cloudbeaver, selected)

    reasons = Counter(item.get("reason", "unknown") for item in skipped)
    report = {
        "policy": policy,
        "selected_candidates": len(selected),
        "newly_enabled_drivers": len(added),
        "added": added,
        "skipped_reason_counts": dict(sorted(reasons.items())),
        "skipped": skipped,
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Selected candidates : {len(selected)}")
    print(f"Newly enabled       : {len(added)}")
    print("Skipped reasons:")
    for reason, count in sorted(reasons.items()):
        print(f"  {reason:32} {count}")
    print(f"Report               : {args.report}")

    if not selected:
        print("ERROR: no JDBC driver candidates were selected; upstream structure may have changed", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
