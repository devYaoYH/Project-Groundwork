"""Print a private join link from an owner-only provisioning descriptor."""

import argparse

from a2a_agent.service import read_descriptor
from a2a_engine.remote.joinlink import encode_join_link


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor")
    args = parser.parse_args()
    try:
        link = encode_join_link(read_descriptor(args.descriptor))
    except (OSError, ValueError, KeyError, TypeError):
        parser.error("descriptor must be valid, unexpired owner-only provisioning")
    print(link)


if __name__ == "__main__":
    main()
