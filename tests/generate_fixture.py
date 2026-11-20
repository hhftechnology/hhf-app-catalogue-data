import argparse
from pathlib import Path

from publisher import run
from tests.fixture_source import BUILD_TIME, ExampleGitHub, MemoryStore


def main():
    parser = argparse.ArgumentParser(description="Generate the public synthetic publisher fixture")
    parser.add_argument("directory", type=Path)
    destination = parser.parse_args().directory
    store = MemoryStore()
    run(store, ExampleGitHub(), now=BUILD_TIME, minimum=2)
    for key, body in store.objects.items():
        if key == "v1/manifest.json" or key.startswith("v1/snapshot/"):
            path = destination / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)


if __name__ == "__main__":
    main()
