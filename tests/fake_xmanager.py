#!/usr/bin/env python3
import json
import sys

ALIASES = {
    "modeshape": ("xout-modeshape", "xout-modeshape"),
    "activemq": ("activemq-artemis-xout", "xout-activemq-artemis"),
    "pmc": ("xout-pmc", "xout-pmc"),
    "portal": ("xout-portal", "xout-portal"),
    "web": ("xout-web", "xout-web"),
    "batchsplitter": ("xout-batchsplitter", "xout-batchsplitter"),
}


def package_args(argv):
    values = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value.startswith("--package="):
            values.append(value.split("=", 1)[1])
        elif value == "--package" and index + 1 < len(argv):
            values.append(argv[index + 1])
            index += 1
        index += 1
    return values


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("xmanager.py 3.0.0")
        return 0

    if "package-plan" in argv:
        packages = []
        for alias in package_args(argv):
            package, service = ALIASES[alias]
            packages.append(
                {
                    "package": package,
                    "service": service,
                    "installed": "1.0-1",
                    "target": "1.1-1",
                    "action": "upgrade",
                    "reason": "target_is_newer",
                    "available": ["1.1-1", "1.0-1"],
                }
            )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "host": "ci-host",
                    "repository": "xout-repo",
                    "mode": "latest",
                    "release": None,
                    "release_file": None,
                    "allow_downgrade": False,
                    "packages": packages,
                    "has_changes": bool(packages),
                    "has_errors": False,
                }
            )
        )
        return 0

    if "list" in argv:
        print("[]")
        return 0

    print(json.dumps({"error": "unsupported fake command", "argv": argv}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
