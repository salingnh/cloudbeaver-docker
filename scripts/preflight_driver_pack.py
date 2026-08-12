#!/usr/bin/env python3
"""Validate generated CloudBeaver autopack driver modules before the full build.

A DBeaver driver definition can point at a Maven coordinate that no longer
exists, contains a bad upstream version, or has an unresolvable transitive
dependency. Failing only during CloudBeaver's full reactor build wastes several
minutes and breaks the whole image.

This script validates every generated ``autopack-*`` module with Maven. Failed
modules are removed from the CloudBeaver driver reactor and from the CloudBeaver
resource/bundle/driver registrations, then recorded in the build report.
Stock CloudBeaver drivers are never modified.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloudbeaver", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--maven", default="mvn")
    parser.add_argument("--timeout", type=int, default=240)
    return parser.parse_args()


def xml_parser() -> ET.XMLParser:
    return ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))


def validate_module(maven: str, pom: Path, timeout: int) -> tuple[bool, str]:
    command = [
        maven,
        "-q",
        "-B",
        "-f",
        str(pom),
        "-DskipTests",
        "-Dmaven.wagon.http.retryHandler.count=3",
        "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:resolve",
        "-DincludeScope=runtime",
        "-DexcludeTransitive=false",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return False, f"Maven preflight timed out after {timeout}s\n{output[-4000:]}"

    return result.returncode == 0, (result.stdout or "")[-4000:]


def remove_failed_modules(cloudbeaver: Path, report: dict, failed: dict[str, str]) -> None:
    drivers_pom = cloudbeaver / "server" / "drivers" / "pom.xml"
    registration = (
        cloudbeaver
        / "server"
        / "bundles"
        / "io.cloudbeaver.resources.drivers.base"
        / "plugin.xml"
    )

    pom_tree = ET.parse(drivers_pom, parser=xml_parser())
    pom_root = pom_tree.getroot()
    ns_match = re.match(r"\{(.+)\}", pom_root.tag)
    ns = ns_match.group(1) if ns_match else ""
    prefix = f"{{{ns}}}" if ns else ""
    modules_node = pom_root.find(f"{prefix}modules")
    if modules_node is None:
        raise RuntimeError(f"No <modules> element in {drivers_pom}")

    for module_node in list(modules_node.findall(f"{prefix}module")):
        module_name = (module_node.text or "").strip()
        if module_name in failed:
            modules_node.remove(module_node)

    reg_tree = ET.parse(registration, parser=xml_parser())
    reg_root = reg_tree.getroot()
    resources_ext = bundles_ext = drivers_ext = None
    for extension in reg_root.findall("./extension"):
        point = extension.get("point")
        if point == "org.jkiss.dbeaver.resources":
            resources_ext = extension
        elif point == "org.jkiss.dbeaver.product.bundles":
            bundles_ext = extension
        elif point == "io.cloudbeaver.driver":
            drivers_ext = extension

    if resources_ext is None or bundles_ext is None or drivers_ext is None:
        raise RuntimeError(f"Unexpected CloudBeaver registration structure: {registration}")

    failed_items = [item for item in report.get("added", []) if item.get("module") in failed]
    failed_driver_ids = {item.get("full_driver_id") for item in failed_items}
    failed_bundle_ids = {item.get("bundle_id") for item in failed_items}
    failed_resource_names = {
        f"drivers/{item.get('runtime_dir')}"
        for item in failed_items
        if item.get("runtime_dir")
    }

    for node in list(resources_ext.findall("./resource")):
        if node.get("name") in failed_resource_names:
            resources_ext.remove(node)

    for node in list(bundles_ext.findall("./bundle")):
        if node.get("id") in failed_bundle_ids:
            bundles_ext.remove(node)

    for node in list(drivers_ext.findall("./driver")):
        if node.get("id") in failed_driver_ids:
            drivers_ext.remove(node)

    for module_name in failed:
        module_dir = cloudbeaver / "server" / "drivers" / module_name
        if module_dir.is_dir():
            shutil.rmtree(module_dir)

    if ns:
        ET.register_namespace("", ns)
    pom_tree.write(drivers_pom, encoding="utf-8", xml_declaration=True)
    reg_tree.write(registration, encoding="utf-8", xml_declaration=True)

    kept = [item for item in report.get("added", []) if item.get("module") not in failed]
    report["added"] = kept
    report["newly_enabled_drivers"] = len(kept)

    skipped = report.setdefault("skipped", [])
    for item in failed_items:
        skipped.append(
            {
                "full_driver_id": item.get("full_driver_id"),
                "provider": item.get("provider"),
                "driver_id": item.get("driver_id"),
                "label": item.get("label"),
                "source": item.get("source"),
                "module": item.get("module"),
                "dependencies": item.get("dependencies", []),
                "reason": "maven-preflight-failed",
                "maven_error_tail": failed[item["module"]],
            }
        )

    counts: dict[str, int] = {}
    for item in skipped:
        reason = item.get("reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    report["skipped_reason_counts"] = dict(sorted(counts.items()))


def main() -> int:
    args = parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))

    modules: dict[str, list[dict]] = {}
    for item in report.get("added", []):
        module = item.get("module")
        if module and module.startswith("autopack-"):
            modules.setdefault(module, []).append(item)

    if not modules:
        report["maven_preflight"] = {
            "checked_modules": 0,
            "passed_modules": 0,
            "failed_modules": 0,
            "failures": {},
        }
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print("Maven preflight: no generated autopack modules to validate")
        return 0

    failed: dict[str, str] = {}
    passed = 0
    print(f"Maven preflight: validating {len(modules)} generated driver modules")

    for module_name in sorted(modules):
        pom = args.cloudbeaver / "server" / "drivers" / module_name / "pom.xml"
        print(f"  CHECK {module_name}")
        ok, output = validate_module(args.maven, pom, args.timeout)
        if ok:
            passed += 1
            print(f"  PASS  {module_name}")
        else:
            failed[module_name] = output
            print(f"  SKIP  {module_name} (unresolvable Maven dependency)")
            if output:
                print("\n".join(f"        {line}" for line in output.splitlines()[-8:]))

    if failed:
        remove_failed_modules(args.cloudbeaver, report, failed)

    report["maven_preflight"] = {
        "checked_modules": len(modules),
        "passed_modules": passed,
        "failed_modules": len(failed),
        "failures": {
            module: {
                "drivers": [item.get("full_driver_id") for item in modules[module]],
                "error_tail": output,
            }
            for module, output in sorted(failed.items())
        },
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(
        f"Maven preflight complete: {passed} passed, {len(failed)} skipped; "
        "the full CloudBeaver build can continue"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
